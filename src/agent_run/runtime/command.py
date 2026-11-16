"""Bounded output capture and cleanup for an adapter-owned process group."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from collections.abc import Callable

from agent_run.runtime.process_io import TailOutput, pump


@dataclass(frozen=True)
class CommandCapture:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def capture_command(
    process: subprocess.Popen[bytes], *, timeout: float, max_output: int,
    stdout: TailOutput | None = None, stderr: TailOutput | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CommandCapture:
    """Drain both pipes and reap a child started with start_new_session=True.

    The adapter owns spawning, environment, and result interpretation. Only the
    child's process group is terminated; escaped descendants are not inferred
    from unrelated processes owned by the caller. Optional bounded collectors
    are passed to the existing pump; omitted collectors keep tail capture.
    """
    stdout = TailOutput(max_output) if stdout is None else stdout
    stderr = TailOutput(max_output) if stderr is None else stderr
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
            if exited or time.monotonic() >= deadline or cancelled is not None and cancelled():
                timed_out = not exited and time.monotonic() >= deadline
                terminate()
                # Escaped pipe holders cannot prolong this owned-group cleanup.
                drain_deadline = time.monotonic() + 0.1
        return drain_deadline is None or time.monotonic() < drain_deadline

    failure: BaseException | None = None
    try:
        pump(process, stdout=stdout, stderr=stderr, check=check, ignore_read_errors=True)
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_failure: BaseException | None = None
        try:
            returncode = terminate()
        except BaseException as error:
            cleanup_failure = error
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    controls = (TimeoutError, InterruptedError, KeyboardInterrupt, SystemExit)
                    if (cleanup_failure is None or isinstance(error, controls)
                            and not isinstance(cleanup_failure, controls)):
                        cleanup_failure = error
        # A failed capture supplies no ACK. Preserve its original exception;
        # otherwise propagate cleanup failure, with control errors taking priority.
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure
    return CommandCapture(returncode, bytes(stdout.data), bytes(stderr.data), timed_out)
