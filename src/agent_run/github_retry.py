from __future__ import annotations

import random
import subprocess
import time
from pathlib import Path


MAX_READ_ATTEMPTS = 3
READ_TIMEOUT_SECONDS = 30
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


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
