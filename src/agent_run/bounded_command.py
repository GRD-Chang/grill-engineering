"""Bounded output capture and cleanup for an adapter-owned process group."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


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
    stdout = bytearray()
    stderr = bytearray()

    stop_readers = threading.Event()

    def drain(stream: object, destination: bytearray) -> None:
        fileno = getattr(stream, "fileno", None)
        if not callable(fileno):
            return
        try:
            descriptor = fileno()
            os.set_blocking(descriptor, False)
            deadline: float | None = None
            while True:
                if stop_readers.is_set():
                    # Drain buffered output, but escaped pipe holders cannot
                    # extend cleanup indefinitely by keeping the pipe open.
                    if deadline is None:
                        deadline = time.monotonic() + 0.1
                    if time.monotonic() >= deadline:
                        return
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    if deadline is not None:
                        return
                    stop_readers.wait(0.05)
                    continue
                if not chunk:
                    return
                destination.extend(chunk)
                if len(destination) > max_output:
                    del destination[:-max_output]
        except OSError:
            return

    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    )
    timed_out = False
    try:
        for reader in readers:
            reader.start()
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # A successful command may leave descendants holding the output pipes.
        # Close the owned group on every path before waiting for EOF readers.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        cleanup_returncode = process.wait()
        if timed_out:
            returncode = cleanup_returncode
        stop_readers.set()
        for reader in readers:
            if reader.ident is not None:
                reader.join()
        process.stdout.close()
        process.stderr.close()
    return CommandCapture(returncode, bytes(stdout), bytes(stderr), timed_out)
