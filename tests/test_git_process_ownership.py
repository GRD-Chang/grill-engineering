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
    handle = os.open(os.devnull, os.O_RDONLY)  # Mock identity, not a live PID.
    descriptors = [stream.fileno() for stream in streams] + [handle]
    reaped = False
    waits = 0
    signals = []

    def waitid(kind, identity, options):
        nonlocal reaped, waits
        assert kind == os.P_PIDFD and identity == handle
        if not options & os.WNOWAIT:
            waits += 1
        if reaped:
            raise ChildProcessError("injected already reaped")
        if not options & os.WNOWAIT:
            if waits == 1 and wait_failure == "before_reap":
                raise OSError("injected wait failure")
            reaped = True
            if waits == 1 and wait_failure == "after_reap":
                raise OSError("injected wait failure")
        return SimpleNamespace(si_pid=900000001, si_code=os.CLD_EXITED, si_status=7)

    def forbidden_poll():
        pytest.fail("Git or pump consumed the leader through poll")

    process = SimpleNamespace(
        pid=900000001, stdin=streams[0], stdout=streams[1], stderr=streams[2],
        poll=forbidden_poll, returncode=None,
    )

    def pipe_boundary(process, *, check, stdout, stderr, **kwargs):
        check()
        check()
        stdout.feed(b"complete output\n")
        stderr.feed(b"diagnostic\n")

    monkeypatch.setattr(git_command, "_require_waitable_children", lambda: None)
    monkeypatch.setattr(git_command.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(git_command, "pump", pipe_boundary)
    monkeypatch.setattr(os, "pidfd_open", lambda *a: handle)
    monkeypatch.setattr(os, "waitid", waitid)
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
        try:
            os.close(handle)
        except OSError:
            pass


@pytest.mark.parametrize("phase", ["observe", "reap"])
def test_git_lost_child_ownership_never_signals_after_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    streams = [(tmp_path / name).open("w+b") for name in ("stdout", "stderr")]
    handle = os.open(os.devnull, os.O_RDONLY)
    signals = []
    process = SimpleNamespace(
        pid=900000001, stdin=None, stdout=streams[0], stderr=streams[1], returncode=None,
    )

    def waitid(kind, identity, options):
        assert kind == os.P_PIDFD and identity == handle
        if phase == "observe" or not options & os.WNOWAIT:
            raise ChildProcessError("injected ECHILD")
        return SimpleNamespace(si_pid=process.pid)

    monkeypatch.setattr(git_command, "_require_waitable_children", lambda: None)
    monkeypatch.setattr(git_command.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(git_command, "pump", lambda *a, **kw: kw["check"]())
    monkeypatch.setattr(os, "pidfd_open", lambda *a: handle)
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "killpg", lambda *a: signals.append(a))
    try:
        with pytest.raises(GitError, match="child ownership lost"):
            git_command.run_git(["git", "--version"], cwd=tmp_path)
        assert len(signals) == (0 if phase == "observe" else 1)
        assert all(stream.closed for stream in streams)
        assert process._child_created is False
        with pytest.raises(OSError):
            os.fstat(handle)
    finally:
        for stream in streams:
            stream.close()
        try:
            os.close(handle)
        except OSError:
            pass


@pytest.fixture(scope="module")
def sigchld_policy_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Use the platform's signal.h, never a guessed ctypes sigaction layout."""
    import subprocess

    directory = tmp_path_factory.mktemp("sigchld-policy")
    source = directory / "policy.c"
    library = directory / "policy.so"
    source.write_text(
        "#include <signal.h>\n"
        "#include <stddef.h>\n"
        "int action_size(void) { return sizeof(struct sigaction); }\n"
        "int flags_offset(void) { return offsetof(struct sigaction, sa_flags); }\n"
        "int mask_offset(void) { return offsetof(struct sigaction, sa_mask); }\n"
        "int no_cldwait(void) { return SA_NOCLDWAIT; }\n"
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
from agent_run.runtime.git_command import run_git, _require_waitable_children
_require_waitable_children()  # Warm the ABI binding before the C/Python policy change.
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


def test_native_signal_layout_matches_platform_headers(sigchld_policy_library: Path) -> None:
    import ctypes
    from agent_run.runtime import native_child_policy as policy

    if policy._libc() is None:
        assert policy.require_native_waitable_children() is False
        return
    compiled = ctypes.CDLL(str(sigchld_policy_library))
    assert ctypes.sizeof(policy._Sigaction) == compiled.action_size()
    assert policy._Sigaction.flags.offset == compiled.flags_offset()
    assert policy._Sigaction.mask.offset == compiled.mask_offset()
    assert compiled.no_cldwait() == 2


@pytest.mark.parametrize("failure", ["open", "wait", "return", "close", "wait_and_close"])
def test_native_capability_failure_prevents_git_and_closes_self_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import errno
    from agent_run.runtime import native_child_policy as policy

    if policy._libc() is None:
        assert policy.require_native_waitable_children() is False
        return
    opened, closed = [], []
    real_open, real_close = os.pidfd_open, os.close

    def acquire(pid):
        assert pid == os.getpid()
        if failure == "open":
            raise OSError(errno.EPERM, "pidfd denied")
        descriptor = real_open(pid)
        opened.append(descriptor)
        return descriptor

    def wait(kind, descriptor, options):
        assert kind == os.P_PIDFD and descriptor == opened[0]
        assert options == os.WEXITED | os.WNOWAIT | os.WNOHANG
        if failure == "return":
            return None
        code = errno.EPERM if failure in {"wait", "wait_and_close"} else errno.ECHILD
        raise OSError(code, "wait result")

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)
        if failure in {"close", "wait_and_close"}:
            raise OSError(errno.EIO, "uncertain close")

    def forbidden(*args, **kwargs):
        pytest.fail("capability failure must not start a child")

    monkeypatch.setattr(os, "pidfd_open", acquire)
    monkeypatch.setattr(os, "waitid", wait)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "posix_spawn", forbidden)
    monkeypatch.setattr(git_command.subprocess, "Popen", forbidden)
    with pytest.raises(GitError, match="waitable children") as raised:
        git_command.run_git(["git", "--version"], cwd=tmp_path)
    assert closed == opened  # Exactly one close, including uncertain close.
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    if failure == "wait_and_close":
        assert raised.value.__cause__.errno == errno.EPERM
        assert raised.value.__cause__.__cause__.errno == errno.EIO


def test_native_admission_checks_each_call_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.runtime import native_child_policy as policy

    if policy._libc() is None:
        assert policy.require_native_waitable_children() is False
        return
    checked = []
    real_wait = os.waitid

    def wait(*args):
        checked.append(args)
        return real_wait(*args)

    def forbidden(*args, **kwargs):
        pytest.fail("native admission created a process")

    monkeypatch.setattr(os, "waitid", wait)
    monkeypatch.setattr(os, "posix_spawn", forbidden)
    git_command._require_waitable_children()
    git_command._require_waitable_children()
    assert len(checked) == 2


def test_native_signal_query_error_does_not_fall_back_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import errno
    from agent_run.runtime import native_child_policy as policy

    def query(*args):
        ctypes.set_errno(errno.EPERM)
        return -1

    def forbidden(*args, **kwargs):
        pytest.fail("unknown live signal policy must not create a child")

    monkeypatch.setattr(policy, "_libc", lambda: SimpleNamespace(sigaction=query))
    monkeypatch.setattr(os, "posix_spawn", forbidden)
    monkeypatch.setattr(git_command.subprocess, "Popen", forbidden)
    with pytest.raises(GitError, match="waitable children"):
        git_command.run_git(["git", "--version"], cwd=tmp_path)


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
    real_spawn, real_signal = subprocess.Popen, os.killpg
    real_open, real_wait = os.pidfd_open, os.waitid
    handles = {}

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

    def pidfd_open(pid, flags=0):
        fd = real_open(pid, flags)
        handles[fd] = pid
        return fd

    def waitid(kind, identity, options):
        result = real_wait(kind, identity, options)
        if not options & os.WNOWAIT:
            assert kind == os.P_PIDFD
            assert result.si_pid == handles[identity]
            if result.si_pid in {process.pid for process in processes}:
                reaped.append(result.si_pid)
        return result

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(os, "waitid", waitid)
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


@pytest.mark.parametrize("stop_at", ["probe_return", "guard_read"])
def test_public_stop_at_git_probe_return_prevents_workspace_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_at: str,
) -> None:
    from agent_run.adapters import git_objects
    from agent_run.adapters.local_git import LocalEffects
    from agent_run.model import DeliveryKind
    from support.offline import Scenario

    scenario = Scenario(tmp_path / "stop-at-probe", DeliveryKind.TICKET)
    checkout = Path(scenario.delivery.checkout)
    assert not checkout.exists()
    real_run, real_probe, real_spawn = git_objects.run_git, git_command._require_waitable_children, git_command.subprocess.Popen
    creating = False
    stopped = False
    worktree_spawns = []
    real_reconcile = LocalEffects.reconcile

    def stop():
        nonlocal stopped
        assert scenario.kernel.control("stop", "stop-at-git-probe") == "applied"
        stopped = True

    def reconcile(local, effect):
        result = real_reconcile(local, effect)
        if creating and not stopped and stop_at == "guard_read":
            stop()
        return result

    def run(arguments, **kwargs):
        nonlocal creating
        previous = creating
        creating = "worktree" in arguments and "add" in arguments
        try:
            return real_run(arguments, **kwargs)
        finally:
            creating = previous

    def probe():
        nonlocal stopped
        real_probe()
        if creating and not stopped and stop_at == "probe_return":
            stop()

    def spawn(arguments, **kwargs):
        if "worktree" in arguments and "add" in arguments:
            worktree_spawns.append(arguments)
        return real_spawn(arguments, **kwargs)

    monkeypatch.setattr(LocalEffects, "reconcile", reconcile)
    monkeypatch.setattr(git_objects, "run_git", run)
    monkeypatch.setattr(git_command, "_require_waitable_children", probe)
    monkeypatch.setattr(git_command.subprocess, "Popen", spawn)
    for _ in range(16):
        scenario.runner().run_once()
        if stopped:
            break
    assert stopped and scenario.kernel.source_status()["stopped"] is True
    assert not checkout.exists()
    assert worktree_spawns == []


@pytest.mark.parametrize("consume_first", [False, True], ids=["before_reap", "after_reap"])
def test_real_popen_wait_error_uses_stable_identity_and_disables_automatic_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consume_first: bool,
) -> None:
    import subprocess

    processes = []
    signals = []
    stable_waits = []
    owned_fds = []
    numeric_reaps = []
    poll_attempts = []
    handles = {}
    injected = False
    real_spawn, real_open = subprocess.Popen, os.pidfd_open
    real_waitid, real_waitpid, real_signal = os.waitid, os.waitpid, os.killpg

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        processes.append(process)
        owned_fds.extend(stream.fileno() for stream in (process.stdout, process.stderr, process.stdin))
        return process

    def pidfd_open(pid, flags=0):
        fd = real_open(pid, flags)
        handles[fd] = pid
        if processes and pid == processes[0].pid:
            owned_fds.append(fd)
        return fd

    def waitid(kind, identity, options):
        nonlocal injected
        target = processes and handles.get(identity) == processes[0].pid
        if kind == os.P_PIDFD and target and not options & os.WNOWAIT:
            stable_waits.append(identity)
            if not injected:
                injected = True
                if consume_first:
                    real_waitid(kind, identity, options)
                raise OSError("injected uncertain wait result")
        return real_waitid(kind, identity, options)

    def waitpid(pid, options):
        nonlocal injected
        if processes and pid == processes[0].pid:
            numeric_reaps.append(pid)
            if not injected:
                injected = True
                if consume_first:
                    real_waitpid(pid, options)
                raise OSError("injected uncertain wait result")
        return real_waitpid(pid, options)

    def signal_group(pid, sig):
        assert pid == processes[0].pid
        signals.append(pid)
        real_signal(pid, sig)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "killpg", signal_group)
    with pytest.raises(OSError, match="injected uncertain wait result"):
        git_command.run_git(["git", "--version"], cwd=tmp_path, input="input")
    process = processes[0]
    assert process.stdout.closed and process.stderr.closed and process.stdin.closed
    # Exercise the real Popen destructor, not a SimpleNamespace stand-in.
    monkeypatch.setattr(process, "_internal_poll", lambda **kw: poll_attempts.append(kw))
    process.__del__()
    # Keep a failing baseline test from enqueuing its fixture for later
    # implicit numeric reaping after the assertion has already caught it.
    setattr(process, "_child_created", False)
    if process in subprocess._active:
        subprocess._active.remove(process)
    assert poll_attempts == []
    assert numeric_reaps == []
    assert len(stable_waits) == 2 and stable_waits[0] == stable_waits[1]
    assert signals == [process.pid]
    assert process.returncode is None  # Do not invent a success/exit status.
    for fd in owned_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(ChildProcessError):
        real_waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


@pytest.mark.parametrize("consume_first", [False, True], ids=["before_reap", "after_reap"])
def test_probe_uncertain_reap_retries_same_pidfd_without_starting_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consume_first: bool,
) -> None:
    monkeypatch.setattr(git_command, "require_native_waitable_children", lambda: False)
    real_wait = os.waitid
    waits = []
    injected = False

    def waitid(kind, identity, options):
        nonlocal injected
        if not options & os.WNOWAIT:
            assert kind == os.P_PIDFD
            waits.append(identity)
            if not injected:
                injected = True
                if consume_first:
                    real_wait(kind, identity, options)
                raise OSError("injected uncertain probe wait")
        return real_wait(kind, identity, options)

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("Git started after the admission probe failed")

    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(git_command.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(GitError, match="waitable children"):
        git_command.run_git(["git", "--version"], cwd=tmp_path)
    assert len(waits) == 2 and waits[0] == waits[1]
    with pytest.raises(OSError):
        os.fstat(waits[0])


@pytest.mark.parametrize("arguments", [
    ("init",), ("config", "user.name", "synthetic"), ("add", "file"),
    ("commit",), ("commit-tree",), ("reset",), ("update-ref",),
    ("worktree", "add"), ("switch",), ("write-tree",),
    ("merge-tree", "--write-tree"), ("merge",), ("hash-object", "-w"), ("push",),
])
def test_local_git_mutations_recheck_authority_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...],
) -> None:
    from agent_run.adapters.git_objects import LocalGit
    from agent_run.errors import KernelError

    boundary = []

    def probe():
        boundary.append("probe")

    def authorize():
        assert boundary == ["probe"]
        boundary.append("authorize")
        raise KernelError("synthetic_stop")

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("a stopped mutation reached Popen")

    monkeypatch.setattr(git_command, "_require_waitable_children", probe)
    monkeypatch.setattr(git_command.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(KernelError, match="synthetic_stop"):
        LocalGit(tmp_path, authorize).run(*arguments)
    assert boundary == ["probe", "authorize"]


@pytest.mark.parametrize("phase", ["probe", "git"])
def test_pidfd_acquisition_failure_reaps_pinned_child_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    monkeypatch.setattr(git_command, "require_native_waitable_children", lambda: False)
    import subprocess

    processes, probes, numeric_reaps, signals = [], [], [], []
    real_spawn, real_probe = subprocess.Popen, os.posix_spawn
    real_open, real_wait, real_signal = os.pidfd_open, os.waitid, os.killpg

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def probe(*args, **kwargs):
        pid = real_probe(*args, **kwargs)
        probes.append(pid)
        return pid

    def pidfd_open(pid, flags=0):
        if (phase == "probe" and pid in probes or phase == "git" and processes
                and pid == processes[0].pid):
            raise OSError("injected pidfd acquisition failure")
        return real_open(pid, flags)

    def waitid(kind, identity, options):
        if kind == os.P_PID and not options & os.WNOWAIT:
            numeric_reaps.append(identity)
        return real_wait(kind, identity, options)

    def signal_group(pid, sig):
        assert processes and pid == processes[0].pid
        # No consuming wait has happened on this acquisition-failure path.
        real_wait(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        signals.append(pid)
        real_signal(pid, sig)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "posix_spawn", probe)
    monkeypatch.setattr(os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "killpg", signal_group)
    with pytest.raises(GitError if phase == "probe" else OSError):
        git_command.run_git(["git", "--version"], cwd=tmp_path)
    target = probes[0] if phase == "probe" else processes[0].pid
    assert numeric_reaps == [target]
    assert signals == ([] if phase == "probe" else [target])
    if phase == "probe":
        assert processes == []
    else:
        assert processes[0].stdout.closed and processes[0].stderr.closed
        assert processes[0]._child_created is False
    with pytest.raises(ChildProcessError):
        real_wait(os.P_PID, target, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def test_probe_observation_error_still_reaps_by_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_command, "require_native_waitable_children", lambda: False)
    real_wait = os.waitid
    waits = []

    def waitid(kind, identity, options):
        assert kind == os.P_PIDFD
        waits.append((identity, options))
        result = real_wait(kind, identity, options)
        if options & os.WNOWAIT:
            raise OSError("injected probe observation failure")
        return result

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("Git started after the admission probe failed")

    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(git_command.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(GitError, match="waitable children"):
        git_command.run_git(["git", "--version"], cwd=tmp_path)
    assert len(waits) == 2 and waits[0][0] == waits[1][0]
    with pytest.raises(OSError):
        os.fstat(waits[0][0])
