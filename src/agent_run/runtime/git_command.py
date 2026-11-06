from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from functools import lru_cache
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_run.errors import GitError
from agent_run.runtime.process_io import CompleteOutput, TailOutput, pump
from agent_run.runtime.native_child_policy import require_native_waitable_children
from agent_run.errors import redact_transport_output


MAX_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_GIT_ERROR_BYTES = 64 * 1024
_TRUNCATED = "[Git output truncated]\n"


@lru_cache(maxsize=16)
def _git_executable(search_path: str) -> str:
    # A delivery performs many short Git commands. Resolve an absolute PATH
    # once so each child does not repeat the shell-style directory search.
    # Relative entries depend on cwd, so never cache their result.
    if any(not os.path.isabs(entry) for entry in search_path.split(os.pathsep)):
        return "git"
    return shutil.which("git", path=search_path) or "git"


def git_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep authentication, but prevent inherited repository redirection."""
    locations = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE", "GIT_TEMPLATE_DIR", "GIT_SHALLOW_FILE", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
    return {
        key: value for key, value in (os.environ if environment is None else environment).items()
        if key not in locations
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    } | {"GIT_OPTIONAL_LOCKS": "0"}


def _wait_child(kind: int, identity: int, options: int) -> os.waitid_result | None:
    try:
        return os.waitid(kind, identity, options)
    except ChildProcessError as error:
        raise GitError("Git child ownership lost") from error


def _reap_child(kind: int, identity: int) -> int:
    try:
        result = _wait_child(kind, identity, os.WEXITED)
    except GitError:
        raise
    except BaseException as wait_error:
        # A failed wait can already have consumed the child. Only a stable
        # pidfd may be retried; a numeric PID must never be looked up again.
        if kind == os.P_PIDFD:
            try:
                _wait_child(kind, identity, os.WEXITED)
            except BaseException as cleanup_error:
                raise wait_error from cleanup_error
        raise
    assert result is not None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def _require_waitable_children() -> None:
    """Linux admission, without changing the host's SIGCHLD disposition.

    The known glibc ABI reads sigaction and validates pidfd/waitid on self.
    Other ABIs retain a real child probe for hidden SA_NOCLDWAIT. The caller
    must retain this policy and exclusive ownership during run_git.
    """
    try:
        if not all(hasattr(os, name) for name in ("WNOWAIT", "P_PIDFD", "pidfd_open")):
            raise ValueError("Linux pidfd/WNOWAIT support is unavailable")
        if require_native_waitable_children():
            return
        masks = dict(
            line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith(("SigIgn:", "SigCgt:"))
        )
        child_signal = 1 << (signal.SIGCHLD - 1)
        if any(int(masks[name], 16) & child_signal for name in ("SigIgn", "SigCgt")):
            raise ValueError("SIGCHLD is ignored or caught")
        # /bin/true is already required by the Linux installation/probes. No
        # inherited loader environment, fork-in-Python, or signal policy change.
        probe = os.posix_spawn("/bin/true", ["/bin/true"], {})
        probe_fd = None
        try:
            probe_fd = os.pidfd_open(probe)
            _wait_child(os.P_PIDFD, probe_fd, os.WEXITED | os.WNOWAIT)
        finally:
            try:
                if probe_fd is None:
                    # Acquisition failure permits one first reap only after
                    # WNOWAIT proves a still-pinned zombie. SA_NOCLDWAIT
                    # cannot yield a waitable exit, so it never reaches reap.
                    _wait_child(os.P_PID, probe, os.WEXITED | os.WNOWAIT)
                _reap_child(os.P_PID if probe_fd is None else os.P_PIDFD,
                            probe if probe_fd is None else probe_fd)
            finally:
                if probe_fd is not None:
                    os.close(probe_fd)
    except (GitError, OSError, ValueError, KeyError, AttributeError) as error:
        raise GitError(
            "Git requires waitable children and a fixed default SIGCHLD policy"
        ) from error


def run_git(
    arguments: list[str], *, cwd: Path, input: str | None = None,
    timeout: float | None = None,
    environment: Mapping[str, str] | None = None,
    authorize: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Drain both pipes; never return a truncated patch or other Git result.

    Linux/CPython host contract: fixed default SIGCHLD policy, no competing
    reaper, and exclusive ownership of this private Popen until final reap.
    The optional write guard runs after preparation, immediately before spawn.
    """
    if input is not None and len(input) > MAX_GIT_OUTPUT_BYTES:
        raise GitError("Git input exceeds the 1 MiB transfer limit")
    data = input.encode() if input is not None else b""
    if len(data) > MAX_GIT_OUTPUT_BYTES:
        raise GitError("Git input exceeds the 1 MiB transfer limit")
    command = arguments[1:]
    while command[:1] in (["-C"], ["-c"]):
        command = command[2:]
    remote = bool(command and command[0] in {"clone", "fetch", "push", "pull", "ls-remote"})
    environment = dict(environment) if environment is not None else git_environment()
    selected_environment = environment

    def safe_output(value: str) -> str:
        if remote:
            return redact_transport_output(value, selected_environment)
        return value

    _require_waitable_children()
    executable = _git_executable(environment.get("PATH", os.defpath))
    prepared = [executable, *arguments[1:]]
    if authorize is not None:
        authorize()
    process = subprocess.Popen(
        prepared, cwd=cwd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env=environment,
    )
    # Transfer reaping out of Popen, including its CPython destructor/_active
    # cleanup. returncode stays None if a wait result is genuinely unknown.
    setattr(process, "_child_created", False)
    pidfd = None
    deadline = time.monotonic() + timeout if timeout is not None else None
    stdout, stderr = CompleteOutput(MAX_GIT_OUTPUT_BYTES), TailOutput(MAX_GIT_ERROR_BYTES)
    exited = False
    group_signalled = False

    def observe(*, block: bool = False) -> bool:
        return _wait_child(
            os.P_PID if pidfd is None else os.P_PIDFD,
            process.pid if pidfd is None else pidfd,
            os.WEXITED | os.WNOWAIT | (0 if block else os.WNOHANG),
        ) is not None

    def close_group() -> None:
        nonlocal group_signalled
        if not group_signalled:
            # A live child or its unreaped zombie reserves the PID/PGID. The
            # host must keep SIGCHLD policy fixed and leave this child to us.
            observe()
            group_signalled = True
            _kill_group(process.pid)

    def check() -> bool:
        nonlocal exited
        if deadline is not None and time.monotonic() >= deadline:
            assert timeout is not None
            raise subprocess.TimeoutExpired(arguments, timeout)
        exited = observe()
        if exited:
            # Hooks must not leave descendants holding our output pipes open.
            close_group()
        return True

    try:
        # Admission and sole ownership pin the new child's PID until our first
        # consuming wait. Acquire its stable identity before entering the pump.
        pidfd = os.pidfd_open(process.pid)
        # The pump must not reap through poll, including after pipe EOF.
        pump(process, stdout=stdout, stderr=stderr, input_data=data, check=check, stop_at_eof=True)
        while not exited:
            if deadline is None:
                exited = observe(block=True)
            else:
                check()
                if not exited:
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    except subprocess.TimeoutExpired as timeout_error:
        raise GitError(
            f"Git command timed out after {timeout:g} seconds",
            evidence={"command": arguments, "returncode": None,
                      "stdout": safe_output(stdout.data.decode("utf-8", errors="replace")),
                      "stderr": safe_output(stderr.data.decode("utf-8", errors="replace")),
                      "timeout_seconds": timeout},
        ) from timeout_error
    finally:
        try:
            close_group()
            returncode = _reap_child(os.P_PID if pidfd is None else os.P_PIDFD,
                                     process.pid if pidfd is None else pidfd)
            process.returncode = returncode
        finally:
            try:
                for stream in (process.stdout, process.stderr, process.stdin):
                    if stream is not None:
                        stream.close()
            finally:
                if pidfd is not None:
                    os.close(pidfd)
    error = safe_output(stderr.data.decode("utf-8", errors="replace"))
    # Replacement decoding may expand invalid bytes; the diagnostic is byte bounded too.
    encoded = error.encode()
    if stderr.overflow or len(encoded) > MAX_GIT_ERROR_BYTES:
        error = _TRUNCATED + encoded[-(MAX_GIT_ERROR_BYTES - len(_TRUNCATED)):].decode(
            "utf-8", errors="ignore"
        )
    if stdout.overflow:
        raise GitError(
            "Git stdout exceeds the 1 MiB transfer limit; complete result unavailable\n"
            + error[-(MAX_GIT_ERROR_BYTES // 4):]
        )
    try:
        output = safe_output(stdout.finish().decode("utf-8"))
    except UnicodeDecodeError as invalid_output:
        raise GitError("Git stdout is not valid UTF-8; complete result unavailable") from invalid_output
    return subprocess.CompletedProcess(arguments, returncode, output, error)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
