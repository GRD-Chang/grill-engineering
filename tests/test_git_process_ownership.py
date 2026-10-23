"""Git owns its child until the single group cleanup and final reap."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.errors import GitError
from agent_run.runtime import git_command


@pytest.mark.parametrize("wait_failure", [None, "before_reap", "after_reap"])
def test_git_group_signal_precedes_only_reap_and_wait_errors_close_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_failure: str | None,
) -> None:
    streams = [(tmp_path / name).open("w+b") for name in ("stdin", "stdout", "stderr")]
    descriptors = [stream.fileno() for stream in streams]
    reaped = False
    waits = 0
    signals = []

    def waitpid(pid, options):
        nonlocal reaped, waits
        assert pid == 900000001 and options == 0
        waits += 1
        if reaped:
            raise ChildProcessError("injected already reaped")
        if waits == 1 and wait_failure == "before_reap":
            raise OSError("injected wait failure")
        reaped = True
        if waits == 1 and wait_failure == "after_reap":
            raise OSError("injected wait failure")
        return pid, 7 << 8

    def waitid(kind, pid, options):
        assert kind == os.P_PID and pid == 900000001
        assert options & os.WNOWAIT
        if reaped:
            raise ChildProcessError("injected already reaped")
        return SimpleNamespace(si_pid=pid)

    def forbidden_poll():
        pytest.fail("Git or pump consumed the leader through poll")

    process = SimpleNamespace(
        pid=900000001, stdin=streams[0], stdout=streams[1], stderr=streams[2],
        poll=forbidden_poll, wait=lambda **kw: waitpid(900000001, 0)[1] >> 8,
        returncode=None,
    )

    def pipe_boundary(process, *, check, stdout, stderr, **kwargs):
        check()
        check()
        stdout.feed(b"complete output\n")
        stderr.feed(b"diagnostic\n")

    monkeypatch.setattr(git_command, "_require_waitable_children", lambda: None, raising=False)
    monkeypatch.setattr(git_command.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(git_command, "pump", pipe_boundary)
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "waitpid", waitpid)
    # Numeric fixture identity is never signalled on the actual OS.
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, reaped)))
    try:
        if wait_failure:
            with pytest.raises(OSError, match="injected wait failure"):
                git_command.run_git(["git", "--version"], cwd=tmp_path, input="input")
        else:
            result = git_command.run_git(["git", "--version"], cwd=tmp_path, input="input")
            assert (result.returncode, result.stdout, result.stderr) == (
                7, "complete output\n", "diagnostic\n",
            )
        assert signals == [(900000001, False)]
        assert waits == (2 if wait_failure else 1)
        assert all(stream.closed for stream in streams)
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for stream in streams:
            stream.close()


@pytest.mark.parametrize("phase", ["observe", "reap"])
def test_git_lost_child_ownership_never_signals_after_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    streams = [(tmp_path / name).open("w+b") for name in ("stdout", "stderr")]
    signals = []
    process = SimpleNamespace(
        pid=900000001, stdin=None, stdout=streams[0], stderr=streams[1], returncode=None,
        poll=lambda: 0, wait=lambda **kw: 0,
    )

    def lost_child(*args):
        raise ChildProcessError("injected ECHILD")

    monkeypatch.setattr(git_command, "_require_waitable_children", lambda: None, raising=False)
    monkeypatch.setattr(git_command.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(git_command, "pump", lambda *a, **kw: kw["check"]())
    monkeypatch.setattr(
        os, "waitid", lost_child if phase == "observe" else lambda *a: SimpleNamespace(si_pid=process.pid),
    )
    monkeypatch.setattr(os, "waitpid", lost_child)
    monkeypatch.setattr(os, "killpg", lambda *a: signals.append(a))
    try:
        with pytest.raises(GitError, match="child ownership lost"):
            git_command.run_git(["git", "--version"], cwd=tmp_path)
        assert len(signals) == (0 if phase == "observe" else 1)
        assert all(stream.closed for stream in streams)
    finally:
        for stream in streams:
            stream.close()


@pytest.fixture(scope="module")
def sigchld_policy_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Use the platform's signal.h, never a guessed ctypes sigaction layout."""
    import subprocess

    directory = tmp_path_factory.mktemp("sigchld-policy")
    source = directory / "policy.c"
    library = directory / "policy.so"
    source.write_text(
        "#include <signal.h>\n"
        "static void handler(int signo) {}\n"
        "int set_policy(int caught) {\n"
        " struct sigaction action = {0};\n"
        " sigemptyset(&action.sa_mask);\n"
        " action.sa_handler = caught ? handler : SIG_DFL;\n"
        " action.sa_flags = caught ? 0 : SA_NOCLDWAIT;\n"
        " return sigaction(SIGCHLD, &action, 0);\n"
        "}\n",
        encoding="ascii",
    )
    built = subprocess.run(
        ["cc", "-shared", "-fPIC", str(source), "-o", str(library)],
        capture_output=True, text=True, timeout=10,
    )
    assert built.returncode == 0, built.stderr
    return library


@pytest.mark.parametrize("policy", ["ignored", "no_cldwait", "python_handler", "c_handler"])
def test_git_rejects_non_waitable_sigchld_policy_before_git_spawn(
    tmp_path: Path, sigchld_policy_library: Path, policy: str,
) -> None:
    import subprocess
    import sys

    # Signal policy changes are confined to this disposable child, not pytest
    # or any user process. Admission may spawn its /bin/true waitability probe.
    program = r'''
import ctypes, signal, subprocess, sys
from pathlib import Path
from agent_run.errors import GitError
from agent_run.runtime.git_command import run_git
policy, library, root = sys.argv[1:]
if policy == 'ignored':
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
elif policy == 'python_handler':
    signal.signal(signal.SIGCHLD, lambda *args: None)
else:
    configure = ctypes.CDLL(library).set_policy
    configure.argtypes = [ctypes.c_int]
    configure.restype = ctypes.c_int
    assert configure(policy == 'c_handler') == 0
    # A C sigaction leaves Python's cached disposition looking like SIG_DFL.
    assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL

def forbidden_spawn(*args, **kwargs):
    raise AssertionError('Git was spawned under an unsupported SIGCHLD policy')
subprocess.Popen = forbidden_spawn
try:
    run_git(['git', '--version'], cwd=Path(root))
except GitError as error:
    assert 'waitable children' in str(error), str(error)
else:
    raise AssertionError('unsupported signal policy was accepted')
'''
    result = subprocess.run(
        [sys.executable, "-c", program, policy, str(sigchld_policy_library), str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_concurrent_git_calls_own_reaping_without_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import threading

    barrier = threading.Barrier(2)
    processes = []
    signalled = []
    reaped = []
    real_spawn, real_signal, real_wait = subprocess.Popen, os.killpg, os.waitpid

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        processes.append(process)

        def forbidden_poll():
            raise AssertionError("Git or pump called poll")

        process.poll = forbidden_poll
        barrier.wait(timeout=5)
        return process

    def signal_group(pid, sig):
        assert pid in {process.pid for process in processes}
        # Successful WNOWAIT confirms ownership at the signal boundary. It
        # neither reaps the leader nor infers ownership from an alive PGID.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        signalled.append(pid)
        real_signal(pid, sig)

    def waitpid(pid, options):
        result = real_wait(pid, options)
        if pid in {process.pid for process in processes}:
            reaped.append(pid)
        return result

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(os, "waitpid", waitpid)
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(git_command.run_git, ["git", "--version"], cwd=tmp_path)
                 for _ in range(2)]
        results = [call.result(timeout=10) for call in calls]
    assert all(result.returncode == 0 and result.stdout.startswith("git version ")
               for result in results)
    assert len(processes) == 2
    assert sorted(signalled) == sorted(reaped) == sorted(process.pid for process in processes)
    assert all(process.stdout.closed and process.stderr.closed for process in processes)
    for process in processes:
        with pytest.raises(ChildProcessError):
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def test_git_cleans_same_group_pipe_holder_after_leader_exits(tmp_path: Path) -> None:
    import subprocess
    import sys

    # The disposable parent owns fixture orphans so it can verify their real
    # exit and reap them, without installing a handler in pytest/the host.
    program = r'''
import ctypes, os, select, signal, subprocess, sys
from pathlib import Path
from agent_run.runtime.git_command import run_git
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
root = Path(sys.argv[1])
helper = root / 'git'
helper.write_text('#!' + sys.executable + '\n' + r"""
import os, signal
from pathlib import Path
ready, notify = os.pipe()
child = os.fork()
if child == 0:
    os.close(ready)
    os.write(notify, b'1')
    os.close(notify)
    signal.pause()
    os._exit(0)
os.close(notify)
assert os.read(ready, 1) == b'1'
os.close(ready)
Path('holder.pid').write_text(str(child))
os.write(1, b'complete output\n')
os.write(2, b'complete diagnostic\n')
os._exit(7)
""")
helper.chmod(0o700)
environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
try:
    result = run_git(['git', '--version'], cwd=root, environment=environment, timeout=2)
    assert result.returncode == 7, result
    assert result.stdout == 'complete output\n'
    assert result.stderr == 'complete diagnostic\n'
    child = int((root / 'holder.pid').read_text())
    with os.fdopen(os.pidfd_open(child), 'rb') as owned:
        assert select.select([owned], [], [], 1)[0], 'same-group hook survived'
    assert os.waitpid(child, 0)[0] == child
    (root / 'holder.pid').unlink()
finally:
    if (root / 'holder.pid').exists():
        child = int((root / 'holder.pid').read_text())
        # Fixture owns this unreaped adopted child, not an arbitrary service.
        os.waitid(os.P_PID, child, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
'''
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("timeout", [None, 1])
def test_git_waits_for_live_leader_after_pipe_eof(tmp_path: Path, timeout: float | None) -> None:
    import sys

    helper = tmp_path / "git"
    helper.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        "os.close(1)\nos.close(2)\ntime.sleep(0.05)\n"
        "open('finished', 'w').close()\nos._exit(7)\n",
    )
    helper.chmod(0o700)
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = git_command.run_git(["git", "--version"], cwd=tmp_path,
                                 environment=environment, timeout=timeout)
    assert result.returncode == 7
    assert result.stdout == result.stderr == ""
    assert (tmp_path / "finished").is_file()
