from __future__ import annotations

import os
import selectors
import signal
import subprocess
from pathlib import Path

from agent_run.git_errors import GitError


MAX_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_GIT_ERROR_BYTES = 64 * 1024
_READ_BYTES = 16 * 1024
_TRUNCATED = "[Git output truncated]\n"


def git_environment() -> dict[str, str]:
    """Keep authentication, but prevent inherited repository redirection."""
    locations = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE", "GIT_TEMPLATE_DIR", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
    return {
        key: value for key, value in os.environ.items()
        if key not in locations
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    } | {"GIT_OPTIONAL_LOCKS": "0"}


def run_git(
    arguments: list[str], *, cwd: Path, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Drain both pipes; never return a truncated patch or other Git result."""
    if input is not None and len(input) > MAX_GIT_OUTPUT_BYTES:
        raise GitError("Git input exceeds the 1 MiB transfer limit")
    data = input.encode() if input is not None else b""
    if len(data) > MAX_GIT_OUTPUT_BYTES:
        raise GitError("Git input exceeds the 1 MiB transfer limit")
    process = subprocess.Popen(
        arguments, cwd=cwd, stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env=git_environment(),
    )
    stdout, stderr = bytearray(), bytearray()
    overflow = {"stdout": False, "stderr": False}
    streams = [process.stdout, process.stderr, process.stdin]
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                if data:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            offset = 0
            while selector.get_map():
                for key, _ in selector.select(timeout=0.1):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fd, data[offset:offset + _READ_BYTES])
                        except BrokenPipeError:
                            offset = len(data)
                        if offset == len(data):
                            selector.unregister(key.fileobj)
                            assert process.stdin is not None
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, _READ_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    destination = stdout if key.data == "stdout" else stderr
                    limit = MAX_GIT_OUTPUT_BYTES if key.data == "stdout" else MAX_GIT_ERROR_BYTES
                    if len(destination) + len(chunk) > limit:
                        overflow[key.data] = True
                        del destination[:max(0, len(destination) + len(chunk) - limit)]
                    destination.extend(chunk)
                if process.poll() is not None:
                    # Hooks must not leave descendants holding our output pipes open.
                    _kill_group(process.pid)
            returncode = process.wait()
    finally:
        _kill_group(process.pid)
        process.wait()
        for opened_stream in streams:
            if opened_stream is not None:
                opened_stream.close()
    error = stderr.decode("utf-8", errors="replace")
    # Replacement decoding may expand invalid bytes; the diagnostic is byte bounded too.
    encoded = error.encode()
    if overflow["stderr"] or len(encoded) > MAX_GIT_ERROR_BYTES:
        error = _TRUNCATED + encoded[-(MAX_GIT_ERROR_BYTES - len(_TRUNCATED)):].decode(
            "utf-8", errors="ignore"
        )
    if overflow["stdout"]:
        raise GitError(
            "Git stdout exceeds the 1 MiB transfer limit; complete result unavailable\n"
            + error[-(MAX_GIT_ERROR_BYTES // 4):]
        )
    try:
        output = stdout.decode("utf-8")
    except UnicodeDecodeError as invalid_output:
        raise GitError("Git stdout is not valid UTF-8; complete result unavailable") from invalid_output
    return subprocess.CompletedProcess(arguments, returncode, output, error)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
