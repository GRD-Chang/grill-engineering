"""Diagnostic non-interference; no native Host or business completion proof."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.model import Step
from agent_run.notifications.sender import NativeNotifications
from support import host_close_trace
from support.systemd_manager import _hold_host_operation_return


def install(tmp_path, monkeypatch, *, run=None, close=None):
    monkeypatch.delenv('ENTRY_HOST_SNAPSHOT_AT', raising=False)
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
    (hook / 'stack.txt').write_text('  File "/private/runtime/executor.py", line 150 in run\n')
    if not missing:
        (hook / 'close-trace.jsonl').write_text('x' * 10000)
    original = AssertionError('original deadline failure')
    with pytest.raises(AssertionError) as caught:
        host_close_trace.raise_with_trace(original, tmp_path, [SimpleNamespace(poll=lambda: 0)])
    assert caught.value is original
    facts = json.loads(original.__notes__[-1].partition(': ')[2])
    assert facts['child_poll'] == [0]
    assert facts['stack'] == {'frames': ['executor.py:150 in run'], 'truncated': False}
    assert (facts.get('trace_unavailable') == 'FileNotFoundError') if missing else len(facts['trace']) == 4096


def test_failure_note_watchdog_rethrows_original_without_continuing(tmp_path, monkeypatch):
    original, watchdog = AssertionError('body'), TimeoutError('watchdog')
    def fail(*args, **kwargs):
        raise watchdog
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(AssertionError) as caught:
        host_close_trace.raise_with_trace(original, tmp_path, [])
    assert caught.value is original


@pytest.mark.parametrize('failure', [RuntimeError('note unavailable'), TimeoutError('note watchdog')])
def test_abandon_prefix_note_failure_still_rethrows_original_assertion(tmp_path, monkeypatch, failure):
    # Exercise the actual story's prefix failure seam only. No public process,
    # storage, manager, transport or execution evidence is supplied by these stubs.
    path = Path(__file__).with_name('test_kernel_public_abandon_completed_ticket.py')
    story = runpy.run_path(str(path))[
        'test_public_abandon_recovers_its_completed_ticket_and_closes_unapproved_final_pr']
    scope, seen, releases = story.__globals__, [], []
    release = SimpleNamespace(set=lambda: releases.append('released'))
    child = SimpleNamespace(poll=lambda: 0)
    kernel = SimpleNamespace(storage=SimpleNamespace(path=tmp_path / 'unopened.sqlite3', run_id='original'),
                             source_status=lambda: {'prefix': 'controlled failure'})
    environment = {'PATH': os.defpath, 'ENTRY_APPROVAL_CONTROL': '@unused'}

    def note(error, **options):
        seen.append((error, options['stage']))
        raise failure

    replacements = {
        '_approval_read_barrier': lambda: nullcontext(('@unused', [], SimpleNamespace(wait=lambda _: False), release)),
        '_hold_host_operation_return': lambda *args, **options: None,
        '_control_environment': lambda *args, **options: (environment, tmp_path / 'unopened.json'),
        'install_currentness': lambda *args: None,
        '_git': lambda *args: 'unexecuted',
        '_local_manager': lambda *args, **options: nullcontext((
            SimpleNamespace(systemctl=tmp_path / 'systemctl'), SimpleNamespace(is_alive=lambda: False),
            [], [child], [], [('--database', str(kernel.storage.path), '--run-id=original')], [(b'', b'')])),
        'Kernel': lambda *args, **options: kernel,
        'subprocess': SimpleNamespace(run=lambda *args, **options: SimpleNamespace(
            returncode=0, stdout=json.dumps({'result': 'accepted', 'action_id': 'unexecuted'}), stderr='')),
        '_managed_failure_note': note,
    }
    for name, value in replacements.items():
        monkeypatch.setitem(scope, name, value)
    with pytest.raises(AssertionError) as caught:
        story(tmp_path, tmp_path, monkeypatch)
    assert len(seen) == 1 and caught.value is seen[0][0]
    assert seen[0][1] == 'abandon-before-first-final-approval'
    assert releases == ['released']
    facts = json.loads(caught.value.__notes__[-1].partition(': ')[2])
    assert facts['child_poll'] == [0] and facts['trace_unavailable'] == 'FileNotFoundError'


@pytest.mark.parametrize('fault', [OSError('trace open'), TimeoutError('original startup watchdog')])
def test_install_io_failure_cannot_hide_startup_watchdog(tmp_path, monkeypatch, fault):
    def fail(*args, **kwargs):
        raise fault
    monkeypatch.setattr(host_close_trace.os, 'open', fail)
    if isinstance(fault, TimeoutError):
        with pytest.raises(TimeoutError) as caught:
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


@pytest.mark.parametrize('at,delay', [('108.0', 7.5), ('99.0', 0.001)])
def test_snapshot_is_one_non_exiting_dump_from_original_absolute_time(tmp_path, monkeypatch, at, delay):
    monkeypatch.setattr(host_close_trace.time, 'monotonic', lambda: 100.5)
    calls = []
    monkeypatch.setattr(host_close_trace.faulthandler, 'dump_traceback_later',
        lambda *args, **kwargs: calls.append((args, kwargs)))
    path = tmp_path / 'stack.txt'
    fd = host_close_trace.arm_snapshot(path, at)
    try:
        assert calls == [((delay,), dict(repeat=False, file=fd, exit=False))]
        assert not os.get_inheritable(fd)
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.close(fd)  # No real timer was installed in this parameter test.


@pytest.mark.parametrize('at', [None, 'bad', 'nan', 'inf'])
def test_missing_or_invalid_snapshot_time_does_not_arm(tmp_path, monkeypatch, at):
    monkeypatch.setattr(host_close_trace.faulthandler, 'dump_traceback_later',
        lambda *args, **kwargs: pytest.fail('invalid observation time armed a timer'))
    assert host_close_trace.arm_snapshot(tmp_path / 'stack.txt', at) == -1
    assert not (tmp_path / 'stack.txt').exists()


def test_snapshot_arm_watchdog_is_same_object_and_descriptor_is_not_reused(tmp_path, monkeypatch):
    watchdog, descriptors = TimeoutError('original watchdog'), []
    def fail(*args, **kwargs):
        descriptors.append(kwargs['file'])
        raise watchdog
    monkeypatch.setattr(host_close_trace.faulthandler, 'dump_traceback_later', fail)
    try:
        with pytest.raises(TimeoutError) as caught:
            host_close_trace.arm_snapshot(tmp_path / 'stack.txt', '108.0')
        assert caught.value is watchdog
        assert not os.get_inheritable(descriptors[0])
    finally:
        for fd in descriptors:
            os.close(fd)  # The fake armer cannot still own a C watchdog.


def test_snapshot_reports_real_c_frame_format_without_paths_or_arbitrary_text(tmp_path):
    path = tmp_path / 'stack.txt'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        host_close_trace.faulthandler.dump_traceback(file=fd, all_threads=False)
    finally:
        os.close(fd)
    facts = host_close_trace.snapshot_frames(path)
    assert any('test_snapshot_reports_real_c_frame_format' in row for row in facts['frames'])
    assert all('/' not in row and '0x' not in row for row in facts['frames'])
    path.write_text('Thread 0x123 (most recent call first):\n'
        '  File "/private/token/remote.py", line 87 in _read\n'
        'Source body and PRIVATE_TOKEN=value\n' + 'z' * 40000)
    assert host_close_trace.snapshot_frames(path) == {
        'frames': ['Thread', 'remote.py:87 in _read'], 'truncated': True}
