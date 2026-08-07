from __future__ import annotations

from collections.abc import Callable
import random
import subprocess
import time
from pathlib import Path
from typing import TypeVar


MAX_READ_ATTEMPTS = 3
READ_TIMEOUT_SECONDS = 30
T = TypeVar("T")


def run_read_command(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(MAX_READ_ATTEMPTS):
        try:
            result = subprocess.run(
                arguments,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=READ_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            result = subprocess.CompletedProcess(
                arguments,
                1,
                _text(error.stdout),
                _text(error.stderr) or "GitHub command timed out",
            )
        if not _is_transient_failure(result) or attempt == MAX_READ_ATTEMPTS - 1:
            return result
        last = result
        time.sleep((2**attempt) + random.uniform(0, 0.25))
    assert last is not None
    return last


def retry_read_operation(
    operation: Callable[[], T], *, should_retry: Callable[[Exception], bool]
) -> T:
    """Run a non-subprocess read through the same bounded retry budget."""
    last_error: Exception | None = None
    for attempt in range(MAX_READ_ATTEMPTS):
        try:
            return operation()
        except Exception as error:
            if not should_retry(error) or attempt == MAX_READ_ATTEMPTS - 1:
                raise
            last_error = error
            time.sleep((2**attempt) + random.uniform(0, 0.25))
    assert last_error is not None
    raise last_error


def run_write_command(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            arguments,
            1,
            _text(error.stdout),
            _text(error.stderr) or "GitHub command timed out",
        )


def _is_transient_failure(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    return is_transient_message(f"{result.stdout}\n{result.stderr}")


def is_transient_message(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "deadline exceeded",
            "connection reset",
            "connection refused",
            "temporary failure",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "i/o timeout",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
