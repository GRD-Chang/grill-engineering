"""Retired foreground notification facts with synthetic domain observations.

Core uses the existing controlled transaction seam. Files, flock, outbox and
sender ownership are real; no namespace, Host or Worker process is launched.
"""
from agent_run.runtime import host_control
from contextlib import closing
import json
import sqlite3
from threading import Event
import time
from types import SimpleNamespace

import pytest

from agent_run.adapters.foreground import DomainUnknown, record_root, write_record
from agent_run.notifications.event_projection import event_state, project_event
from agent_run.notifications.sender import NativeNotifications
from agent_run.runtime.host_recovery import HostExitUnavailable, foreground_exit_notice, host_exit_current
from agent_run.runtime.foreground_host import _settle
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from agent_run.runtime.package_lease import RunnerLeaseBusy, runner_management_lease, runner_usage_lease
from agent_run.model import HostObservation
from agent_run.storage import notification_outbox as outbox
from support.notification import _kernel
from support.observer_errors import ObserverErrors
from support.run_scoped_kernel import ScopedKernel


@pytest.fixture
def exited_input(tmp_path):
    original = _kernel(tmp_path)
    with closing(sqlite3.connect(original.storage.path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        core = ScopedKernel(db, original.storage.run_id)
        core.storage.path, core.storage.run_id = original.storage.path, original.storage.run_id
        now = [time.time() + .25]
        core.clock = lambda: now[0]
        lock = tmp_path / 'runner.lock'
        with runner_usage_lease(lock):
            request = core.admit_host('original', kind='run', command=('controlled-executor',),
                environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
            assert core.dispatch_host(request)
            observer = {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'}
            common = {'boot_id': 'test-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
            domain = {'nonce': request.nonce, 'observer': observer,
                'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
                'executor': {**common, 'pid': 202, 'parent': 200, 'start_token': '203', 'namespace_pids': [202, 2]}}
            receipt = {'request_digest': _digest(request), 'nonce': request.nonce,
                'runner': _runner_witness(lock), 'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]'}
            running = HostObservation('running', None, backend='foreground', boot_id='test-boot',
                main_pid=202, start_token='203', domain=domain, usage_ready=True, evidence=_encode(receipt).decode())
            assert core.observe_host(request, running) == 'running'
            path = record_root(lock, create=True) / (request.nonce + '.json')
            record = {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
                'receipt': receipt, 'phase': 'exited', 'domain': domain,
                'database': str(original.storage.path.resolve()), 'action': 'original',
                'ended_at': now[0], 'executor_exit_code': -9, 'namespace_exit_code': -9,
                'exit_basis': 'retained_namespace_pidfd'}
            write_record(path, record, create=True)
        yield SimpleNamespace(original=original, core=core, db=db, request=request, path=path,
                              record=record, lock=lock, now=now)


def notice(f):
    return foreground_exit_notice(f.original.storage.path, 'original', f.path, storage=f.original.storage)


def test_settlement_commits_and_removes_record_before_any_notification(exited_input, monkeypatch):
    f = exited_input
    def forbidden(*args, **kwargs):
        pytest.fail('Physical settlement must not call notification delivery')
    monkeypatch.setattr('agent_run.notifications.sender.deliver_host_exit_notice', forbidden)
    monkeypatch.setattr('agent_run.notifications.sender.notify_committed', forbidden)
    with runner_usage_lease(f.lock):
        assert _settle(f.core, f.request, f.path, f.record).usage_released
    assert not f.path.exists()
    with runner_management_lease(f.lock):
        pass
    projected = notice(f)
    assert projected is not None and host_exit_current(f.original.storage.path, projected, storage=f.original.storage)
    assert projected['executor_exit_code'] == -9 and projected['ended_at'] == f.record['ended_at']


@pytest.mark.parametrize('status', [None, 0])
def test_unknown_executor_code_remains_unknown_and_zero_is_not_an_alert(exited_input, status):
    f = exited_input
    f.record['executor_exit_code'] = status
    write_record(f.path, f.record)
    _settle(f.core, f.request, f.path, f.record)
    projected = notice(f)
    assert (projected is None) is (status == 0)
    if projected is not None:
        assert projected['executor_exit_code'] is None


@pytest.mark.parametrize('field', ['nonce', 'request_digest', 'domain', 'runner', 'physical_record'])
def test_retired_notice_refuses_changed_original_identity(exited_input, field):
    f = exited_input
    _settle(f.core, f.request, f.path, f.record)
    projected = notice(f)
    assert projected is not None
    changed = dict(projected)
    changed[field] = 'foreign' if field != 'runner' else dict(projected['runner'], inode=0)
    assert not host_exit_current(f.original.storage.path, changed, storage=f.original.storage)


@pytest.mark.parametrize('boundary', ['stop', 'abandon', 'successor', 'new-executor', 'old-task'])
def test_retired_notice_rechecks_task_and_later_execution(exited_input, boundary):
    f = exited_input
    _settle(f.core, f.request, f.path, f.record)
    projected = notice(f)
    assert projected is not None
    if boundary == 'stop':
        f.core.control('stop', 'new-stop')
    elif boundary == 'abandon':
        f.core.abandon_task('new-abandon', resource_inspections=())
    elif boundary == 'successor':
        f.core.admit_host('new-host', kind='run', command=('controlled-executor',),
            environment_carrier=f.path.parent / 'new-carrier', preflight=lambda: None, backend='foreground')
    elif boundary == 'new-executor':
        # Corrupt external metadata is a negative input, never an execution grant.
        f.db.execute('UPDATE runs SET executor=? WHERE id=?', ('foreign-owner', f.request.run_id))
    else:
        f.db.execute('UPDATE tasks SET current_run=NULL WHERE current_run=?', (f.request.run_id,))
        with pytest.raises(HostExitUnavailable):
            host_exit_current(f.original.storage.path, projected, storage=f.original.storage)
        return
    assert not host_exit_current(f.original.storage.path, projected, storage=f.original.storage)


def test_retirement_commit_failure_preserves_the_management_barrier(exited_input):
    f = exited_input
    def reject_commit(action, one, two, database, source):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and one == 'COMMIT' else sqlite3.SQLITE_OK
    f.db.set_authorizer(reject_commit)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            _settle(f.core, f.request, f.path, f.record)
    finally:
        f.db.set_authorizer(None)
    assert f.path.exists() and f.core.host_usage('original').disposition == 'running'
    assert f.db.execute("SELECT count(*) FROM history WHERE kind='host_interrupted'").fetchone()[0] == 0
    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(f.lock):
            pytest.fail('Failed Core retirement released the protection barrier')


def test_history_recovery_keeps_original_window_and_slow_delivery_has_no_usage_lease(exited_input, monkeypatch):
    f = exited_input
    _settle(f.core, f.request, f.path, f.record)
    assert not outbox.notification_path(f.original.storage.path.parent, f.request.run_id).exists()
    expected = notice(f)
    assert expected is not None
    # No sender ran between Core commit and deletion: recovery consumes only history.
    f.now[0] += 10
    monkeypatch.setattr('agent_run.notifications.sender.time', SimpleNamespace(time=lambda: f.now[0]))
    entered, release, settled = Event(), Event(), Event()
    errors = ObserverErrors()
    original_finish = NativeNotifications._finish_result
    def transport(*args, **kwargs):
        with errors.capture():
            entered.set()
            assert release.wait(3)
            return {'outcome': 'unknown', 'reason': 'controlled lost ACK'}
    def finish(self, claim, result, returned_at):
        original_finish(self, claim, result, returned_at)
        settled.set()
    monkeypatch.setattr(NativeNotifications, '_finish_result', finish)
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *a, **kw: [])
    sender = NativeNotifications(f.original.storage.path, f.original.storage.path.parent,
        transport=transport, storage=f.original.storage)
    try:
        assert entered.wait(3)
        with runner_management_lease(f.lock):
            pass
        pending = outbox.read_notifications(f.original.storage.path.parent, f.request.run_id)['pending']
        event, = [item for item in pending if item['event']['kind'] == 'executor_exit']
        assert event['event']['exit_evidence'] == expected and event['attempts'] == 1
        assert event['created_at'] == f.record['ended_at'] and event['deadline_at'] == f.record['ended_at'] + 60
        release.set()
        assert settled.wait(3)
    finally:
        release.set()
        sender.close(drain=False)
        assert sender.closed
        errors.raise_if_any()


def test_late_history_recovery_exhausts_original_window_without_a_send_or_renewal(exited_input):
    f = exited_input
    _settle(f.core, f.request, f.path, f.record)
    with f.original.storage.transaction(write=False) as tx:
        row = dict(tx.one("SELECT * FROM history WHERE run_id=? AND kind='host_interrupted'",
                          (f.request.run_id,)))
        projected, = project_event(tx, row, event_state(tx), 'concise')
    recovered_at = f.record['ended_at'] + 61
    with outbox.transaction(f.original.storage.path.parent, f.request.run_id) as document:
        NativeNotifications._enqueue_history(document, projected.content, recovered_at)
        outbox.settle(document, recovered_at)
    first = outbox.read_notifications(f.original.storage.path.parent, f.request.run_id)
    retired, = [item for item in first['records'] if item['kind'] == 'executor_exit']
    assert retired['attempts'] == 0 and retired['exhaustion_reason'] == 'deadline'
    assert retired['created_at'] == f.record['ended_at']
    assert retired['deadline_at'] == f.record['ended_at'] + 60
    # A second process opening the same journal cannot issue another budget.
    with outbox.transaction(f.original.storage.path.parent, f.request.run_id) as document:
        NativeNotifications._enqueue_history(document, projected.content, recovered_at + 120)
        outbox.settle(document, recovered_at + 120)
    assert outbox.read_notifications(f.original.storage.path.parent, f.request.run_id) == first


@pytest.mark.parametrize('stage', ['unlink', 'directory-open', 'directory-fsync'])
def test_failed_record_removal_defers_notification_and_preserves_the_original_error(exited_input, monkeypatch, stage):
    import os
    from pathlib import Path
    from agent_run.cli import lifecycle
    from agent_run.runtime import host_control
    from agent_run.runtime.foreground_host import ForegroundHost
    f = exited_input
    original = OSError('controlled record removal failure')
    calls = []
    root = f.path.parent.stat()
    unlink, open_directory, fsync = Path.unlink, os.open, os.fsync

    def remove(path, *args, **kwargs):
        if stage == 'unlink' and path == f.path:
            raise original
        return unlink(path, *args, **kwargs)

    def opened(path, flags, *args, **kwargs):
        if stage == 'directory-open' and Path(path) == f.path.parent and flags & os.O_DIRECTORY:
            raise original
        return open_directory(path, flags, *args, **kwargs)

    def synced(descriptor):
        info = os.fstat(descriptor)
        if stage == 'directory-fsync' and (info.st_dev, info.st_ino) == (root.st_dev, root.st_ino):
            raise original
        return fsync(descriptor)

    class SettlingHost(ForegroundHost):
        def launch(self, kernel, request, *, lock, lease):
            assert not lease.file.closed
            return _settle(kernel, request, f.path, f.record)

    monkeypatch.setattr(host_control, 'admit', lambda *_a, **_k: f.request)
    monkeypatch.setattr('agent_run.notifications.sender.notify_committed', lambda *_a, **_k: calls.append(True))
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', remove)
        patch.setattr(os, 'open', opened)
        patch.setattr(os, 'fsync', synced)
        with pytest.raises(OSError) as caught:
            host_control.submit(f.core, 'run', action_id='original', command=f.request.command,
                manager=SettlingHost(environment={}), environment={}, runner_lock=f.lock)
        assert caught.value is original
        assert calls == [] and f.core.host_usage('original').usage_released
        assert f.path.exists() is (stage == 'unlink')
        if stage == 'unlink':
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(f.lock):
                    pytest.fail('The retained record lost its management barrier')
        expected = notice(f)
        assert expected is not None
        with pytest.raises(HostExitUnavailable):
            host_exit_current(f.original.storage.path, expected, storage=f.original.storage)
    # A later healthy entry completes the original directory sync and wakes history.
    assert host_control.attach(f.core, requested_action='resume', manager=ForegroundHost(environment={}),
        environment={}, runner_lock=f.lock) is None
    assert calls == [True] and not f.path.exists()
    assert notice(f) == expected
    assert host_exit_current(f.original.storage.path, expected, storage=f.original.storage)


@pytest.mark.parametrize('boundary', ['record-present', 'sync-failed'])
def test_existing_sender_waits_for_physical_removal_without_renewing_the_exit_budget(exited_input, monkeypatch, boundary):
    import os
    from agent_run.runtime.foreground_host import ForegroundHost, _observation
    f = exited_input
    calls, errors = [], ObserverErrors()
    fsync, root = os.fsync, f.path.parent.stat()

    def failed_sync(descriptor):
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (root.st_dev, root.st_ino):
            raise OSError('controlled deletion durability unavailable')
        return fsync(descriptor)

    def transport(*_a, **_k):
        with errors.capture():
            document = outbox.read_notifications(f.original.storage.path.parent, f.request.run_id)
            sending, = [item for item in document['pending'] if item['outcome'] == 'sending']
            calls.append(sending['event']['kind'])
            return {'outcome': 'success', 'reason': None}

    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *_a, **_k: [])
    sender = None
    try:
        with monkeypatch.context() as patch:
            if boundary == 'record-present':
                assert f.core.observe_host(f.request, _observation(f.request, f.record, 'exited', released=True)) == 'exited'
            else:
                patch.setattr(os, 'fsync', failed_sync)
                with pytest.raises(OSError, match='durability unavailable'):
                    _settle(f.core, f.request, f.path, f.record)
            sender = NativeNotifications(f.original.storage.path, f.original.storage.path.parent,
                transport=transport, storage=f.original.storage)
            with sender.condition:
                assert sender.condition.wait_for(lambda: sender.failure_reason is not None, timeout=3)
            assert 'executor_exit' not in calls
            document = outbox.read_notifications(f.original.storage.path.parent, f.request.run_id)
            pending, = [item for item in document['pending'] if item['event']['kind'] == 'executor_exit']
            assert pending['attempts'] == 0
            assert pending['created_at'] == f.record['ended_at']
            assert pending['deadline_at'] == f.record['ended_at'] + 60
            identity = pending['event']['id']
        with runner_usage_lease(f.lock):
            ForegroundHost(environment={}).reconcile_closed(f.core, f.lock)
        assert not f.path.exists()
        sender.observe_committed()
        sender.close(timeout_seconds=3)
        errors.raise_if_any()
        assert sender.failure_reason is None and calls.count('executor_exit') == 1
        document = outbox.read_notifications(f.original.storage.path.parent, f.request.run_id)
        retired, = [item for item in document['records'] if item['kind'] == 'executor_exit']
        assert retired['id'] == identity and retired['attempts'] == 1
        assert retired['created_at'] == pending['created_at'] and retired['deadline_at'] == pending['deadline_at']
    finally:
        if sender is not None:
            sender.close(drain=False)
            assert sender.closed
        errors.raise_if_any()
