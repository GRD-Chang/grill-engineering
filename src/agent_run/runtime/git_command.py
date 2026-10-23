from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from functools import lru_cache
from collections.abc import Mapping
from pathlib import Path

from agent_run.errors import GitError
from agent_run.runtime.process_io import CompleteOutput, TailOutput, pump
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


def run_git(
    arguments: list[str], *, cwd: Path, input: str | None = None,
    timeout: float | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Drain both pipes; never return a truncated patch or other Git result."""
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

    executable = _git_executable(environment.get("PATH", os.defpath))
    process = subprocess.Popen(
        [executable, *arguments[1:]], cwd=cwd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env=environment,
    )
    deadline = time.monotonic() + timeout if timeout is not None else None
    stdout, stderr = CompleteOutput(MAX_GIT_OUTPUT_BYTES), TailOutput(MAX_GIT_ERROR_BYTES)

    def check() -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            assert timeout is not None
            raise subprocess.TimeoutExpired(arguments, timeout)
        if process.poll() is not None:
            # Hooks must not leave descendants holding our output pipes open.
            _kill_group(process.pid)
        return True

    try:
        pump(process, stdout=stdout, stderr=stderr, input_data=data, check=check)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as timeout_error:
        raise GitError(
            f"Git command timed out after {timeout:g} seconds",
            evidence={"command": arguments, "returncode": None,
                      "stdout": safe_output(stdout.data.decode("utf-8", errors="replace")),
                      "stderr": safe_output(stderr.data.decode("utf-8", errors="replace")),
                      "timeout_seconds": timeout},
        ) from timeout_error
    finally:
        _kill_group(process.pid)
        process.wait()
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None:
                stream.close()
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
