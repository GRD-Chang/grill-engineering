from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_PROBE = r'''
import ctypes, os, select, signal, subprocess, sys, threading
from pathlib import Path
from agent_run.github_retry import _run_bounded_command
from agent_run.systemd_executor_host import _run_bounded

assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # Own and reap fixture orphans.
helper, outcome, root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
escaped = sys.argv[4] == 'escaped'
command = r"""
import os, signal, sys
from pathlib import Path
root, outcome = Path(sys.argv[1]), sys.argv[2]
escaped = sys.argv[3] == 'escaped'
ready_read, ready_write = os.pipe()
child = os.fork()
if child == 0:
    if escaped:
        os.setsid()
    os.close(ready_read)
    os.write(ready_write, b'1')
    os.close(ready_write)
    signal.pause()
    os._exit(0)
os.close(ready_write)
assert os.read(ready_read, 1) == b'1'
os.close(ready_read)
(root / 'child.pid').write_text(str(child))
print('normal output', flush=True)
print('error tail', file=sys.stderr, flush=True)
with (root / 'ready.fifo').open('w') as ready:
    ready.write('1')
if outcome == 'normal':
    os._exit(0)
signal.pause()
"""
real_popen = subprocess.Popen
unrelated = real_popen([sys.executable, '-c', 'import signal; signal.pause()'],
                      start_new_session=True)
try:
    for index in range(2):
        sample = root / str(index)
        sample.mkdir()
        os.mkfifo(sample / 'ready.fifo')
        baseline_threads = set(threading.enumerate())
        baseline_fds = set(os.listdir('/proc/self/fd'))
        spawned = []
        original_start = threading.Thread.start
        starts = 0
        def start(reader):
            global starts
            starts += 1
            if starts == 2:
                raise RuntimeError("can't start new thread")
            return original_start(reader)
        if outcome == 'reader_start_failure':
            threading.Thread.start = start
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            # Fixture startup is not output-reader cleanup time. Wait for the
            # real descendant/output boundary before arming the rescue guard.
            ready = os.open(sample / 'ready.fifo', os.O_RDONLY | os.O_NONBLOCK)
            try:
                assert select.select([ready], [], [], 5)[0], 'fixture did not become ready'
                assert os.read(ready, 1) == b'1'
            finally:
                os.close(ready)
            original_start(guard)
            if outcome in {'timeout', 'exception'}:
                original_wait = process.wait
                injected = False
                def wait(timeout=None):
                    nonlocal injected
                    if not injected:
                        injected = True
                        if outcome == 'timeout':
                            raise subprocess.TimeoutExpired(args[0], timeout)
                        raise RuntimeError('injected wait failure')
                    return original_wait(timeout=timeout)
                process.wait = wait
            return process
        subprocess.Popen = spawn
        child = None
        reaped = False
        guard_fired = threading.Event()
        def rescue():
            guard_fired.set()
            if (sample / 'child.pid').exists():
                os.kill(int((sample / 'child.pid').read_text()), signal.SIGKILL)
        guard = threading.Timer(3, rescue)
        try:
            arguments = [sys.executable, '-c', command, str(sample), outcome, 'escaped' if escaped else 'owned']
            try:
                result = (_run_bounded_command(arguments, cwd=sample, timeout=30)
                          if helper == 'github' else _run_bounded(arguments, max_output=1024))
            except subprocess.TimeoutExpired as error:
                assert helper == 'github' and outcome == 'timeout'
                assert b'normal output' in error.stdout and b'error tail' in error.stderr
            except RuntimeError as error:
                assert outcome in {'exception', 'reader_start_failure'}
                assert str(error) == ("can't start new thread" if outcome == 'reader_start_failure'
                                      else 'injected wait failure')
            else:
                assert outcome not in {'exception', 'reader_start_failure'}
                assert result.stdout == 'normal output\n'
                assert 'error tail' in result.stderr
                if outcome == 'normal':
                    assert result.returncode == 0
                else:
                    assert result.returncode != 0 and 'systemd helper timed out' in result.stderr
            guard.cancel()
            guard.join()
            assert not guard_fired.is_set(), 'output readers needed external rescue'
            child = int((sample / 'child.pid').read_text())
            assert set(threading.enumerate()) == baseline_threads, 'reader threads survived return'
            assert set(os.listdir('/proc/self/fd')) == baseline_fds, 'file descriptors leaked'
            with os.fdopen(os.pidfd_open(child), 'rb') as pidfd:
                if escaped:
                    # Escaped ownership is not guessed; only the fixture owns this PID.
                    assert not select.select([pidfd], [], [], 0)[0]
                    signal.pidfd_send_signal(pidfd.fileno(), signal.SIGKILL)
                else:
                    assert select.select([pidfd], [], [], 2)[0] == [pidfd], 'descendant survived return'
            assert os.waitpid(child, 0)[0] == child
            reaped = True
            child = None
            assert unrelated.poll() is None, 'unrelated process group was killed'
        finally:
            guard.cancel()
            if guard.ident is not None:
                guard.join()
            subprocess.Popen = real_popen
            threading.Thread.start = original_start
            for process in spawned:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if not reaped and child is None and (sample / 'child.pid').exists():
                child = int((sample / 'child.pid').read_text())
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(child, 0)
                except ChildProcessError:
                    pass
    print('two calls: no reader, fd, or descendant accumulation; unrelated group intact')
finally:
    unrelated.kill()
    unrelated.wait()
'''


@pytest.mark.skipif(not hasattr(os, 'pidfd_open'), reason='requires Linux pidfd')
@pytest.mark.parametrize('escape', ['owned', 'escaped'])
@pytest.mark.parametrize('helper', ['github', 'systemd'])
@pytest.mark.parametrize('outcome', ['normal', 'timeout', 'exception', 'reader_start_failure'])
def test_bounded_commands_reap_descendants_and_readers(
    tmp_path: Path, helper: str, outcome: str, escape: str,
) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'TMPDIR'):
        directory = tmp_path / key.lower()
        directory.mkdir()
        environment[key] = str(directory)
    result = subprocess.run(
        [sys.executable, '-c', _PROBE, helper, outcome, str(tmp_path), escape],
        env=environment, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no reader, fd, or descendant accumulation' in result.stdout
