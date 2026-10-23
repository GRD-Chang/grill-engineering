"""Bytes-only pipe mechanics; callers own spawning, deadlines and termination."""
from __future__ import annotations

from collections.abc import Callable
import os
import selectors
import subprocess


class TailOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.overflow = False

    def feed(self, chunk: bytes) -> None:
        excess = len(self.data) + len(chunk) - self.limit
        if excess > 0:
            self.overflow = True
            del self.data[:excess]
        self.data.extend(chunk[-self.limit:] if len(chunk) > self.limit else chunk)


class CompleteOutput(TailOutput):
    def finish(self) -> bytes:
        if self.overflow:
            raise ValueError("Complete process output unavailable: limit exceeded")
        return bytes(self.data)


def pump(
    process: subprocess.Popen[bytes], *, stdout: TailOutput | None = None,
    stderr: TailOutput | None = None,
    check: Callable[[], bool], input_data: bytes | None = None, ignore_read_errors: bool = False,
) -> None:
    """Pump pipes until EOF and leader exit, or until the owner ends draining.

    check runs at most 50 ms apart while waiting. It may terminate the owned
    group, raise a deadline error, or end a bounded post-exit drain. No pipe
    observation here proves descendant exit or authorizes another operation.
    Each output collector must correspond to an actual pipe, or both are None.
    """
    pending = memoryview(input_data or b"")
    with selectors.DefaultSelector() as selector:
        for stream, output in ((process.stdout, stdout), (process.stderr, stderr)):
            if stream is None and output is None:
                continue
            if stream is None or output is None:
                raise ValueError("A pipe and its collector must be selected together")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, output)
        if process.stdin is not None and input_data is not None:
            os.set_blocking(process.stdin.fileno(), False)
            if pending:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        while selector.get_map() or process.poll() is None:
            if not check():
                return
            for key, _ in selector.select(timeout=0.05):
                if key.events == selectors.EVENT_WRITE:
                    try:
                        pending = pending[os.write(key.fd, pending[:16384]):]
                    except BrokenPipeError:
                        pending = memoryview(b"")
                    if not pending:
                        selector.unregister(key.fileobj)
                        assert process.stdin is not None
                        process.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, 16384)
                except BlockingIOError:
                    continue
                except OSError:
                    if not ignore_read_errors:
                        raise
                    chunk = b""
                if chunk:
                    key.data.feed(chunk)
                else:
                    selector.unregister(key.fileobj)
