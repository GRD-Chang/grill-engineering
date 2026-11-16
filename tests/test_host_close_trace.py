"""Diagnostic non-interference; no native Host or business completion proof."""
import faulthandler
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.cli import lifecycle, main as cli_main
from agent_run.model import Step
from agent_run.notifications.sender import NativeNotifications
from agent_run.runtime.foreground_host import ForegroundHost
from support import host_close_trace
from support.systemd_manager import _hold_host_operation_return


def install(tmp_path, monkeypatch, *, run=None, close=None):
    monkeypatch.setattr(OperationRunner, 'run_once', run or (lambda self, *, agent_capacity=1: Step('complete', 'private-subject')))
    monkeypatch.setattr(NativeNotifications, 'close', close or (lambda self, *args, **kwargs: None))
    path = tmp_path / 'trace.jsonl'
    fd = host_close_trace.install(path)
    return path, fd


def test_original_results_arguments_and_fixed_bounded_records(tmp_path, monkeypatch):
    step, result, calls = Step('complete', 'secret-subject'), object(), []
    def run(self, *, agent_capacity=1):
        calls.append(('run', self, agent_capacity))
        return step
    def close(self, *args, **kwargs):
        calls.append(('close', self, args, kwargs))
        return result
    path, fd = install(tmp_path, monkeypatch, run=run, close=close)
    runner, sender = object(), object()
    try:
        assert not os.get_inheritable(fd)
        for _ in range(100):
            assert OperationRunner.run_once(runner, agent_capacity=0) is step
        assert NativeNotifications.close(sender, drain=True, timeout_seconds=60) is result
        assert calls[-1] == ('close', sender, (), {'drain': True, 'timeout_seconds': 60})
        assert calls[0] == ('run', runner, 0)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row['stage'] for row in rows] == ['run_once_return_complete', 'notification_close_enter', 'notification_close_return']
        assert all(set(row) == {'stage', 'seconds'} and row['seconds'] >= 0 for row in rows)
        assert 'secret' not in path.read_text() and path.stat().st_size < 4096
    finally:
        os.close(fd)


@pytest.mark.parametrize('where', ['run', 'close'])
def test_original_failure_is_same_object_and_no_false_return(tmp_path, monkeypatch, where):
    failure = RuntimeError('original failure')
    def fail(*args, **kwargs):
        raise failure
    path, fd = install(tmp_path, monkeypatch, **{where: fail})
    try:
        invoke = OperationRunner.run_once if where == 'run' else NativeNotifications.close
        with pytest.raises(RuntimeError) as caught:
            invoke(object())
        assert caught.value is failure
        assert [json.loads(line)['stage'] for line in path.read_text().splitlines()] == (
            [] if where == 'run' else ['notification_close_enter'])
    finally:
        os.close(fd)


@pytest.mark.parametrize('fault', [OSError('trace IO'), TimeoutError('original watchdog')])
def test_trace_io_failure_is_benign_but_watchdog_cannot_resume_business(tmp_path, monkeypatch, fault):
    path, fd = install(tmp_path, monkeypatch)
    def fail(*args):
        raise fault
    monkeypatch.setattr(host_close_trace.os, 'write', fail)
    try:
        if isinstance(fault, TimeoutError):
            with pytest.raises(TimeoutError) as caught:
                NativeNotifications.close(object())
            assert caught.value is fault
        else:
            assert OperationRunner.run_once(object()).kind == 'complete'
    finally:
        os.close(fd)


def test_handled_except_cannot_replace_watchdog_and_allow_continuation(tmp_path, monkeypatch):
    _, fd = install(tmp_path, monkeypatch)
    watchdog, continued = TimeoutError('watchdog'), []
    def fail(*args):
        raise watchdog
    monkeypatch.setattr(host_close_trace.os, 'write', fail)
    try:
        with pytest.raises(TimeoutError) as caught:
            try:
                try:
                    raise RuntimeError('already handled')
                except RuntimeError:
                    OperationRunner.run_once(object())
            except RuntimeError:
                continued.append('caught obsolete exception')
            continued.append('business continued')
        assert caught.value is watchdog and continued == []
    finally:
        os.close(fd)


@pytest.mark.parametrize('close_fails', [False, True])
def test_entry_watchdog_still_closes_once_with_original_arguments(tmp_path, monkeypatch, close_fails):
    watchdog, cleanup, calls = TimeoutError('entry watchdog'), RuntimeError('close failed'), []
    def close(self, *args, **kwargs):
        calls.append((self, args, kwargs))
        if close_fails:
            raise cleanup
        return object()
    _, fd = install(tmp_path, monkeypatch, close=close)
    def fail(*args):
        raise watchdog
    monkeypatch.setattr(host_close_trace.os, 'write', fail)
    sender = object()
    try:
        with pytest.raises(TimeoutError) as caught:
            NativeNotifications.close(sender, 'original', drain=False, timeout_seconds=60)
        assert caught.value is watchdog
        assert calls == [(sender, ('original',), {'drain': False, 'timeout_seconds': 60})]
        assert caught.value.__cause__ is (cleanup if close_fails else None)
    finally:
        os.close(fd)


@pytest.mark.parametrize('missing', [False, True])
def test_failure_note_keeps_exit_code_and_never_returns(tmp_path, missing):
    hook = tmp_path / 'host-return-hook'
    hook.mkdir()
    if not missing:
        (hook / 'close-trace.jsonl').write_text('x' * 10000)
    original = AssertionError('original deadline failure')
    with pytest.raises(AssertionError) as caught:
        host_close_trace.raise_with_trace(original, tmp_path, [SimpleNamespace(poll=lambda: 0)])
    assert caught.value is original
    facts = json.loads(original.__notes__[-1].partition(': ')[2])
    assert facts['child_poll'] == [0]
    assert (facts.get('trace_unavailable') == 'FileNotFoundError') if missing else len(facts['trace']) == 4096


def test_failure_note_watchdog_rethrows_original_without_continuing(tmp_path, monkeypatch):
    original, watchdog = AssertionError('body'), TimeoutError('watchdog')
    def fail(*args, **kwargs):
        raise watchdog
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(AssertionError) as caught:
        host_close_trace.raise_with_trace(original, tmp_path, [])
    assert caught.value is original


def test_observer_does_not_arm_native_dump_even_with_old_snapshot_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ENTRY_HOST_SNAPSHOT_AT', '108.0')
    monkeypatch.setattr(OperationRunner, 'run_once', lambda self, **options: Step('complete', 'original'))
    monkeypatch.setattr(NativeNotifications, 'close', lambda self, **options: None)
    def forbidden(*args, **options):
        pytest.fail('Stage observation armed an unsafe native traceback watchdog')
    monkeypatch.setattr(faulthandler, 'dump_traceback_later', forbidden)
    descriptors, original_open = [], os.open
    def opened(*args, **options):
        descriptor = original_open(*args, **options)
        descriptors.append(descriptor)
        return descriptor
    monkeypatch.setattr(host_close_trace.os, 'open', opened)
    path = tmp_path / 'trace.jsonl'
    existing = {p.name for p in tmp_path.iterdir()}
    try:
        fd = host_close_trace.install(path)
        assert descriptors == [fd] and not os.get_inheritable(fd)
        assert OperationRunner.run_once(object()).kind == 'complete'
        assert NativeNotifications.close(object(), drain=True) is None
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row['stage'] for row in rows] == [
            'run_once_return_complete', 'notification_close_enter', 'notification_close_return']
        assert {p.name for p in tmp_path.iterdir()} == existing | {'trace.jsonl'}
    finally:
        for descriptor in descriptors:
            os.close(descriptor)  # The forbidden fake never installed a C timer.


@pytest.mark.parametrize('failure', [RuntimeError('note unavailable'), TimeoutError('note watchdog')])
def test_abandon_prefix_note_failure_still_rethrows_original_assertion(tmp_path, monkeypatch, failure):
    from support import managed_abandon_recovery

    seen, releases = [], []
    release = SimpleNamespace(set=lambda: releases.append('released'))
    child = SimpleNamespace(poll=lambda: 0)
    kernel = SimpleNamespace(storage=SimpleNamespace(path=tmp_path / 'unopened.sqlite3', run_id='original'),
                             source_status=lambda: {'prefix': 'controlled failure'})

    def note(error, **options):
        seen.append((error, options['stage']))
        raise failure

    monkeypatch.setattr(managed_abandon_recovery, '_managed_failure_note', note)
    with pytest.raises(AssertionError) as caught:
        with managed_abandon_recovery.final_approval_hold(SimpleNamespace(wait=lambda _: False), release,
                SimpleNamespace(is_alive=lambda: False), kernel, tmp_path, [child], 0):
            pytest.fail('A failed approval observation continued to Abandon')
    assert len(seen) == 1 and caught.value is seen[0][0]
    assert seen[0][1] == 'abandon-before-first-final-approval'
    assert releases == ['released']
    facts = json.loads(caught.value.__notes__[-1].partition(': ')[2])
    assert facts['child_poll'] == [0] and facts['trace_unavailable'] == 'FileNotFoundError'


@pytest.mark.parametrize('fault', [OSError('trace open'), TimeoutError('original startup watchdog'), InterruptedError('startup interrupt')])
def test_install_io_failure_cannot_hide_startup_watchdog(tmp_path, monkeypatch, fault):
    def fail(*args, **kwargs):
        raise fault
    monkeypatch.setattr(host_close_trace.os, 'open', fail)
    if isinstance(fault, (TimeoutError, InterruptedError)):
        with pytest.raises(type(fault)) as caught:
            install(tmp_path, monkeypatch)
        assert caught.value is fault
    else:
        _, fd = install(tmp_path, monkeypatch)
        assert fd == -1
        assert OperationRunner.run_once(object()).kind == 'complete'
        assert NativeNotifications.close(object()) is None


@pytest.mark.parametrize('host', [False, True])
def test_existing_hook_keeps_official_module_argv_and_other_processes_untouched(tmp_path, monkeypatch, host):
    _hold_host_operation_return(tmp_path, monkeypatch, hold=False, observe_close=True)
    argv = ['python', '-m', 'agent_run.runtime.executor' if host else 'agent_run', '--help']
    monkeypatch.setattr(sys, 'orig_argv', argv[:])
    monkeypatch.setattr(sys, 'path', sys.path[:])
    captured = []
    monkeypatch.setattr(host_close_trace, 'install', lambda path: captured.append(path))
    runpy.run_path(str(tmp_path / 'host-return-hook' / 'sitecustomize.py'))
    assert sys.orig_argv == argv
    assert captured == ([tmp_path / 'host-return-hook' / 'close-trace.jsonl'] if host else [])


def install_cli(tmp_path, monkeypatch, calls, *, fail_at=None, failure=None, result=0):
    def original(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name == fail_at:
                raise failure
            return result
        return call
    monkeypatch.setattr(ForegroundHost, 'launch', original('launch'))
    monkeypatch.setattr(lifecycle, '_submit', original('submit'))
    monkeypatch.setattr(lifecycle, '_notify_committed', original('notify'))
    monkeypatch.setattr(cli_main, 'main', original('main'))
    path = tmp_path / 'cli.jsonl'
    return path, host_close_trace.install_cli(path)


@pytest.mark.parametrize('result', [0, 7])
def test_cli_records_original_calls_identity_and_no_sensitive_arguments(tmp_path, monkeypatch, result):
    calls, request = [], SimpleNamespace(run_id='run-123', nonce='nonce-456', private='secret-request')
    path, fd = install_cli(tmp_path, monkeypatch, calls, result=result)
    manager, kernel = object(), object()
    try:
        assert not os.get_inheritable(fd)
        assert ForegroundHost.launch(manager, kernel, request, lock='secret-lock', lease=kernel) == result
        assert lifecycle._submit(kernel, request, manager=manager) == result
        assert lifecycle._notify_committed(kernel) == result
        assert cli_main.main(['secret-cli'], repository_routes=kernel) == result
        assert calls[0] == ('launch', (manager, kernel, request), {'lock': 'secret-lock', 'lease': kernel})
        assert calls[1] == ('submit', (kernel, request), {'manager': manager})
        assert calls[2] == ('notify', (kernel,), {})
        assert calls[-1] == ('main', (['secret-cli'],), {'repository_routes': kernel})
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row['stage'] for row in rows] == [
            'foreground_launch_return', 'submit_return', 'notify_enter', 'notify_return', 'cli_return']
        assert rows[0]['run'] == 'run-123' and rows[0]['nonce'] == 'nonce-456'
        assert rows[-1]['result'] == result
        assert all(row['pid'] == os.getpid() and type(row['monotonic_ns']) is int for row in rows)
        assert [row['monotonic_ns'] for row in rows] == sorted(row['monotonic_ns'] for row in rows)
        assert 'secret' not in path.read_text() and path.stat().st_size < 8192
    finally:
        os.close(fd)


@pytest.mark.parametrize('where', ['launch', 'main', 'submit', 'notify'])
def test_cli_business_exception_keeps_original_object_with_benign_diagnostic_failure(tmp_path, monkeypatch, where):
    failure, calls = RuntimeError('secret original failure'), []
    _, fd = install_cli(tmp_path, monkeypatch, calls, fail_at=where, failure=failure)
    def diagnostic(*args):
        raise OSError('diagnostic unavailable')
    monkeypatch.setattr(host_close_trace.os, 'write', diagnostic)
    invoke = {
        'launch': lambda: ForegroundHost.launch(object(), object(), SimpleNamespace(run_id='run', nonce='nonce')),
        'main': lambda: cli_main.main(['secret']),
        'submit': lambda: lifecycle._submit('secret', private='secret'),
        'notify': lambda: lifecycle._notify_committed('secret'),
    }[where]
    try:
        with pytest.raises(RuntimeError) as caught:
            invoke()
        assert caught.value is failure and len(calls) == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize('where,stage', [
    ('launch', 'foreground_launch_return'),
    ('main', 'cli_return'),
    ('submit', 'submit_return'),
    ('notify', 'notify_return'),
])
@pytest.mark.parametrize('error_type', [TimeoutError, InterruptedError])
def test_cli_return_diagnostic_control_error_never_retries_original_call(tmp_path, monkeypatch, where, stage, error_type):
    calls, watchdog = [], error_type('return watchdog')
    _, fd = install_cli(tmp_path, monkeypatch, calls)
    write = os.write
    def diagnostic(descriptor, payload):
        if json.loads(payload)['stage'] == stage:
            raise watchdog
        return write(descriptor, payload)
    monkeypatch.setattr(host_close_trace.os, 'write', diagnostic)
    invoke = {
        'launch': lambda: ForegroundHost.launch(object(), object(), SimpleNamespace(run_id='run', nonce='nonce')),
        'main': lambda: cli_main.main(['secret']),
        'submit': lambda: lifecycle._submit('secret', private='secret'),
        'notify': lambda: lifecycle._notify_committed('secret'),
    }[where]
    try:
        with pytest.raises(error_type) as caught:
            invoke()
        assert caught.value is watchdog and len(calls) == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize('where', ['submit', 'notify'])
@pytest.mark.parametrize('error_type', [OSError, TimeoutError, InterruptedError, KeyboardInterrupt])
def test_new_cli_boundaries_keep_original_call_failure(tmp_path, monkeypatch, where, error_type):
    calls, failure = [], error_type('original call failure')
    path, fd = install_cli(tmp_path, monkeypatch, calls, fail_at=where, failure=failure)
    try:
        with pytest.raises(error_type) as caught:
            getattr(lifecycle, '_submit' if where == 'submit' else '_notify_committed')('private')
        assert caught.value is failure and len(calls) == 1
        stages = [json.loads(line)['stage'] for line in path.read_text().splitlines()]
        assert stages == ([] if where == 'submit' else ['notify_enter'])
    finally:
        os.close(fd)


@pytest.mark.parametrize('source', ['write', 'cpu'])
@pytest.mark.parametrize('error_type', [TimeoutError, InterruptedError])
@pytest.mark.parametrize('stage', ['notify_enter', 'notify_return', 'submit_return'])
def test_new_boundary_probe_failure_propagates_without_fallback(
    tmp_path, monkeypatch, source, error_type, stage,
):
    calls, failure = [], error_type('probe watchdog')
    _, fd = install_cli(tmp_path, monkeypatch, calls)
    write, snapshot = host_close_trace.os.write, host_close_trace.cpu_snapshot
    def failed_write(descriptor, payload):
        if json.loads(payload)['stage'] == stage:
            raise failure
        return write(descriptor, payload)
    def failed_cpu(**kwargs):
        # notify_enter is the first snapshot; notify_return follows one write.
        if stage != 'notify_return' or calls:
            raise failure
        return snapshot(**kwargs)
    monkeypatch.setattr(host_close_trace, 'cpu_snapshot', failed_cpu if source == 'cpu' else snapshot)
    monkeypatch.setattr(host_close_trace.os, 'write', failed_write if source == 'write' else write)
    try:
        with pytest.raises(error_type) as caught:
            getattr(lifecycle, '_submit' if stage == 'submit_return' else '_notify_committed')('private')
        assert caught.value is failure
        assert len(calls) == (0 if stage == 'notify_enter' else 1)
    finally:
        os.close(fd)


@pytest.mark.parametrize('where', ['submit', 'notify'])
@pytest.mark.parametrize('context', ['finally', 'handled-except'])
@pytest.mark.parametrize('new_failure', [None, TimeoutError, InterruptedError])
def test_active_exception_completely_bypasses_new_boundary_probes(
    tmp_path, monkeypatch, where, context, new_failure,
):
    calls, original = [], RuntimeError('original business failure')
    newer = None if new_failure is None else new_failure('new original-call watchdog')
    path, fd = install_cli(tmp_path, monkeypatch, calls,
        fail_at=where if newer is not None else None, failure=newer, result=7)
    probes = []
    def forbidden(*args, **kwargs):
        probes.append(True)
        raise AssertionError('an active exception must bypass all new probes')
    monkeypatch.setattr(host_close_trace, 'cpu_snapshot', forbidden)
    monkeypatch.setattr(host_close_trace.os, 'write', forbidden)
    def invoke():
        call = getattr(lifecycle, '_submit' if where == 'submit' else '_notify_committed')
        if context == 'finally':
            try:
                raise original
            finally:
                call('private', secret='argument')
        else:
            try:
                raise original
            except RuntimeError:
                return call('private', secret='argument')
    try:
        if newer is not None or context == 'finally':
            expected = original if newer is None else newer
            with pytest.raises(type(expected)) as caught:
                invoke()
            assert caught.value is expected
        else:
            assert invoke() == 7
        assert len(calls) == 1 and calls[0][1:] == (('private',), {'secret': 'argument'})
        assert not probes and path.read_bytes() == b''
    finally:
        os.close(fd)
