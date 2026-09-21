from __future__ import annotations

import random
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from agent_run.bounded_command import capture_command
from agent_run.git_output import git_environment


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
        env=git_environment(),
    )
    result = capture_command(
        process, timeout=timeout, max_output=MAX_COMMAND_OUTPUT_BYTES
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(
            arguments, timeout, output=result.stdout, stderr=result.stderr
        )
    return subprocess.CompletedProcess(
        arguments,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )
