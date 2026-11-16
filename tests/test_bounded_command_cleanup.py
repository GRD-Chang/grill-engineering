from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('wait_fails', [False, True])
def test_capture_signals_group_once_even_when_wait_fails(
    monkeypatch: pytest.MonkeyPatch, wait_fails: bool,
) -> None:
    from agent_run.runtime.command import capture_command

    process = subprocess.Popen(
        [sys.executable, '-c', "print('completed', flush=True)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    process.wait()  # Ordinary completed child; no PID reuse is constructed.
    signals = []
    monkeypatch.setattr(os, 'killpg', lambda *args: signals.append(args))
    original_wait = process.wait
    waits = 0

    def wait(timeout=None):
        nonlocal waits
        waits += 1
        if wait_fails and waits == 1:
            raise RuntimeError('injected wait failure after group signal')
        return original_wait(timeout=timeout)

    monkeypatch.setattr(process, 'wait', wait)
    if wait_fails:
        with pytest.raises(RuntimeError, match='wait failure after group signal'):
            capture_command(process, timeout=1, max_output=128)
    else:
        result = capture_command(process, timeout=1, max_output=128)
        assert result.returncode == 0 and result.stdout == b'completed\n'
    assert len(signals) == 1
    assert process.stdout.closed and process.stderr.closed


_PROBE = """
import ctypes, os, select, signal, subprocess, sys, threading, selectors
from pathlib import Path
from agent_run.adapters.github_commands import _run_bounded_command
from agent_run.adapters import github_commands
from agent_run.adapters.github import GhGitHubReader
from agent_run.runtime.git_command import run_git
from agent_run.adapters.systemd import HostUnavailable, SystemdHost

assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # Own and reap fixture orphans.
helper, outcome, root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
escaped = sys.argv[4] == 'escaped'
command = r\"\"\"
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
\"\"\"
if helper == 'systemd' and outcome == 'timeout':
    from agent_run.adapters import systemd
    capture = systemd.capture_command
    def short_capture(process, *, timeout, max_output):
        assert timeout == 5
        return capture(process, timeout=0.05, max_output=max_output)
    systemd.capture_command = short_capture
if helper == 'github_read':
    command = command.replace("print('normal output', flush=True)", "print('{}', flush=True)")
def structured_read(arguments, sample):
    bounded = github_commands._run_bounded_command
    def invoke(_arguments, **options):
        options['cwd'] = sample
        return bounded(arguments, **options)
    github_commands._run_bounded_command = invoke
    try:
        return GhGitHubReader('example/project')._gh_json('api', 'graphql')
    finally:
        github_commands._run_bounded_command = bounded
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
        original_selector = selectors.DefaultSelector
        def failed_selector():
            raise RuntimeError("can't create pipe selector")
        if outcome == 'capture_setup_failure':
            selectors.DefaultSelector = failed_selector
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
            if outcome == 'exception':
                original_poll = process.poll
                injected = False
                def poll():
                    nonlocal injected
                    if not injected:
                        injected = True
                        raise RuntimeError('injected poll failure')
                    return original_poll()
                process.poll = poll
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
            if helper == 'git':
                program = sample / 'pipe_holder.py'
                program.write_text(command)
                arguments = ['git', '-c', 'alias.holder=!' + sys.executable + ' ' + str(program),
                             'holder', str(sample), outcome, 'owned']
            try:
                result = (run_git(arguments, cwd=sample, timeout=5) if helper == 'git' else
                          structured_read(arguments, sample) if helper == 'github_read' else
                          _run_bounded_command(arguments, cwd=sample, timeout=0.05 if outcome == 'timeout' else 30)
                          if helper == 'github' else SystemdHost(
                              systemctl='unused-fixture', systemd_run='unused-fixture',
                              environment=os.environ)._run(arguments))
            except subprocess.TimeoutExpired as error:
                assert helper == 'github' and outcome == 'timeout'
                assert isinstance(error.stdout, str) and isinstance(error.stderr, str)
                assert 'normal output' in error.stdout and 'error tail' in error.stderr
            except OSError as error:
                assert helper == 'github_read' and escaped and outcome == 'normal'
                assert 'EOF' in str(error)
            except RuntimeError as error:
                if isinstance(error, HostUnavailable):
                    assert helper == 'systemd' and outcome == 'timeout'
                    assert str(error) == 'Bounded user systemd observation was unavailable'
                else:
                    assert outcome in {'exception', 'capture_setup_failure'}
                    assert str(error) == ("can't create pipe selector" if outcome == 'capture_setup_failure'
                                          else 'injected poll failure')
            else:
                assert helper != 'github_read', 'A valid JSON prefix without stdout EOF was accepted'
                assert outcome not in {'exception', 'capture_setup_failure'}
                stdout = result.stdout.decode() if helper == 'systemd' else result.stdout
                stderr = result.stderr.decode() if helper == 'systemd' else result.stderr
                assert stdout == 'normal output\\n'
                assert 'error tail' in stderr
                assert outcome == 'normal'
                assert result.returncode == 0
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
            selectors.DefaultSelector = original_selector
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
"""


@pytest.mark.skipif(not hasattr(os, 'pidfd_open'), reason='requires Linux pidfd')
@pytest.mark.parametrize('escape', ['owned', 'escaped'])
@pytest.mark.parametrize('helper', ['github', 'systemd'])
@pytest.mark.parametrize('outcome', ['normal', 'timeout', 'exception', 'capture_setup_failure'])
def test_bounded_commands_reap_descendants_and_readers(
    tmp_path: Path, helper: str, outcome: str, escape: str,
) -> None:
    _check_cleanup(tmp_path, helper, outcome, escape)


@pytest.mark.skipif(not hasattr(os, 'pidfd_open'), reason='requires Linux pidfd')
def test_git_reaps_pipe_holders_after_leader_exit(tmp_path: Path) -> None:
    _check_cleanup(tmp_path, 'git', 'normal', 'owned')


@pytest.mark.skipif(not hasattr(os, 'pidfd_open'), reason='requires Linux pidfd')
def test_structured_read_requires_eof_from_an_escaped_pipe_holder(tmp_path: Path) -> None:
    _check_cleanup(tmp_path, 'github_read', 'normal', 'escaped')


def _check_cleanup(tmp_path: Path, helper: str, outcome: str, escape: str) -> None:
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
