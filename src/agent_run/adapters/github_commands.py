from __future__ import annotations

import random
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any
from pathlib import Path

from agent_run.adapters.auth.runner_transport import runner_options
from agent_run.runtime.command import capture_command
from agent_run.errors import GitHubReadError, redact_transport_output


MAX_READ_ATTEMPTS = 3
READ_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024


class _TimedOutRead(subprocess.CompletedProcess[str]):
    """Keep timeout provenance while retaining the existing retry result contract."""


def read_response_error(response: subprocess.CompletedProcess[str], *, invalid_json: bool = False) -> GitHubReadError:
    """Fixed safe facts only; no arbitrary command output becomes a diagnostic."""
    from agent_run.adapters.auth.native_probe import _ErrorEvidence

    output = response.stdout.strip()
    try:
        parsed = json.loads(output)
    except (ValueError, RecursionError):
        shape = "text" if output else "empty"
    else:
        shape = "json_array" if isinstance(parsed, list) else "json_object" if isinstance(parsed, dict) else "json_scalar"
    evidence = _ErrorEvidence()
    evidence.feed(response.stderr.encode())
    timed_out = isinstance(response, _TimedOutRead)
    category = ("timeout" if timed_out else "invalid_json" if invalid_json else
                "network" if evidence.network_failure else "command_failed")
    status = re.search(r"\bHTTP[ /](?:[0-9.]+ )?([1-5][0-9]{2})\b", response.stderr, re.IGNORECASE)
    detail = f"; HTTP {status[1]}" if status else ""
    return GitHubReadError("github_invalid_response" if invalid_json else "github_read_failed",
        f"GitHub observation failed: category={category}; exit={None if timed_out else response.returncode}; output_shape={shape}{detail}")


def run_read_command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    before_attempt: Callable[[], None] | None = None,
    runner_identity: Mapping[str, Any] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    last: subprocess.CompletedProcess[str] | None = None
    cancellation: dict[str, Any] = {"cancelled": cancelled} if cancelled is not None else {}
    for attempt in range(MAX_READ_ATTEMPTS):
        if before_attempt is not None:
            before_attempt()
        try:
            result = _run_bounded_command(
                arguments,
                cwd=cwd,
                timeout=READ_TIMEOUT_SECONDS,
                **runner_options(runner_identity),
                **cancellation,
            )
        except subprocess.TimeoutExpired as error:
            result = _TimedOutRead(
                arguments,
                1,
                _text(error.stdout),
                _text(error.stderr) or "GitHub command timed out",
            )
        if cancelled is not None and cancelled():
            raise InterruptedError("GitHub read cancelled")
        if result.returncode == 0 or attempt == MAX_READ_ATTEMPTS - 1:
            return result
        last = result
        time.sleep((2**attempt) + random.uniform(0, 0.25))
    assert last is not None
    return last


def run_write_command(
    arguments: list[str], *, authorize: Callable[[], None], cwd: Path | None = None,
    runner_identity: Mapping[str, Any] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Prepare credentials, then authorize once at the command-start boundary."""
    cancellation: dict[str, Any] = {"cancelled": cancelled} if cancelled is not None else {}
    try:
        return _run_bounded_command(
            arguments,
            cwd=cwd,
            timeout=READ_TIMEOUT_SECONDS,
            before_start=authorize,
            **runner_options(runner_identity),
            **cancellation,
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
    arguments: list[str], *, cwd: Path | None, timeout: int,
    runner_identity: Mapping[str, Any] | None = None,
    before_start: Callable[[], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    from agent_run.adapters.auth.runner_transport import (
        runner_environment, runner_git_environment,
    )

    environment = (runner_git_environment(runner_identity, cwd=cwd) if arguments[0] == "git"
                   else runner_environment(runner_identity))
    if before_start is not None:
        # No credential preparation or remote observation after this callback.
        before_start()
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=environment,
    )
    cancellation: dict[str, Any] = {"cancelled": cancelled} if cancelled is not None else {}
    result = capture_command(
        process, timeout=timeout, max_output=MAX_COMMAND_OUTPUT_BYTES,
        **cancellation,
    )
    stdout = redact_transport_output(result.stdout.decode("utf-8", errors="replace"), environment)
    stderr = redact_transport_output(result.stderr.decode("utf-8", errors="replace"), environment)
    if result.timed_out:
        # Match CompletedProcess[str]: timeout streams are decoded and redacted
        # text too, so callers never need to log raw credential-bearing bytes.
        raise subprocess.TimeoutExpired(
            arguments, timeout, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(
        arguments,
        result.returncode,
        stdout,
        stderr,
    )
