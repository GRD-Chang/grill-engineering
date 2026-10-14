"""Shared persistence loss must cancel ready Workers before their deadlines."""
from __future__ import annotations

from contextvars import copy_context
from copy import deepcopy
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from threading import Event, Thread, enumerate as threads
import time
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend
from agent_run.controller import Controller
from agent_run.executor import DeliveryExecutor
from agent_run.executor_admission import (
    check_executor_cancelled, close_executor_admission, executor_admission_scope,
    executor_cancellation_scope,
)
from agent_run.executor_host import _process_start_token
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from agent_run.task_control import TaskControlBusyError, TaskControlStore, TaskKey
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run
from support.worker_process_timing import wait_until


def test_shared_save_and_diagnostic_loss_stops_review_and_development(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    state, states, git, fixture = ticket_run(
        git_repo, (2, 3, 4, 5), workers=2, blocked_by={5: [3]},
    )
    run_id = state["run_id"]
    agents = TicketAgents(git_repo, (2, 3, 4, 5), lambda _: None)
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.resume(run_id)
    completed = deepcopy(state["ticket_jobs"]["2"])
    assert completed["phase"] == "completed"

    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    action_id = claim.action_id
    assert action_id is not None
    generation = control.begin_executor(task, action_id=action_id, run_id=run_id).generation
    identity = dict(action_id=action_id, generation=generation)
    control.bind_run(task, action_id, run_id, generation=generation)
    control.mark_handshake(task, **identity, pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    control.record_application(task, **identity, run_id=run_id, payload_digest=claim.action["payload_digest"])
    control.complete_action(task, **identity, result_status="active")

    script = git_repo / "held-worker.py"
    script.write_text('''import os, pathlib, signal, sys, time
number, root = sys.argv[1:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if number == "4":
    pathlib.Path("retained-draft.txt").write_text("draft " + number)
(pathlib.Path(root) / (number + ".ready")).write_text(str(os.getpid()))
while True:
    time.sleep(1)
''', encoding="utf-8")
    starts: list[int] = []
    processes: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = real_popen(*args, **kwargs)
        if str(script) in args[0]:
            processes.append(child)
        return child

    def command(arguments: list[str], *, checkout: Path, **_options: Any) -> list[str]:
        number = int(checkout.name.split("ticket-", 1)[1].split("-", 1)[0])
        starts.append(number)
        return [sys.executable, str(script), str(number), str(git_repo)]

    monkeypatch.setattr("agent_run.codex.bubblewrap_command", command)
    monkeypatch.setattr(subprocess, "Popen", popen)
    backend = CodexCliBackend(
        on_worker_started=lambda pid: control.mark_worker_started(
            task, **identity, pid=pid, process_start_token=_process_start_token(pid),
        ),
        on_worker_finished=lambda pid: control.mark_worker_finished(
            task, **identity, pid=pid, process_start_token=tokens[pid],
        ),
    )

    class Processes(TicketAgents):
        def develop(self, request: dict[str, Any]) -> Any:
            if self._number(request) == 4:
                return backend.develop(request)
            return super().develop(request)

        def review(self, request: dict[str, Any]) -> Any:
            return backend.review(request)

    engine = ticket_engine(git, states, fixture, Processes(git_repo, (3, 4, 5), lambda _: None))
    fault, exited = Event(), Event()
    errors: list[BaseException] = []
    tokens: dict[int, str] = {}
    failed_commits: list[str] = []
    failed_control: list[str] = []
    real_connect = sqlite3.connect
    real_write = control._write_unlocked

    class FailingConnection(sqlite3.Connection):
        def commit(self) -> None:
            if fault.is_set():
                failed_commits.append("shared commit")
                raise sqlite3.OperationalError("database or disk is full")
            super().commit()

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if database == states.root / "state.sqlite3":
            kwargs["factory"] = FailingConnection
        return real_connect(database, *args, **kwargs)

    def write(target: TaskKey, record: Any) -> None:
        if fault.is_set():
            failed_control.append("control write")
            raise OSError("control storage unavailable")
        real_write(target, record)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(control, "_write_unlocked", write)

    def supervise(_lanes: frozenset[int]) -> None:
        if fault.wait(10):
            controller.record_execution_failure(run_id, "shared state save failed")
        else:
            raise AssertionError("test did not reach shared storage fault")

    def close(error: BaseException | None) -> None:
        control.close_ticket_resume_admission(
            task, **identity, run_id=run_id, can_exit=lambda: True, failure=str(error),
        )

    def run() -> None:
        try:
            with executor_admission_scope(close):
                DeliveryExecutor(
                    states=states,
                    driver_factory=lambda *_: SimpleNamespace(advance=lambda _: engine.develop_frontier(
                        run_id, state, supervise=supervise,
                    )),
                    record_execution_failure=lambda _states, target, message, _binding: controller.record_execution_failure(target, message),
                ).execute(run_id, action_id=action_id, generation=generation)
        except BaseException as error:
            errors.append(error)
        finally:
            exited.set()

    unrelated = real_popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True)
    thread = Thread(target=run, name="storage-stop-executor")
    thread.start()
    try:
        pids = {
            number: int(wait_until(lambda number=number: (git_repo / f"{number}.ready").read_text()
                                  if (git_repo / f"{number}.ready").exists() else None))
            for number in (3, 4)
        }
        def started_invocations() -> Any:
            current = states.load_current_run(run_id)
            if current is None:
                return None
            invocations = current["ticket_agent_invocations"]
            return current if all(invocations.get(str(number), {}).get("execution_segments") for number in (3, 4)) else None

        before = wait_until(started_invocations)
        assert before is not None
        assert before["ticket_agent_invocations"]["3"]["semantic_attempt"]["role"] == "reviewer"
        assert before["ticket_agent_invocations"]["4"]["role"] == "development"
        assert all(before["ticket_agent_invocations"][str(number)]["deadline_seconds"] > 6 for number in (3, 4))
        saved_control = control.load(task)
        tokens.update({worker["pid"]: worker["process_start_token"] for worker in saved_control["executor"]["workers"]})
        assert set(tokens) == set(pids.values())
        assert all(_process_start_token(pid) == token for pid, token in tokens.items())
        started_threads = {item for item in threads() if item.name.startswith(("ticket_", "agent-run-worker-"))}
        started = time.monotonic()
        fault.set()
        assert exited.wait(5), "ready Workers were not cancelled before their normal deadline"
        thread.join(1)
        assert not thread.is_alive()
        assert time.monotonic() - started < 6
        assert processes and all(process.poll() == -signal.SIGKILL for process in processes)
        assert all(not item.is_alive() for item in started_threads)
        for pid in pids.values():
            with pytest.raises(ProcessLookupError):
                os.killpg(pid, 0)
        assert unrelated.poll() is None
        assert sorted(starts) == [3, 4]
        assert len(failed_commits) >= 2 and failed_control
        assert len(errors) == 1 and "database or disk is full" in str(errors[0])
        assert "durable failure/exit unknown" in caplog.text
        assert "durable admission unknown" in caplog.text
        reloaded = StateStore(states.root).load_current_run(run_id)
        assert reloaded == before  # No uncommitted failure or return is claimed.
        assert control.load(task) == saved_control
        assert reloaded["ticket_jobs"]["2"] == completed
        checkout_root = states.root / "worktrees" / run_id
        assert (checkout_root / "ticket-3" / "ticket-3.txt").read_text() == "done\n"
        assert (checkout_root / "ticket-4" / "retained-draft.txt").read_text() == "draft 4"
    finally:
        fault.set()
        for child in [*processes, unrelated]:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        thread.join(10)
    assert not thread.is_alive()


def test_cancellation_is_scoped_to_the_exact_execution_context() -> None:
    with executor_cancellation_scope():
        older = copy_context()
    with executor_cancellation_scope():
        current = copy_context()
        close_executor_admission()
        with pytest.raises(TaskControlBusyError, match="Executor stopped"):
            current.run(check_executor_cancelled)
        older.run(check_executor_cancelled)
    check_executor_cancelled()
