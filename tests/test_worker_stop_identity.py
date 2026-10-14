"""An execution-local stop still requires accurate child process evidence."""
from types import SimpleNamespace
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread, enumerate as threads
from typing import Any

import pytest

from agent_run import executor_host, worker_sandbox
from agent_run.agent_work import AgentWorkScope
from agent_run.codex import CodexCliBackend
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_work import TicketWorkAdmission
from agent_run.worker_control import worker_control, worker_lease
from support.ticket_parallel import ticket_run
from support.worker_process_timing import wait_until


@pytest.mark.parametrize("observed", [None, "different"])
def test_unknown_or_replaced_worker_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    observed: str | None,
) -> None:
    signals: list[int] = []
    process = SimpleNamespace(pid=3001, poll=lambda: None)
    monkeypatch.setattr(executor_host, "_process_start_token", lambda _pid: observed)
    monkeypatch.setattr(worker_sandbox.os, "killpg", lambda pid, _signal: signals.append(pid))
    with pytest.raises(worker_sandbox.WorkerSandboxError, match="ownership/exit unknown"):
        worker_sandbox._terminate_process_group(process, start_token="owned")
    assert signals == []
    assert "no process group signal sent" in caplog.text


def test_sent_signal_without_exit_proof_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def wait(*, timeout: float) -> None:
        raise subprocess.TimeoutExpired("owned Worker", timeout)

    process = SimpleNamespace(pid=3001, poll=lambda: None, wait=wait)
    monkeypatch.setattr(executor_host, "_process_start_token", lambda _pid: "owned")
    monkeypatch.setattr(worker_sandbox.os, "killpg", lambda *_: None)
    with pytest.raises(worker_sandbox.WorkerSandboxError, match="exit unknown after"):
        worker_sandbox._terminate_process_group(process, start_token="owned")
    assert "exit unknown after" in caplog.text


@pytest.mark.parametrize("source", ["control", "stdout_callback"])
def test_unknown_exit_reclaims_io_without_reporting_worker_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    ready_path = tmp_path / "ready"
    release_path = tmp_path / "callback-release"
    script = f'''import pathlib,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pathlib.Path({str(ready_path)!r}).write_text('ready')
while not pathlib.Path({str(release_path)!r}).exists():
    time.sleep(0.01)
print('{{"type":"thread.started","thread_id":"unknown-worker"}}', flush=True)
time.sleep(60)
'''
    command = [sys.executable, "-c", script]
    children: list[subprocess.Popen[Any]] = []
    finished: list[int] = []
    execution: list[str] = []
    errors: list[BaseException] = []
    fault, done, callback_failed = Event(), Event(), Event()
    real_popen = subprocess.Popen
    real_token = executor_host._process_start_token

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("agent_run.codex.bubblewrap_command", lambda *_args, **_kwargs: command)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(executor_host, "_process_start_token", lambda pid: None if fault.is_set() else real_token(pid))
    backend = CodexCliBackend(on_worker_finished=finished.append)

    def check() -> None:
        if fault.is_set() and source == "control":
            raise TaskControlBusyError("execution stopped")

    def on_thread(_value: str) -> None:
        callback_failed.set()
        raise OSError("shared save callback failed")

    def run() -> None:
        token = worker_control.set(check)
        try:
            backend._invoke(
                prompt="x" * (1024 * 1024), checkout=tmp_path, thread_id=None,
                timeout=60, on_execution=lambda kind, **_facts: execution.append(kind),
                on_thread=on_thread if source == "stdout_callback" else None,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            worker_control.reset(token)
            done.set()

    thread = Thread(target=run)
    thread.start()
    try:
        wait_until(lambda: ready_path.exists() and "execution_started" in execution)
        io_threads = {item for item in threads() if item.name.startswith("agent-run-worker-")}
        assert len(io_threads) == 3  # Includes a writer blocked by unread stdin.
        fault.set()
        if source == "stdout_callback":
            release_path.write_text("release")
            assert callback_failed.wait(3)
        assert done.wait(3)
        thread.join(1)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], worker_sandbox.WorkerExitUnknownError)
        if source == "stdout_callback":
            assert str(errors[0].__cause__) == "shared save callback failed"
        assert children[0].poll() is None  # Unknown identity did not authorize a kill.
        assert all(not item.is_alive() for item in io_threads)
        assert all(pipe is not None and pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr))
        assert finished == []
        assert "execution_finished" not in execution
    finally:
        fault.set()
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        thread.join(5)
    assert not thread.is_alive()


def test_unknown_exit_retains_its_durable_ticket_lease(git_repo: Path) -> None:
    state, states, _git, _fixture = ticket_run(git_repo, (2, 3), workers=2)
    admission = TicketWorkAdmission(states, state["run_id"])
    scope = AgentWorkScope(admission)
    job = {"ticket_number": 2, "ticket_branch_generation": 1}
    previous_lease = worker_lease.get()
    with pytest.raises(worker_sandbox.WorkerExitUnknownError):
        with scope.step(job, allow_agent=True):
            scope.require("development")
            assert worker_lease.get() is not None
            raise worker_sandbox.WorkerExitUnknownError("Worker exit unknown")
    assert worker_lease.get() == previous_lease
    current = states.load_current_run(state["run_id"])
    assert current is not None
    item, = current["ticket_work_queue"]
    assert item["ticket_number"] == 2 and item["lease_id"] is not None
    with pytest.raises(TaskControlBusyError, match="already has current Agent work"):
        admission.acquire(2, 1, "development")
