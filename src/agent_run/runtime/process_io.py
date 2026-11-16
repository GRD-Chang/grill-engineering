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


class CompleteOutputOverflow(ValueError):
    """The collector observed more original bytes than its fixed limit."""


class CompleteOutput(TailOutput):
    def __init__(self, limit: int) -> None:
        super().__init__(limit)
        self.eof = False

    def finish(self, *, require_eof: bool = False) -> bytes:
        if self.overflow:
            raise CompleteOutputOverflow("Complete process output unavailable: limit exceeded")
        if require_eof and not self.eof:
            raise OSError("Complete process output unavailable: stdout EOF not observed")
        return bytes(self.data)


def pump(
    process: subprocess.Popen[bytes], *, stdout: TailOutput | None = None,
    stderr: TailOutput | None = None,
    check: Callable[[], bool], input_data: bytes | None = None, ignore_read_errors: bool = False,
    stop_at_eof: bool = False,
) -> None:
    """Pump pipes until EOF and leader exit, or until the owner ends draining.

    check runs at most 50 ms apart while waiting. It may terminate the owned
    group, raise a deadline error, or end a bounded post-exit drain. No pipe
    observation here proves descendant exit or authorizes another operation.
    Each output collector must correspond to an actual pipe, or both are None.
    stop_at_eof leaves any post-EOF process waiting to the caller.
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
        while selector.get_map() or (not stop_at_eof and process.poll() is None):
            if not check():
                return
            if not selector.get_map():
                # All pipes reached EOF. Keep the same bounded checks while
                # allowing an ordinary child exit to wake the owner early.
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
                continue
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
                except OSError as error:
                    if (not ignore_read_errors or isinstance(key.data, CompleteOutput)
                            or isinstance(stdout, CompleteOutput) and isinstance(error, (TimeoutError, InterruptedError))):
                        raise
                    selector.unregister(key.fileobj)
                    continue  # A failed read never establishes EOF.
                if chunk:
                    key.data.feed(chunk)
                else:
                    if isinstance(key.data, CompleteOutput):
                        key.data.eof = True
                    selector.unregister(key.fileobj)
