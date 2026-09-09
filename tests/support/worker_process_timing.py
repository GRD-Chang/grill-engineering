"""Control test deadlines while retaining real Worker processes and cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import agent_run.worker_sandbox as worker_sandbox


class AdvancingClock:
    """Advance a deadline without changing subprocess/thread library clocks."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    sleep = staticmethod(time.sleep)


def run_worker_expecting_early_failure(
    command: list[str],
    *,
    cwd: Path,
    ready: threading.Event,
    monkeypatch: pytest.MonkeyPatch,
    on_stdout_line: Callable[[str], None] | None = None,
    abort_event: threading.Event | None = None,
    abort_reason: Callable[[], str] | None = None,
    on_process_started: Callable[[int], None] | None = None,
) -> None:
    """Observe cancellation after its trigger, excluding process startup."""

    finished = threading.Event()
    errors: list[BaseException] = []
    processes: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def capture_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def run() -> None:
        try:
            worker_sandbox.run_worker_process(
                command, cwd=cwd, prompt="", environment=os.environ.copy(),
                timeout=60, on_stdout_line=on_stdout_line,
                abort_event=abort_event, abort_reason=abort_reason,
                on_process_started=on_process_started,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(worker_sandbox.subprocess, "Popen", capture_popen)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert ready.wait(10), "Worker did not reach its cancellation trigger"
        # Missing cancellation must fail well before the 60s process deadline.
        assert finished.wait(5), "cancellation did not terminate the ready Worker"
        for process in processes:
            assert process.poll() is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
        assert len(errors) == 1
        raise errors[0]
    finally:
        for process in processes:
            # A reaped parent may still have descendants holding stdout open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        worker.join(timeout=5)
        assert not worker.is_alive()
