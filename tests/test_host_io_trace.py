"""Diagnostic entry/recorder contracts only; no Host, child or native clock."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import time

import pytest

from support import host_io_entry as entry
from support.host_io_trace import HostIOTrace, observe_host, read_traces


def recorder(tmp_path):
    root = tmp_path / 'trace'
    root.mkdir(mode=0o700)
    return root, HostIOTrace(root, 'owned-action', 'owned-run', time.monotonic() + 60)


def test_trace_retains_32_events_current_phase_and_one_bounded_stack_without_locals(tmp_path):
    root, trace = recorder(tmp_path)
    private_local = 'PRIVATE_SQL_ARG_ENV_MUST_NOT_APPEAR'
    try:
        for _ in range(40):
            with trace.span('complete'):
                pass
        with trace.span('run_once'):
            with trace.span('adapter.reconcile', effect='effect-one'):
                trace.sample_once()
                sampled = trace.snapshot()['stack_sample']
                trace.sample_once()
                assert trace.snapshot()['stack_sample'] is sampled
                current = list(sampled['current'].values())
                assert len(current) == 1 and [row['phase'] for row in current[0]] == ['run_once', 'adapter.reconcile']
                assert current[0][-1]['effect'] == 'effect-one'
        assert trace.snapshot()['current'] == {} and len(trace.snapshot()['ring']) == 32
    finally:
        trace.close()
    assert not trace.timer.is_alive()
    facts, = read_traces(root, 'owned-run')
    assert len(facts['ring']) == 32 and facts['measurement_errors'] == 0
    assert facts['sample_status'] == 'sampled'
    assert facts['stack_sample']['threads'] and len(facts['stack_sample']['threads']) <= 8
    assert all(len(row['frames']) <= 12 and all(set(frame) == {'file', 'function', 'line'}
               for frame in row['frames']) for row in facts['stack_sample']['threads'])
    assert private_local not in json.dumps(facts) and len(json.dumps(facts).encode()) <= 65536
    with pytest.raises(OSError):
        os.fstat(trace.fd)


def test_measurement_failure_cannot_hide_original_exception(tmp_path, monkeypatch):
    import support.host_io_trace as module
    _root, trace = recorder(tmp_path)
    error = ValueError('Original body failure')
    try:
        def unavailable():
            raise OSError('Diagnostic clock failure')
        with monkeypatch.context() as patch:
            patch.setattr(module.time, 'monotonic_ns', unavailable)
            with pytest.raises(ValueError) as caught:
                with trace.span('run_once'):
                    raise error
        assert caught.value is error and trace.errors > 0
    finally:
        trace.close()


def test_failed_stack_sample_stays_incomplete_without_echoing_error(tmp_path, monkeypatch):
    import support.host_io_trace as module
    root, trace = recorder(tmp_path)
    def unavailable():
        raise ValueError('PRIVATE_STACK_ERROR')
    try:
        monkeypatch.setattr(module.sys, '_current_frames', unavailable)
        trace.sample_once()
        assert trace.snapshot()['sample_status'] == 'sample_incomplete' and trace.errors == 1
    finally:
        trace.close()
    facts, = read_traces(root, 'owned-run')
    assert facts['stack_sample']['complete'] is False and 'PRIVATE_STACK_ERROR' not in json.dumps(facts)


@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'directory', 'foreign'])
def test_trace_reader_rejects_unsafe_or_foreign_file_without_blocking(tmp_path, kind):
    root = tmp_path / 'trace'
    root.mkdir(mode=0o700)
    path = root / 'a.json'
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'directory':
        path.mkdir()
    else:
        target = tmp_path / 'original'
        target.write_text('{"run":"foreign","private":"DO_NOT_ECHO"}')
        target.chmod(0o600)
        if kind == 'symlink':
            path.symlink_to(target)
        else:
            path.write_bytes(target.read_bytes())
            path.chmod(0o600)
    facts = read_traces(root, 'owned-run')
    assert len(facts) == 1 and set(facts[0]) == {'unavailable'}
    assert 'DO_NOT_ECHO' not in json.dumps(facts)


def test_original_calls_keep_args_returns_errors_and_sqlite_commit_close_phases(tmp_path, monkeypatch):
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.runtime.codex import _CodexCapture
    from agent_run.storage.sqlite import Storage
    from agent_run.runtime import git_command
    root, trace = recorder(tmp_path)
    database = tmp_path / 'original.sqlite3'
    calls, sentinel, failure = [], object(), ValueError('Original adapter failure')
    def run_once(self, **kwargs):
        calls.append(kwargs)
        return sentinel
    def execute(self, effect, authority):
        assert effect.effect_id == 'effect-id' and authority is sentinel
        raise failure
    def original_git(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return sentinel
    @contextmanager
    def transaction(self, **kwargs):
        yield None  # Only classify the original _transaction keyword contract.
    try:
        with monkeypatch.context() as patch:
            patch.setattr(OperationRunner, 'run_once', run_once)
            patch.setattr(RemoteEffects, 'execute', execute)
            patch.setattr(_CodexCapture, '_close', lambda self: sentinel)
            patch.setattr(Storage, '_transaction', transaction)
            patch.setattr(git_command, 'run_git', original_git)
            observe_host(patch, trace, database)
            assert OperationRunner.run_once(object(), agent_capacity=0) is sentinel
            with pytest.raises(ValueError) as caught:
                RemoteEffects.execute(object(), SimpleNamespace(effect_id='effect-id', operation='cleanup_worktree'), sentinel)
            assert caught.value is failure
            assert _CodexCapture._close(SimpleNamespace(call=SimpleNamespace(invocation_id='invocation-id'))) is sentinel
            command = ['git', '-C', 'PRIVATE_ARG_MUST_NOT_APPEAR', 'worktree', 'list']
            assert git_command.run_git(command, cwd=tmp_path) is sentinel
            for metadata in (False, True):
                with Storage._transaction(object(), write=True, select_run=not metadata):
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute("SELECT 'PRIVATE_SQL_MUST_NOT_APPEAR'")
                        connection.commit()
                    finally:
                        connection.close()
            assert calls == [{'agent_capacity': 0}, (command, {'cwd': tmp_path})]
    finally:
        trace.close()
    facts, = read_traces(root, 'owned-run')
    phases = {row['phase'] for row in facts['ring']}
    assert {'run_once', 'adapter.execute.cleanup_worktree', 'capture.close', 'git.worktree',
            'sqlite.metadata.commit', 'sqlite.metadata.close', 'sqlite.business.commit', 'sqlite.business.close'} <= phases
    assert any(row['effect'] == 'effect-id' and row['boundary'] == 'raise' for row in facts['ring'])
    assert any(row['invocation'] == 'invocation-id' for row in facts['ring'])
    assert 'PRIVATE_ARG' not in json.dumps(facts) and 'PRIVATE_SQL' not in json.dumps(facts)


@pytest.mark.parametrize('foreign', [False, 'command', 'run', 'backend'])
def test_sealed_entry_checks_exact_command_before_trace_or_executor(tmp_path, monkeypatch, foreign):
    root = tmp_path / 'trace'
    root.mkdir(mode=0o700)
    database, lock, now = tmp_path / 'db', tmp_path / 'runner.lock', time.monotonic() + 60
    selected = entry.command(database, 'action', lock, os.environ, run_id='run', root=root, sample_at=now)
    request = SimpleNamespace(backend='systemd' if foreign == 'backend' else 'foreground',
        command=selected + (('--foreign',) if foreign == 'command' else ()),
        run_id='foreign-run' if foreign == 'run' else 'run')
    monkeypatch.setattr(entry, 'Kernel', lambda *a, **kw: SimpleNamespace(host_request=lambda action: request))
    called = []
    monkeypatch.setattr(entry.executor, 'main', lambda args: called.append(args) or 17)
    args = list(selected[selected.index('--trace-host'):])
    if foreign:
        with pytest.raises(ValueError, match='sealed admission'):
            entry.main(args)
        assert not called and not list(root.iterdir())
    else:
        assert entry.main(args) == 17
        assert called == [['--database', str(database), '--action', 'action', '--runner-lock', str(lock), '--run-id=run']]
        facts, = read_traces(root, 'run')
        assert facts['sample_status'] == 'not_due' and facts['measurement_errors'] == 0


@pytest.mark.parametrize('return_trace', [False, True])
def test_cli_selects_diagnostic_command_before_public_admission_and_restores_factory(tmp_path, monkeypatch, return_trace):
    from agent_run.cli import main as public
    from support import host_close_trace
    installed = []
    monkeypatch.setattr(host_close_trace, 'install_cli', lambda path: installed.append(path))
    if return_trace:
        monkeypatch.setenv('ENTRY_HOST_COST_ROOT', str(tmp_path / 'costs'))
    else:
        monkeypatch.delenv('ENTRY_HOST_COST_ROOT', raising=False)
    root, called, now = tmp_path / 'trace', [], time.monotonic() + 60
    original = entry.executor.command
    def admitted(arguments):
        called.append(arguments)
        selected = entry.executor.command(tmp_path / 'db', 'action', tmp_path / 'lock',
            {'LD_LIBRARY_PATH': '/test/sqlite', 'TOKEN': 'PRIVATE_NOT_IN_COMMAND'}, run_id='-exact-run')
        assert selected[:2] == ('/usr/bin/env', 'LD_LIBRARY_PATH=/test/sqlite')
        assert selected[-1] == '--run-id=-exact-run' and '--trace-host' in selected
        assert selected[selected.index('--trace-root') + 1] == str(root)
        assert selected[selected.index('--sample-at') + 1] == repr(now)
        assert 'PRIVATE_NOT_IN_COMMAND' not in repr(selected)
        assert not root.exists()  # Selecting argv performs no external preparation/write.
        return 19
    monkeypatch.setattr(public, 'main', admitted)
    assert entry.main(['--trace-root', str(root), '--sample-at', repr(now), '--',
                       'resume', '-exact-run', '--foreground']) == 19
    assert called == [['resume', '-exact-run', '--foreground']]
    assert entry.executor.command is original
    assert installed == ([tmp_path / 'costs' / f'cli-{os.getpid()}.jsonl'] if return_trace else [])


@pytest.mark.parametrize('count,diagnostic', [(1, True), (2, True), (4, True), (2, False), (4, False)])
def test_diagnostic_mode_cannot_enter_full_ci_shard_collection(count, diagnostic):
    import conftest
    values = {'host_loss_io_diagnostics': diagnostic, 'test_shard_count': count}
    config = SimpleNamespace(getoption=values.__getitem__)
    if diagnostic and count != 1:
        with pytest.raises(pytest.UsageError, match='cannot produce full CI shard evidence'):
            conftest.pytest_configure(config)
    else:
        conftest.pytest_configure(config)
