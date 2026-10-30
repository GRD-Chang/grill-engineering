"""Synchronous test observation; not native Host/clock or business acceptance."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_run.adapters.remote import RemoteEffects
from agent_run.core.operation_runner import OperationRunner
from agent_run.storage.sqlite import Storage
from support import host_cost_trace as costs
from support.systemd_manager import _measure_host_calls


@pytest.fixture
def trace(tmp_path):
    root = tmp_path / 'host-costs'
    root.mkdir(mode=0o700)
    trace = costs.HostCosts(root)
    try:
        yield trace
    finally:
        os.close(trace.directory)


def test_nested_costs_exclude_children_without_counting_them_twice(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr(costs, 'time', SimpleNamespace(monotonic_ns=lambda: now[0]))
    root = tmp_path / 'host-costs'
    root.mkdir(mode=0o700)
    trace = costs.HostCosts(root)
    try:
        with trace.span('source'):
            now[0] = 10
            with trace.span('command.gh'):
                now[0] = 30
            now[0] = 50
        trace.flush()
        value, = costs.read_costs(tmp_path)['hosts']
        assert value['totals'] == {'source': [1, 1, 0, 50, 30], 'command.gh': [1, 1, 0, 20, 20]}
        assert value['active'] == [] and value['measurement_errors'] == 0
    finally:
        os.close(trace.directory)


def test_original_calls_keep_arguments_results_and_contextmanager_unwind(trace, monkeypatch, tmp_path):
    sentinel, calls = object(), []

    def source(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    @contextmanager
    def transaction(*args, **kwargs):
        calls.append(('enter', args, kwargs))
        try:
            yield sentinel
        finally:
            calls.append('exit')

    monkeypatch.setattr(RemoteEffects, 'source', source)
    monkeypatch.setattr(Storage, 'transaction', transaction)
    costs.observe_host(monkeypatch.setattr, trace)
    owner = object()
    assert RemoteEffects.source(owner, 'PRIVATE_ARGUMENT', token='PRIVATE_TOKEN') is sentinel
    failure = RuntimeError('PRIVATE_FAILURE')
    with pytest.raises(RuntimeError) as caught:
        with Storage.transaction(owner, write=False, deadline=123.25) as tx:
            assert tx is sentinel
            raise failure
    assert caught.value is failure
    assert calls == [((owner, 'PRIVATE_ARGUMENT'), {'token': 'PRIVATE_TOKEN'}),
                     ('enter', (owner,), {'write': False, 'deadline': 123.25}), 'exit']
    trace.flush()
    value, = costs.read_costs(tmp_path)['hosts']
    assert value['totals']['sqlite.reader'][:3] == [1, 1, 1]
    assert 'PRIVATE' not in json.dumps(value)


@pytest.mark.parametrize('original_fails', [False, True])
def test_exit_measurement_watchdog_keeps_original_failure_and_cannot_resume_business(trace, monkeypatch, original_fails):
    original, watchdog, calls = RuntimeError('original'), TimeoutError('watchdog'), []

    def source(*args, **kwargs):
        calls.append('original')
        if original_fails:
            raise original
        return object()

    writes = []
    def flush():
        writes.append(1)
        if len(writes) == 2:
            raise watchdog

    monkeypatch.setattr(RemoteEffects, 'source', source)
    costs.observe_host(monkeypatch.setattr, trace)
    monkeypatch.setattr(trace, 'flush', flush)
    # A handled ambient exception is not evidence that it is unwinding.
    try:
        raise RuntimeError('already handled')
    except RuntimeError:
        with pytest.raises(RuntimeError if original_fails else TimeoutError) as caught:
            RemoteEffects.source(object(), 1)
            calls.append('resumed')
    assert caught.value is (original if original_fails else watchdog)
    assert calls == ['original']


def test_regular_snapshot_write_failure_is_secondary_and_bounded(trace, monkeypatch, tmp_path):
    original = os.write
    failure = OSError('private diagnostic write unavailable')
    monkeypatch.setattr(costs.os, 'write', lambda *a: (_ for _ in ()).throw(failure))
    with trace.span('source', publish=True):
        pass
    assert trace.errors == 2
    monkeypatch.setattr(costs.os, 'write', original)
    trace.flush()
    value, = costs.read_costs(tmp_path)['hosts']
    assert value['measurement_errors'] == 2 and not list((tmp_path / 'host-costs').glob('*.tmp'))
    assert (tmp_path / 'host-costs' / trace.name).stat().st_size <= 16384


def test_real_git_retains_each_original_waitable_child_probe(git_repo, trace, monkeypatch, tmp_path):
    from agent_run.adapters.git_objects import LocalGit

    expected = LocalGit(git_repo).run('rev-parse', 'HEAD').stdout
    costs.observe_host(monkeypatch.setattr, trace)
    assert LocalGit(git_repo).run('rev-parse', 'HEAD').stdout == expected
    trace.flush()
    value, = costs.read_costs(tmp_path)['hosts']
    for phase in ('command.git_local', 'git.waitable_probe', 'pipe.pump'):
        assert value['totals'][phase][:3] == [1, 1, 0]


@pytest.mark.parametrize('host', [False, True])
def test_existing_hook_keeps_argv_and_only_installs_in_native_host(tmp_path, monkeypatch, host):
    _measure_host_calls(tmp_path, monkeypatch)
    argv = ['python', '-m', 'agent_run.runtime.executor' if host else 'agent_run', '--help']
    monkeypatch.setattr(sys, 'orig_argv', argv[:])
    monkeypatch.setattr(sys, 'path', sys.path[:])
    seen = []
    monkeypatch.setattr(costs, 'install_from_environment', lambda: seen.append('installed'))
    runpy.run_path(str(tmp_path / 'host-return-hook' / 'sitecustomize.py'))
    assert sys.orig_argv == argv and seen == (['installed'] if host else [])


def test_isolated_official_help_keeps_output_without_diagnostic_threads(tmp_path, monkeypatch):
    command = [sys.executable, '-m', 'agent_run.runtime.executor', '--help']
    before = subprocess.run(command, capture_output=True, timeout=5)
    _measure_host_calls(tmp_path, monkeypatch)
    after = subprocess.run(command, capture_output=True, timeout=5)
    assert (after.returncode, after.stdout, after.stderr) == (before.returncode, before.stdout, before.stderr)
    value, = costs.read_costs(tmp_path)['hosts']
    assert value['totals'] == {} and value['measurement_errors'] == 0


@pytest.mark.parametrize('failure_at', ['read', 'enabled_lookup'])
def test_failure_note_keeps_explicit_error_when_observation_fails(tmp_path, monkeypatch, failure_at):
    original = AssertionError('original boundary')
    monkeypatch.setenv('ENTRY_HOST_COST_ROOT', str(tmp_path / 'host-costs'))
    def unavailable(*_):
        raise TimeoutError('diagnostic watchdog')
    if failure_at == 'read':
        monkeypatch.setattr(costs, 'read_costs', unavailable)
    else:
        monkeypatch.setattr(costs, 'os', SimpleNamespace(environ=SimpleNamespace(get=unavailable)))
    try:
        raise original
    except AssertionError as error:
        costs.note_costs(error, tmp_path, arrivals_ns=[123])
        with pytest.raises(AssertionError) as caught:
            raise
    assert caught.value is original


def test_full_mode_does_not_install_hook_or_emit_cost_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv('ENTRY_HOST_COST_ROOT', raising=False)
    before = os.environ.get('PYTHONPATH')
    _measure_host_calls(tmp_path, monkeypatch, enabled=False)
    assert not (tmp_path / 'host-return-hook').exists() and not (tmp_path / 'host-costs').exists()
    assert os.environ.get('PYTHONPATH') == before and 'ENTRY_HOST_COST_ROOT' not in os.environ
    monkeypatch.setattr(costs, 'read_costs', lambda *_: pytest.fail('Full mode cannot read cost evidence'))
    costs.note_costs(AssertionError('original'), tmp_path)
    costs.report_costs(lambda *_: pytest.fail('Full mode cannot emit cost evidence'), tmp_path)


def test_active_spans_are_fixed_metadata_and_need_no_thread(trace, monkeypatch, tmp_path):
    monkeypatch.setattr(threading.Thread, 'start', lambda *_: pytest.fail('No diagnostic thread may start'))
    with trace.span('delivery', publish=True):
        value, = costs.read_costs(tmp_path)['hosts']
        assert value['totals']['delivery'][:3] == [1, 0, 0]
        active, = value['active']
        assert active[0]['phase'] == 'delivery' and active[0]['elapsed_ns'] >= 0


def test_official_main_globals_are_bound_after_load_once(trace, monkeypatch):
    calls, result = [], object()
    monkeypatch.delitem(sys.modules, 'agent_run.runtime.executor', raising=False)
    monkeypatch.setattr(OperationRunner, '__init__', lambda self: calls.append('init'))
    costs.observe_host(monkeypatch.setattr, trace)

    class Driver:
        def agent(self, *args, **kwargs):
            calls.append((args, kwargs))
            return result

    def wait(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    module = SimpleNamespace(__spec__=SimpleNamespace(name='agent_run.runtime.executor'),
                             _Executor=Driver, wait=wait)
    monkeypatch.setitem(sys.modules, '__main__', module)
    OperationRunner.__init__(object())
    OperationRunner.__init__(object())
    assert module.wait('private-future', timeout=1) is result
    assert module._Executor().agent('private-call', cancelled='private-event') is result
    assert calls == ['init', 'init', (('private-future',), {'timeout': 1}),
                     (('private-call',), {'cancelled': 'private-event'})]
    assert trace.totals['host.future_wait'][:3] == [1, 1, 0]
    assert trace.totals['worker'][:3] == [1, 1, 0]


def test_fifo_metadata_and_wait_keep_exact_calls_and_transaction_body(trace, monkeypatch):
    result, calls = object(), []

    def metadata(*args, **kwargs):
        calls.append(('metadata', args, kwargs))
        return result

    def pause(*args, **kwargs):
        calls.append(('pause', args, kwargs))
        return result

    @contextmanager
    def transaction(*args, **kwargs):
        calls.append(('transaction', args, kwargs))
        yield result
        calls.append('committed')

    monkeypatch.setattr(Storage, '_save_metadata', metadata)
    monkeypatch.setattr(Storage, '_save_pause', staticmethod(pause))
    monkeypatch.setattr(Storage, '_transaction', transaction)
    costs.observe_host(monkeypatch.setattr, trace)
    owner, request, cancelled, commit = object(), object(), object(), object()
    assert Storage._save_metadata(owner, request, (1, 2), cancelled, 'register', commit) is result
    assert Storage._save_metadata(self=owner, request=request, identity=(1, 2), cancelled=cancelled,
                                  operation='register', commit=commit) is result
    assert Storage._save_pause(123.25, cancelled) is result
    with Storage._transaction(owner, write=True, identity=(1, 2), deadline=123.25, select_run=False) as tx:
        assert tx is result
        calls.append('body')
    assert calls == [('metadata', (owner, request, (1, 2), cancelled, 'register', commit), {}),
                     ('metadata', (), {'self': owner, 'request': request, 'identity': (1, 2),
                                       'cancelled': cancelled, 'operation': 'register', 'commit': commit}),
                     ('pause', (123.25, cancelled), {}),
                     ('transaction', (owner,), {'write': True, 'identity': (1, 2), 'deadline': 123.25, 'select_run': False}),
                     'body', 'committed']
    assert {phase: row[:3] for phase, row in trace.totals.items()} == {
        'fifo.register': [2, 2, 0], 'fifo.retry_wait': [1, 1, 0], 'sqlite.metadata': [1, 1, 0]}


def test_concurrent_calls_keep_independent_stacks_and_atomic_snapshots(trace, tmp_path):
    arrived, release, errors = threading.Barrier(3, timeout=2), threading.Event(), []

    def call(phase):
        try:
            with trace.span(phase, publish=True):
                arrived.wait()
                assert release.wait(2)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=call, args=(phase,)) for phase in ('source', 'delivery')]
    try:
        for thread in threads:
            thread.start()
        arrived.wait()
        trace.flush()
        value, = costs.read_costs(tmp_path)['hosts']
        assert sorted(stack[0]['phase'] for stack in value['active']) == ['delivery', 'source']
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
    assert not errors and all(not thread.is_alive() for thread in threads)
    trace.flush()
    value, = costs.read_costs(tmp_path)['hosts']
    assert value['active'] == [] and all(row[:3] == [1, 1, 0] for row in value['totals'].values())


def test_close_failure_after_fd_reuse_never_closes_the_replacement(trace, monkeypatch):
    close, replacement, calls = os.close, [], []

    def released(fd):
        calls.append(fd)
        close(fd)
        if not replacement:
            reused = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            replacement.append(reused)
            assert reused == fd
            raise OSError('Close released its descriptor before reporting failure')

    monkeypatch.setattr(costs.os, 'close', released)
    try:
        trace.flush()
        assert replacement and calls.count(replacement[0]) == 1
        assert stat.S_ISCHR(os.fstat(replacement[0]).st_mode)
    finally:
        for fd in replacement:
            try:
                close(fd)
            except OSError:
                pass


@pytest.mark.parametrize('through_install', [False, True])
def test_initialization_watchdog_survives_cleanup_failure(tmp_path, monkeypatch, through_install):
    root = tmp_path / 'costs'
    root.mkdir(mode=0o700)
    watchdog, cleanup, closed = TimeoutError('Original constructor watchdog'), OSError('Cleanup close failed'), []

    def fstat(fd):
        raise watchdog

    def close(fd):
        os.close(fd)
        closed.append(fd)
        raise cleanup

    observed_os = SimpleNamespace(**{name: getattr(os, name) for name in
        ('open', 'O_RDONLY', 'O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'geteuid')},
        fstat=fstat, close=close, environ={'ENTRY_HOST_COST_ROOT': str(root)})
    monkeypatch.setattr(costs, 'os', observed_os)
    with pytest.raises(TimeoutError) as caught:
        costs.install_from_environment() if through_install else costs.HostCosts(root)
    assert caught.value is watchdog and caught.value.__cause__ is cleanup
    assert len(closed) == 1


def test_slow_snapshot_write_cannot_hold_another_threads_span(trace, monkeypatch):
    writing, release, entered, errors = threading.Event(), threading.Event(), threading.Event(), []
    original = os.write

    def write(fd, data):
        if threading.current_thread().name == 'cost-test-writer':
            writing.set()
            assert release.wait(2)
        return original(fd, data)

    def publisher():
        try:
            trace.flush()
        except BaseException as error:
            errors.append(error)

    def consumer():
        try:
            with trace.span('delivery', publish=True):
                entered.set()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(costs.os, 'write', write)
    writer, worker = threading.Thread(target=publisher, name='cost-test-writer'), threading.Thread(target=consumer)
    try:
        writer.start()
        assert writing.wait(1)
        worker.start()
        assert entered.wait(0.5), 'A business span waited for diagnostic file I/O'
        worker.join(timeout=0.5)
        assert not worker.is_alive() and trace.skipped_flushes == 2
    finally:
        release.set()
        writer.join(timeout=2)
        if worker.ident is not None:
            worker.join(timeout=2)
    assert not errors and not writer.is_alive() and not worker.is_alive()


def test_reader_prefers_latest_host_and_reports_omissions_and_age(tmp_path):
    root = tmp_path / 'host-costs'
    root.mkdir(mode=0o700)
    starts = []
    for _ in range(5):
        trace = costs.HostCosts(root)
        try:
            trace.flush()
            starts.append(trace.started)
        finally:
            os.close(trace.directory)
    value = costs.read_costs(tmp_path)
    assert len(value['hosts']) == 4 and value['omitted_hosts'] == 1
    assert [row['started_ns'] for row in value['hosts']] == list(reversed(starts[1:]))
    assert all(row['snapshot_age_ns'] >= 0 for row in value['hosts'])


def test_cli_records_are_bounded_report_omissions_and_precede_slow_failure_reads(tmp_path, monkeypatch):
    root = tmp_path / 'host-costs'
    root.mkdir(mode=0o700)
    for index in range(5):
        path = root / f'cli-{index}.jsonl'
        path.write_bytes(b'x' * 9000 if index == 4 else b'{"stage":"cli_return"}\n')
        path.chmod(0o600)
        os.utime(path, ns=(index + 1, index + 1))
    value = costs.read_costs(tmp_path)
    assert value['hosts'] == [] and value['omitted_cli_returns'] == 1
    assert [row['file'] for row in value['cli_returns']] == [f'cli-{index}.jsonl' for index in (4, 3, 2, 1)]
    assert value['cli_returns'][0]['truncated'] and not value['cli_returns'][0]['complete_line']
    assert len(value['cli_returns'][0]['trace']) == 8192
    assert all(not row['truncated'] and row['complete_line'] for row in value['cli_returns'][1:])
    order = []
    monkeypatch.setenv('ENTRY_HOST_COST_ROOT', str(root))
    monkeypatch.setattr(costs, 'read_costs', lambda *_: order.append('read') or value)
    child = SimpleNamespace(pid=123, poll=lambda: order.append('poll') or None)
    error = TimeoutError('original')
    costs.note_costs(error, tmp_path, process=child)
    assert order == ['poll', 'read']
    assert json.loads(error.__notes__[0].partition(': ')[2])['child_before_diagnostics']['returncode'] is None


@pytest.mark.parametrize('command', ['run', 'resume', 'approve', 'status', '--help'])
def test_cli_return_hook_only_observes_selected_control_commands(tmp_path, monkeypatch, command):
    from support import host_close_trace
    _measure_host_calls(tmp_path, monkeypatch)
    argv = ['python', '-m', 'agent_run', command]
    monkeypatch.setattr(sys, 'orig_argv', argv[:])
    monkeypatch.setattr(sys, 'path', sys.path[:])
    seen = []
    monkeypatch.setattr(host_close_trace, 'install_cli', lambda path: seen.append(path))
    runpy.run_path(str(tmp_path / 'host-return-hook' / 'sitecustomize.py'))
    assert sys.orig_argv == argv
    assert seen == ([tmp_path / 'host-costs' / f'cli-{os.getpid()}.jsonl']
                    if command in {'run', 'resume', 'approve'} else [])


def test_actual_cli_help_keeps_original_output_with_return_trace(tmp_path, monkeypatch):
    command = [sys.executable, '-m', 'agent_run', 'resume', '--help']
    before = subprocess.run(command, capture_output=True, timeout=5)
    _measure_host_calls(tmp_path, monkeypatch)
    after = subprocess.run(command, capture_output=True, timeout=5)
    assert (after.returncode, after.stdout, after.stderr) == (before.returncode, before.stdout, before.stderr)
    value = costs.read_costs(tmp_path)
    assert value['hosts'] == []
    trace, = value['cli_returns']
    row, = [json.loads(line) for line in trace['trace'].splitlines()]
    assert row['stage'] == 'cli_return' and row['result'] == 0 and row['pid'] > 0
    assert not trace['truncated'] and trace['complete_line']


def test_oversized_snapshot_replaces_old_data_with_explicit_bounded_marker(trace, tmp_path):
    trace.flush()
    trace.totals['private-invalid-phase-' + 'x' * 20000] = [1, 1, 0, 1, 1]
    trace.flush()
    value, = costs.read_costs(tmp_path)['hosts']
    assert value['status'] == 'oversized' and value['totals_omitted'] == 1
    assert 'totals' not in value and 'active' not in value and value['measurement_errors'] == 1
    assert (tmp_path / 'host-costs' / trace.name).stat().st_size < 1024


@pytest.mark.parametrize('stage', ['install', 'flush', 'report'])
@pytest.mark.parametrize('primary_type', [OSError, TimeoutError, InterruptedError, KeyboardInterrupt])
@pytest.mark.parametrize('cleanup_type', [OSError, TimeoutError, InterruptedError, KeyboardInterrupt])
def test_diagnostic_double_faults_never_swallow_watchdog_or_interrupt(
    trace, tmp_path, monkeypatch, stage, primary_type, cleanup_type,
):
    # Actual owned descriptors; only the original I/O failure and its close
    # failure are injected. No business/clock/Host evidence is supplied.
    trace.flush()
    primary, cleanup, closed, reported = primary_type('original diagnostic failure'), cleanup_type('close failure'), [], []

    def fail(*args, **kwargs):
        raise primary

    def close(fd):
        os.close(fd)
        closed.append(fd)
        raise cleanup

    names = ('open', 'read', 'write', 'close', 'fstat', 'geteuid', 'unlink', 'replace',
             'O_RDONLY', 'O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK', 'O_WRONLY', 'O_CREAT', 'O_EXCL')
    observed_os = SimpleNamespace(**{name: getattr(os, name) for name in names},
                                 environ={'ENTRY_HOST_COST_ROOT': str(tmp_path / 'host-costs')})
    observed_os.close = close
    setattr(observed_os, {'install': 'fstat', 'flush': 'write', 'report': 'read'}[stage], fail)
    monkeypatch.setattr(costs, 'os', observed_os)

    def invoke():
        if stage == 'install':
            return costs.install_from_environment()
        if stage == 'flush':
            return trace.flush()
        return costs.report_costs(lambda *row: reported.append(row), tmp_path)

    expected = primary if primary_type is not OSError else cleanup if cleanup_type is not OSError else None
    if expected is None:
        invoke()  # Ordinary diagnostic failures remain best effort.
        if stage == 'report':
            assert json.loads(reported[0][1]) == {'unavailable': 'OSError'}
    else:
        with pytest.raises(BaseException) as caught:
            invoke()
        assert caught.value is expected and not reported
    assert len(closed) == 1
