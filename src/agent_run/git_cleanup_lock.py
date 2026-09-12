from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from agent_run.git_errors import GitError
from agent_run.git_output import MAX_GIT_ERROR_BYTES, _kill_group


_ACK_TIMEOUT_SECONDS = 5.0


@contextmanager
def hold_cleanup_head(
    root: Path, *, checkout: Path, branch: str, expected_head_sha: str
) -> Iterator[None]:
    """Lock the checkout HEAD and exact source ref during retirement.

    Git 2.43 rejects HEAD and its referent in one transaction. Two prepared
    verification transactions, ordered HEAD then branch, preserve both locks.
    """
    if "\0" in branch or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_head_sha) is None:
        raise GitError("cleanup verification requires an exact source ref and commit")
    with _hold_ref(checkout, "HEAD", expected_head_sha):
        with _hold_ref(root, f"refs/heads/{branch}", expected_head_sha):
            yield


@contextmanager
def _hold_ref(root: Path, ref: str, expected_head_sha: str) -> Iterator[None]:
    process = subprocess.Popen(
        ["git", "update-ref", "--no-deref", "--stdin", "-z"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    prepared = False
    try:
        # NUL framing avoids treating branch names as transaction commands.
        request = f"start\0verify {ref}\0{expected_head_sha}\0prepare\0"
        process.stdin.write(request.encode())
        process.stdin.flush()
        _await_prepared(process)
        prepared = True
        yield
    except (BrokenPipeError, subprocess.TimeoutExpired) as error:
        raise GitError("could not hold the completed source ref for cleanup") from error
    finally:
        # Closing stdin aborts even a prepared transaction. Give Git a bounded
        # opportunity to remove its own lock files before terminating helpers.
        if not prepared:
            _terminate_group(process.pid)
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=_ACK_TIMEOUT_SECONDS if prepared else 1.0)
        except subprocess.TimeoutExpired:
            _terminate_group(process.pid)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid)
                process.wait()
        finally:
            _kill_group(process.pid)
            process.stdout.close()


def _await_prepared(process: subprocess.Popen[bytes]) -> None:
    assert process.stdout is not None
    deadline = monotonic() + _ACK_TIMEOUT_SECONDS
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while b"prepare: ok\n" not in output.split(b"start: ok\n", 1)[-1]:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise GitError("timed out preparing the completed source ref for cleanup")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise GitError(output.decode(errors="replace").strip() or "could not lock completed source ref")
            output.extend(chunk)
            if len(output) > MAX_GIT_ERROR_BYTES:
                raise GitError("cleanup ref preparation output exceeds the bounded limit")


def _terminate_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
