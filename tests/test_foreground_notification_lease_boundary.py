"""CLI lease boundaries with real flock and an injected Core transaction seam.

The manager supplies explicit synthetic Host observations. These tests prove
neither physical namespace exit nor native ordered-save/Host admission.
"""
from contextlib import closing, contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.cli import lifecycle
from agent_run.cli.main import _parser
from agent_run.model import HostObservation
from agent_run.notifications import sender
from agent_run.notifications.sender import notify_committed as notify_owned_history
from agent_run.runtime import package_lease
from agent_run.runtime.foreground_host import ForegroundHost, _settle
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from agent_run.storage import notification_outbox as outbox
from agent_run.storage.sqlite import Storage
from agent_run.adapters.foreground import record_root, write_record
from agent_run.storage.routing import RepositoryRoute
from support.notification import _kernel
from support.run_scoped_kernel import ScopedKernel


def _exclusive_available(path):
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    original = _kernel(tmp_path / 'state')
    with closing(sqlite3.connect(original.storage.path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, 'run-native')
        kernel.storage.path = original.storage.path
        kernel.storage.run_id = 'run-native'
        lock = tmp_path / 'runner.lock'
        held, notices = [], []
        original_lease = package_lease.runner_usage_lease

        @contextmanager
        def usage(path):
            with original_lease(path) as terminal:
                held.append(terminal.file)
                yield terminal

        monkeypatch.setattr(package_lease, 'runner_usage_lease', usage)
        monkeypatch.setattr(package_lease, 'default_runner_lock_path', lambda *_a: lock)
        monkeypatch.setattr(sender, 'notify_committed',
            lambda database, *, storage: notices.append((database, storage.run_id, [f.closed for f in held],
                                                        _exclusive_available(lock))))
        workspace = SimpleNamespace(repository='example/project', repository_root=tmp_path / 'state',
            state_root=kernel.storage.path.parent, open_path=lambda: None)
        route = RepositoryRoute(kernel.storage.path, workspace.repository_root, workspace.repository)
        monkeypatch.setattr(ManagedWorkspace, 'for_repository', staticmethod(lambda *_a: workspace))
        monkeypatch.setattr('agent_run.adapters.workspace.workspace_for_root', lambda *_a: workspace)
        monkeypatch.setattr(RepositoryRoute, 'open_task', lambda *_a: kernel)
        monkeypatch.setattr(RepositoryRoute, 'kernel', lambda *_a: kernel)
        monkeypatch.setattr(lifecycle, '_active_runner', lambda: True)
        yield kernel, lock, held, notices, route, original_lease


class _ObservedForeground(ForegroundHost):
    """The only synthetic physical leaf; Core still commits its original facts."""
    def __init__(self, lock, *, failure=None, retire=True):
        super().__init__(environment={})
        self.lock, self.failure, self.retire = lock, failure, retire
        self.calls = []

    def preflight(self):
        assert not _exclusive_available(self.lock), 'physical preparation outlived this caller lease'

    def reconcile_closed(self, kernel, lock):
        assert not _exclusive_available(lock)

    def _observe(self, kernel, request):
        assert not _exclusive_available(self.lock), 'Host observation escaped the caller lease'
        self.calls.append(request)
        # A prevalidated startup-domain exit, not a live process or namespace.
        common = {'boot_id': 'test-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
        domain = {'nonce': request.nonce,
            'observer': {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'},
            'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
            'executor': None}
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(self.lock),
                   'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]'}
        observation = HostObservation('exited', None, boot_id='test-boot', backend='foreground', domain=domain,
            evidence=_encode(receipt).decode(), usage_released=True, ended_at=kernel.clock(),
            exit_evidence='retained_namespace_pidfd')
        if self.retire:
            assert kernel.observe_host(request, observation) == 'exited'
        if self.failure is not None:
            raise self.failure
        return observation

    def launch(self, kernel, request, *, lock, lease):
        assert not lease.file.closed
        assert kernel.dispatch_host(request)
        return self._observe(kernel, request)

    def observe(self, kernel, request, lock):
        return self._observe(kernel, request)


def _admit(kernel, tmp_path):
    request = kernel.admit_host('original', kind='run', command=('controlled-host',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    return request


@pytest.mark.parametrize('entry', ['submit', 'attach', 'execute-run'])
@pytest.mark.parametrize('other_lease', [False, True], ids=['only-caller', 'another-usage-owner'])
def test_foreground_notification_runs_after_this_callers_real_leases_close(
    boundary, tmp_path, monkeypatch, entry, other_lease,
):
    kernel, lock, held, notices, route, original_lease = boundary
    manager = _ObservedForeground(lock)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    if entry == 'attach':
        _admit(kernel, tmp_path)
    parent = original_lease(lock) if other_lease else nullcontext()
    with parent:
        if entry == 'submit':
            result = lifecycle.submit(kernel, 'run', action_id='original', command=('controlled-host',),
                manager=manager, environment=dict(os.environ), runner_lock=lock)
        elif entry == 'attach':
            result = lifecycle.attach(kernel, requested_action='resume', manager=manager,
                                      environment=dict(os.environ), runner_lock=lock)
        else:
            parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project', '--foreground'])
            result = lifecycle.execute(parsed, language='en', repository_route=route)
        assert result['host'] == 'exited' and len(manager.calls) == 1
        assert notices == [(kernel.storage.path, 'run-native', [True] * len(held), not other_lease)]
    assert _exclusive_available(lock)


@pytest.mark.parametrize('entry', ['submit', 'attach', 'execute-run'])
@pytest.mark.parametrize('retired', [False, True], ids=['exit-unproved', 'committed-exit'])
def test_foreground_retirement_and_notification_errors_preserve_the_original_exception(
    boundary, tmp_path, monkeypatch, entry, retired,
):
    kernel, lock, held, notices, route, _ = boundary
    original = OSError('original foreground operation failure')
    manager = _ObservedForeground(lock, failure=original, retire=retired)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    if entry == 'attach':
        _admit(kernel, tmp_path)
    notify_calls = []

    def failed_notification(*_a, **_k):
        notify_calls.append(([file.closed for file in held], _exclusive_available(lock)))
        raise OSError('secondary notification failure')

    monkeypatch.setattr(sender, 'notify_committed', failed_notification)
    with pytest.raises(OSError) as caught:
        if entry == 'submit':
            lifecycle.submit(kernel, 'run', action_id='original', command=('controlled-host',),
                manager=manager, environment=dict(os.environ), runner_lock=lock)
        elif entry == 'attach':
            lifecycle.attach(kernel, requested_action='resume', manager=manager, environment=dict(os.environ), runner_lock=lock)
        else:
            parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project', '--foreground'])
            lifecycle.execute(parsed, language='en', repository_route=route)
    assert caught.value is original
    # A committed DB exit cannot prove the failing manager removed its record.
    assert notify_calls == []
    assert all(file.closed for file in held) and _exclusive_available(lock)


def test_run_keeps_its_original_attached_exit_without_launching_again(boundary, tmp_path, monkeypatch):
    kernel, lock, held, notices, route, _ = boundary
    request = _admit(kernel, tmp_path)
    manager = _ObservedForeground(lock)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project', '--foreground'])
    result = lifecycle.execute(parsed, language='en', repository_route=route)
    assert result['result'] == 'attached' and result['action_id'] == 'original'
    assert manager.calls == [request]
    assert notices == [(kernel.storage.path, 'run-native', [True], True)]


@pytest.mark.parametrize('status', ['not_running', 'complete', 'applied'])
def test_stop_readonly_receipts_do_not_create_notification_opportunities(boundary, monkeypatch, status):
    kernel, lock, held, notices, route, _ = boundary
    # Prevalidated Core return isolates the CLI branch and real flock lifetime.
    monkeypatch.setattr(kernel, 'stop_task', lambda *_a, **_k: status)
    parsed = _parser('en').parse_args(['stop', 'run-native', '--repo', 'example/project'])
    result = lifecycle.execute(parsed, language='en', repository_route=route)
    assert result['status'] == ('action_applied' if status == 'applied' else 'completed' if status == 'complete' else 'not_running')
    assert len(notices) == int(status == 'applied')
    if notices:
        assert notices == [(kernel.storage.path, 'run-native', [True], True)]
    assert all(file.closed for file in held) and _exclusive_available(lock)


def test_empty_foreground_attach_offers_history_recovery_without_new_core_facts(boundary):
    kernel, lock, held, notices, _, _ = boundary
    with kernel.storage.transaction(write=False) as tx:
        before = [tuple(row) for row in tx.rows('SELECT * FROM history ORDER BY position')]
    result = lifecycle.attach(kernel, requested_action='resume', manager=_ObservedForeground(lock),
                              environment={}, runner_lock=lock)
    assert result is None and notices == [(kernel.storage.path, 'run-native', [True], True)]
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.rows('SELECT * FROM history ORDER BY position')] == before
        assert not tx.rows('SELECT id FROM actions')
        assert not tx.rows("SELECT position FROM history WHERE kind='host_interrupted'")
    assert all(file.closed for file in held) and _exclusive_available(lock)


@pytest.mark.parametrize('entry', ['submit', 'attach', 'execute-run'])
def test_failed_foreground_call_does_not_infer_physical_cleanup_from_host_usage(boundary, tmp_path, monkeypatch, entry):
    kernel, lock, held, notices, route, _ = boundary
    original = OSError('original operation failure after committed retirement')
    manager = _ObservedForeground(lock, failure=original)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    if entry == 'attach':
        _admit(kernel, tmp_path)
    rechecks = []

    def unavailable(_action):
        rechecks.append(([file.closed for file in held], _exclusive_available(lock)))
        raise OSError('retirement recheck unavailable')

    monkeypatch.setattr(kernel, 'host_usage', unavailable)
    with pytest.raises(OSError) as caught:
        if entry == 'submit':
            lifecycle.submit(kernel, 'run', action_id='original', command=('controlled-host',),
                manager=manager, environment=dict(os.environ), runner_lock=lock)
        elif entry == 'attach':
            lifecycle.attach(kernel, requested_action='resume', manager=manager,
                             environment=dict(os.environ), runner_lock=lock)
        else:
            parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project', '--foreground'])
            lifecycle.execute(parsed, language='en', repository_route=route)
    assert caught.value is original
    assert rechecks == [] and notices == []
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT status,usage_released FROM hosts WHERE run_id=? AND nonce=?',
                      (kernel.storage.run_id, manager.calls[0].nonce))[:] == ('exited', 1)


def test_execute_keeps_retired_attach_notification_until_after_later_control_error(boundary, tmp_path, monkeypatch):
    kernel, lock, held, notices, route, _ = boundary
    request = _admit(kernel, tmp_path)
    manager = _ObservedForeground(lock)
    original = OSError('original later control read failed')
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)

    def failed_selection(**_kwargs):
        assert not _exclusive_available(lock), 'subsequent control ran after releasing its outer lease'
        raise original

    monkeypatch.setattr(kernel, 'resume_subject', failed_selection)
    parsed = _parser('en').parse_args(['resume', 'run-native', '--repo', 'example/project', '--foreground'])
    with pytest.raises(OSError) as caught:
        lifecycle.execute(parsed, language='en', repository_route=route)
    assert caught.value is original and manager.calls == [request]
    assert notices == [(kernel.storage.path, 'run-native', [True], True)]


@pytest.mark.parametrize('stderr_error', [OSError, BrokenPipeError])
def test_notification_diagnostic_failure_preserves_the_original_control_error(
    boundary, tmp_path, monkeypatch, stderr_error,
):
    kernel, lock, held, _notices, route, _ = boundary
    original = OSError('original control failure after successful physical settlement')
    manager = _ObservedForeground(lock)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    _admit(kernel, tmp_path)

    def unavailable(*_a, **_k):
        raise OSError('notification unavailable')

    def failed_control(**_kwargs):
        raise original

    def broken_write(_text):
        raise stderr_error('diagnostic output unavailable')

    monkeypatch.setattr(sender, 'notify_committed', unavailable)
    monkeypatch.setattr(kernel, 'resume_subject', failed_control)
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.sys, 'stderr', SimpleNamespace(write=broken_write))
        with pytest.raises(OSError) as caught:
            parsed = _parser('en').parse_args(['resume', 'run-native', '--repo', 'example/project', '--foreground'])
            lifecycle.execute(parsed, language='en', repository_route=route)
    assert caught.value is original
    assert manager.calls and all(file.closed for file in held) and _exclusive_available(lock)


@pytest.mark.parametrize('stderr_error', [OSError, BrokenPipeError])
@pytest.mark.parametrize('entry', ['submit', 'attach', 'execute-run'])
def test_notification_and_diagnostic_failure_preserve_successful_receipt(
    boundary, tmp_path, monkeypatch, entry, stderr_error,
):
    kernel, lock, held, _notices, route, _ = boundary
    manager = _ObservedForeground(lock)
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    if entry == 'attach':
        _admit(kernel, tmp_path)

    def unavailable(*_a, **_k):
        raise OSError('notification unavailable after successful retirement')

    def broken_write(_text):
        raise stderr_error('diagnostic output unavailable')

    monkeypatch.setattr(sender, 'notify_committed', unavailable)
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.sys, 'stderr', SimpleNamespace(write=broken_write))
        if entry == 'submit':
            result = lifecycle.submit(kernel, 'run', action_id='original', command=('controlled-host',),
                manager=manager, environment=dict(os.environ), runner_lock=lock)
        elif entry == 'attach':
            result = lifecycle.attach(kernel, requested_action='resume', manager=manager,
                environment=dict(os.environ), runner_lock=lock)
        else:
            parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project', '--foreground'])
            result = lifecycle.execute(parsed, language='en', repository_route=route)
    assert result['result'] == ('attached' if entry == 'attach' else 'accepted')
    assert result['host'] == 'exited' and len(manager.calls) == 1
    assert all(file.closed for file in held) and _exclusive_available(lock)


@pytest.mark.parametrize('entry', ['attach', 'execute-resume'])
@pytest.mark.parametrize('record_state', ['retained', 'deleted'])
def test_empty_foreground_entry_recovers_committed_exit_before_its_first_outbox_write(
    boundary, tmp_path, monkeypatch, entry, record_state,
):
    kernel, lock, held, notices, route, original_lease = boundary
    now = time.time() + .25
    kernel.clock = lambda: now
    request = _admit(kernel, tmp_path)
    with original_lease(lock):
        observed = _ObservedForeground(lock).observe(kernel, request, lock)
        path = record_root(lock, create=True) / (request.nonce + '.json')
        record = {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
            'receipt': json.loads(observed.evidence), 'phase': 'exited', 'domain': observed.domain,
            'database': str(kernel.storage.path.resolve()), 'action': 'original',
            'ended_at': observed.ended_at, 'executor_exit_code': observed.exit_status,
            'namespace_exit_code': observed.namespace_exit_status, 'exit_basis': observed.exit_evidence}
        write_record(path, record, create=True)
        if record_state == 'deleted':
            _settle(kernel, request, path, record)
    with kernel.storage.transaction(write=False) as tx:
        before = [tuple(row) for row in tx.rows('SELECT * FROM history ORDER BY position')]
        assert len([row for row in tx.rows("SELECT position FROM history WHERE kind='host_interrupted'")]) == 1
    assert kernel.active_host_action() is None
    assert not outbox.notification_path(kernel.storage.path.parent, request.run_id).exists()
    assert path.exists() is (record_state == 'retained')

    manager = ForegroundHost(environment={})
    monkeypatch.setattr(manager, 'preflight', lambda: None)  # No native namespace is claimed.
    monkeypatch.setattr(lifecycle, '_manager_for', lambda *_a: manager)
    monkeypatch.setattr(sender, 'status_subjects', lambda *_a, **_k: [])
    real_notify, real_sender = notify_owned_history, sender.NativeNotifications
    delivered = []

    def transport(*_a, **_k):
        pending = outbox.read_notifications(kernel.storage.path.parent, request.run_id)['pending']
        item, = [item for item in pending if item['outcome'] == 'sending']
        delivered.append((item['event']['kind'], all(file.closed for file in held), _exclusive_available(lock)))
        return {'outcome': 'success', 'reason': None}

    monkeypatch.setattr(sender, 'NativeNotifications',
        lambda database, root, **options: real_sender(database, root, transport=transport, **options))

    def notify(database, *, storage):
        notices.append((database, storage.run_id, [file.closed for file in held], _exclusive_available(lock)))
        # A fresh native read-only Storage owns sender-thread reads; Core above
        # remains on its explicitly injected, caller-thread transaction seam.
        real_notify(database, storage=Storage(database, run_id=storage.run_id))

    monkeypatch.setattr(sender, 'notify_committed', notify)
    if entry == 'attach':
        assert lifecycle.attach(kernel, requested_action='resume', manager=manager,
                                environment=dict(os.environ), runner_lock=lock) is None
    else:
        failure = OSError('later control selection unavailable')

        def unavailable(**_kw):
            assert not _exclusive_available(lock)
            raise failure

        monkeypatch.setattr(kernel, 'resume_subject', unavailable)
        parsed = _parser('en').parse_args(['resume', 'run-native', '--repo', 'example/project', '--foreground'])
        with pytest.raises(OSError) as caught:
            lifecycle.execute(parsed, language='en', repository_route=route)
        assert caught.value is failure
    assert notices == [(kernel.storage.path, 'run-native', [True], True)]
    assert ('executor_exit', True, True) in delivered
    assert all(closed and available for _kind, closed, available in delivered)
    assert not path.exists()
    document = outbox.read_notifications(kernel.storage.path.parent, request.run_id)
    exit_record, = [item for item in document['records'] if item['kind'] == 'executor_exit']
    assert exit_record['outcome'] == 'success' and exit_record['attempts'] == 1
    assert exit_record['created_at'] == now and exit_record['deadline_at'] == now + 60
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.rows('SELECT * FROM history ORDER BY position')] == before
        assert tx.one('SELECT count(*) FROM actions')[0] == 1
