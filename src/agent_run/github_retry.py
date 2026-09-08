from __future__ import annotations

import random
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path


MAX_READ_ATTEMPTS = 3
READ_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024


def run_read_command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    before_attempt: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(MAX_READ_ATTEMPTS):
        if before_attempt is not None:
            before_attempt()
        try:
            result = _run_bounded_command(
                arguments,
                cwd=cwd,
                timeout=READ_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            result = subprocess.CompletedProcess(
                arguments,
                1,
                _text(error.stdout),
                _text(error.stderr) or "GitHub command timed out",
            )
        if result.returncode == 0 or attempt == MAX_READ_ATTEMPTS - 1:
            return result
        last = result
        time.sleep((2**attempt) + random.uniform(0, 0.25))
    assert last is not None
    return last


def run_write_command(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_bounded_command(
            arguments,
            cwd=cwd,
            timeout=READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            arguments,
            1,
            _text(error.stdout),
            _text(error.stderr) or "GitHub command timed out",
        )


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _run_bounded_command(
    arguments: list[str], *, cwd: Path | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream: object, destination: bytearray) -> None:
        fileno = getattr(stream, "fileno", None)
        if not callable(fileno):
            return
        try:
            descriptor = fileno()
            while chunk := os.read(descriptor, 64 * 1024):
                destination.extend(chunk)
                if len(destination) > MAX_COMMAND_OUTPUT_BYTES:
                    del destination[:-MAX_COMMAND_OUTPUT_BYTES]
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
        process.wait()
        for reader in readers:
            if reader.ident is not None:
                reader.join()
        process.stdout.close()
        process.stderr.close()
    if timed_out:
        raise subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=bytes(stdout),
            stderr=bytes(stderr),
        )
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        bytes(stdout).decode("utf-8", errors="replace"),
        bytes(stderr).decode("utf-8", errors="replace"),
    )
