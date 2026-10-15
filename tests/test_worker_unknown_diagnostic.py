"""A failed diagnostic must not turn unknown Worker exit into a free lease."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread, enumerate as threads
from typing import Any

import pytest

from agent_run import executor_host
from agent_run.codex import CodexCliBackend
from agent_run.state import StateStore
from agent_run.task_control import TaskControlBusyError
from agent_run.worker_control import worker_control, worker_lease
from agent_run.worker_sandbox import WorkerExitUnknownError
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run
from support.state_commits import observe_run_commits
from support.worker_process_timing import wait_until


@pytest.mark.parametrize("role", ["development", "reviewer"])
@pytest.mark.parametrize("scenario", ["unknown_save_failed", "unknown_recorded", "known_save_failed"])
def test_role_diagnostic_boundary_retains_only_unproven_worker_leases(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
    caplog: pytest.LogCaptureFixture, scenario: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,), workers=1)
    run_id = state["run_id"]
    ready = git_repo / "unknown-ready"
    script = git_repo / "unknown-worker.py"
    script.write_text(f'''import os,pathlib,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(60)
''', encoding="utf-8")
    fault, done = Event(), Event()
    children: list[subprocess.Popen[Any]] = []
    finished: list[int] = []
    errors: list[BaseException] = []
    failed_diagnostics: list[str] = []
    lease_contexts: list[str] = []
    real_popen = subprocess.Popen
    real_token = executor_host._process_start_token

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = real_popen(*args, **kwargs)
        if str(script) in args[0]:
            children.append(child)
        return child

    def save(target: str, proposed: dict[str, Any]) -> None:
        invocation = proposed.get("ticket_agent_invocations", {}).get("2", {})
        if (scenario != "unknown_recorded" and fault.is_set()
                and invocation.get("status") == "failed" and not failed_diagnostics):
            failed_diagnostics.append("failed event SQLite save rejected")
            raise OSError(failed_diagnostics[-1])

    def start_token(pid: int) -> str | None:
        if scenario != "known_save_failed" and fault.is_set() and any(child.pid == pid for child in children):
            return None
        return real_token(pid)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(executor_host, "_process_start_token", start_token)
    observe_run_commits(monkeypatch, states, before=save)
    monkeypatch.setattr("agent_run.codex.bubblewrap_command", lambda *_args, **_kwargs: [sys.executable, str(script)])
    backend = CodexCliBackend(on_worker_finished=finished.append)

    class Processes(TicketAgents):
        def invoke(self, request: dict[str, Any]) -> Any:
            check_lease = worker_control.get()
            assert check_lease is not None
            active_lease = worker_lease.get()
            assert active_lease is not None
            lease_contexts.append(active_lease)

            def check() -> None:
                if fault.is_set():
                    raise TaskControlBusyError("execution stopped")
                check_lease()

            token = worker_control.set(check)
            try:
                return backend.develop(request) if role == "development" else backend.review(request)
            finally:
                worker_control.reset(token)

        def develop(self, request: dict[str, Any]) -> Any:
            return self.invoke(request) if role == "development" else super().develop(request)

        def review(self, request: dict[str, Any]) -> Any:
            return self.invoke(request)

    engine = ticket_engine(git, states, fixture, Processes(git_repo, (2,), lambda _: None))

    def run() -> None:
        try:
            engine.deliver_ticket(run_id, 2)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    thread = Thread(target=run)
    thread.start()
    try:
        wait_until(lambda: ready.exists() and ready.read_text())

        def invocation_started() -> Any:
            current = states.load_current_run(run_id)
            invocation = current["ticket_agent_invocations"].get("2", {}) if current else {}
            return current if invocation.get("execution_segments") else None

        before = wait_until(invocation_started)
        lease, = before["ticket_work_queue"]
        assert lease["role"] == role and lease["lease_id"] is not None
        assert lease_contexts == [lease["lease_id"]]
        assert before["ticket_agent_invocations"]["2"]["deadline_seconds"] > 5
        fault.set()
        assert done.wait(5), "unknown exit waited for the normal Agent deadline"
        thread.join(1)
        assert not thread.is_alive()
        assert len(children) == 1
        assert not any(item.name.startswith(f"agent-run-worker-{children[0].pid}-") for item in threads())
        current = StateStore(states.root).load_current_run(run_id)
        assert current is not None
        if scenario == "known_save_failed":
            assert children[0].poll() == -signal.SIGKILL
            assert finished == [children[0].pid] and errors == []
            assert current["ticket_work_queue"] == []
            assert failed_diagnostics == ["failed event SQLite save rejected"]
            assert current["ticket_jobs"]["2"]["execution_failure"] == failed_diagnostics[0]
        else:
            assert children[0].poll() is None  # Observe actual survival before fallback cleanup.
            assert finished == []
            assert current["ticket_work_queue"] == [lease], "a live Worker lost its lease"
            assert len(errors) == 1 and isinstance(errors[0], WorkerExitUnknownError)
            assert "ownership/exit unknown" in str(errors[0])
            if scenario == "unknown_save_failed":
                assert failed_diagnostics == ["failed event SQLite save rejected"]
                assert failed_diagnostics[0] in " ".join(errors[0].__notes__)
                assert "Worker failure diagnostic recording failed" in caplog.text
                assert current == before  # Diagnostic rollback did not commit exit/return facts.
            else:
                assert failed_diagnostics == []
                invocation = current["ticket_agent_invocations"]["2"]
                assert invocation["status"] == "failed" and "ownership/exit unknown" in invocation["error"]
                assert invocation["execution_segments"][-1]["ended_at"] is None
                job, prior = current["ticket_jobs"]["2"], before["ticket_jobs"]["2"]
                assert job["review_budget"] == prior["review_budget"]
                assert job.get("candidate_sha") == prior.get("candidate_sha")
                assert job["pending_semantic_attempt"] == prior["pending_semantic_attempt"]
    finally:
        fault.set()
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        thread.join(5)
    assert not thread.is_alive()
