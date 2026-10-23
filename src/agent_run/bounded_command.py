"""Bounded output capture and cleanup for an adapter-owned process group."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from ._process_io import TailOutput, pump


@dataclass(frozen=True)
class CommandCapture:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def capture_command(
    process: subprocess.Popen[bytes], *, timeout: float, max_output: int
) -> CommandCapture:
    """Drain both pipes and reap a child started with start_new_session=True.

    The adapter owns spawning, environment, and result interpretation. Only the
    child's process group is terminated; escaped descendants are not inferred
    from unrelated processes owned by the caller.
    """
    stdout, stderr = TailOutput(max_output), TailOutput(max_output)
    deadline = time.monotonic() + timeout
    drain_deadline: float | None = None
    timed_out = False
    group_signalled = False

    def terminate() -> int:
        nonlocal group_signalled
        if not group_signalled:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # A later wait/drain failure cannot re-signal a recycled PGID.
            group_signalled = True
        return process.wait()

    def check() -> bool:
        nonlocal timed_out, drain_deadline
        if drain_deadline is None:
            exited = process.poll() is not None
            if exited or time.monotonic() >= deadline:
                timed_out = not exited
                terminate()
                # Escaped pipe holders cannot prolong this owned-group cleanup.
                drain_deadline = time.monotonic() + 0.1
        return drain_deadline is None or time.monotonic() < drain_deadline

    try:
        pump(process, stdout=stdout, stderr=stderr, check=check, ignore_read_errors=True)
    finally:
        try:
            returncode = terminate()
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return CommandCapture(returncode, bytes(stdout.data), bytes(stderr.data), timed_out)
