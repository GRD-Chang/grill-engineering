"""Public Ticket Stop through real Worker children and persistent checkouts."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event, Thread
import time
from typing import Any, Callable

import pytest

from agent_run.codex import CodexCliBackend
from agent_run.controller import Controller, apply_resume_to_state
from agent_run.delivery_policy import parse_policy_snapshot
from agent_run.delivery_status import status_progress_view
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from support.cli import run_cli, stdout_json
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state


def _until(predicate: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Ticket stop did not reach the expected boundary")


def test_public_stop_kills_only_owned_worker_cancels_queue_and_resumes_attempt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={
        str(n): {"number": n, "title": f"Ticket {n}", "body": f"Implement {n}.",
                 "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": []}
        for n in (2, 3, 4)
    })
    seeded = seed_run(git_repo, fixture, "1", "--ticket-development-workers", "2", idle_control=True)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    run_id = stdout_json(seeded)["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    state = states.load_current_run(run_id)
    assert state is not None
    script = git_repo / "worker.py"
    script.write_text('''import json, os, pathlib, signal, sys, time
number, output, root = sys.argv[1:]
root = pathlib.Path(root)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"type": "thread.started", "thread_id": "developer-" + number}), flush=True)
pathlib.Path("draft.txt").write_text("preserved " + number)
(root / (number + ".pid")).write_text(str(os.getpid()))
while not (root / (number + ".release")).exists():
    time.sleep(0.01)
pathlib.Path(output).write_text(json.dumps({"result_kind": "development", "summary": "continued", "human_blockers": None}))
''', encoding="utf-8")
    starts: list[int] = []

    def command(arguments: list[str], *, checkout: Path, **_options: Any) -> list[str]:
        number = int(checkout.name.removeprefix("ticket-"))
        starts.append(number)
        output = arguments[arguments.index("--output-last-message") + 1]
        return [sys.executable, str(script), str(number), output, str(git_repo)]

    monkeypatch.setattr("agent_run.codex.bubblewrap_command", command)
    exited, finish = Event(), Event()

    def worker_finished(pid: int) -> None:
        pid_file = git_repo / "2.pid"
        if pid_file.exists() and pid == int(pid_file.read_text()) and not finish.is_set():
            exited.set()
            assert finish.wait(10), "test did not release Worker closeout"

    backend = CodexCliBackend(on_worker_finished=worker_finished)

    class Processes(TicketAgents):
        def develop(self, request: dict[str, Any]) -> Any:
            return backend.develop(request)

    agents = Processes(git_repo, (2, 3, 4), lambda _: None)
    engine = ticket_engine(git, states, fixture, agents)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            engine.develop_frontier(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    pids: dict[int, int] = {}
    try:
        for number in (2, 3):
            _until(lambda n=number: (git_repo / f"{n}.pid").exists())
            pids[number] = int((git_repo / f"{number}.pid").read_text())
        before = states.load_current_run(run_id)
        assert before is not None
        invocation = before["ticket_agent_invocations"]["2"]
        assert invocation["reported_thread_id"] == "developer-2"
        attempt_id = invocation["semantic_attempt"]["attempt_id"]
        usage = before["ticket_jobs"]["2"]["review_budget"].copy()
        # Ticket 4 has not started any Agent and may not have a Job yet.
        stopped_queue = run_cli(git_repo, fixture, "stop", "1", "--ticket", "4", "--json")
        assert stopped_queue.returncode == 0, stopped_queue.stdout + stopped_queue.stderr
        assert 4 not in starts
        queued = states.load_current_run(run_id)
        assert queued is not None
        assert all(item["ticket_number"] != 4 for item in queued["ticket_work_queue"])
        stopped = run_cli(git_repo, fixture, "stop", "1", "--ticket", "2", "--json")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr

        assert exited.wait(10), "production Stop did not exit the target Worker"
        with pytest.raises(ProcessLookupError):
            os.kill(pids[2], 0)
        os.kill(pids[3], 0)
        closing = states.load_current_run(run_id)
        assert closing is not None
        assert {item["ticket_number"] for item in closing["ticket_work_queue"] if item["lease_id"]} == {2, 3}
        progress = status_progress_view(closing, {})
        details = progress["multi_ticket"]["tickets"]
        assert next(item for item in details if item["ticket_number"] == 2)["stop_wait_reason"] == "agent_exit"
        finish.set()

        def target_paused() -> dict[str, Any] | None:
            saved = states.load_current_run(run_id)
            assert saved is not None
            return saved if (
                saved["ticket_jobs"]["2"].get("blocked_reason") == "ticket_paused"
                and not any(item["ticket_number"] == 2 for item in saved["ticket_work_queue"])
            ) else None

        paused = _until(target_paused)
        # These assertions precede fallback cleanup: production Stop must kill.
        with pytest.raises(ProcessLookupError):
            os.kill(pids[2], 0)
        os.kill(pids[3], 0)
        assert paused["status"] != "operator_stopped"
        assert [item["ticket_number"] for item in paused["ticket_work_queue"] if item["lease_id"]] == [3]
        checkout = states.root / "worktrees" / run_id / "ticket-2"
        assert (checkout / "draft.txt").read_text() == "preserved 2"
        assert paused["ticket_jobs"]["2"]["review_budget"] == usage
        assert paused["ticket_agent_invocations"]["2"]["semantic_attempt"]["attempt_id"] == attempt_id
        repeated = run_cli(git_repo, fixture, "stop", "1", "--ticket", "2", "--json")
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        (git_repo / "3.release").touch()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not errors, errors
        final = states.load_current_run(run_id)
        assert final is not None
        assert final["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
        assert final["ticket_jobs"]["4"]["blocked_reason"] == "ticket_paused"
        assert starts == [2, 3] or starts == [3, 2]
        assert final["ticket_work_queue"] == []

        # Both active and inactive Resume use this transition. Check the
        # explicit new-thread option without creating another Worker process.
        new_thread_state = deepcopy(final)
        apply_resume_to_state(
            new_thread_state, explicit_resume=True, resume_human_blocker=False,
            new_thread=True, human_response=None, resume_budget_checkpoint=False,
            budget_policy=parse_policy_snapshot(final["policy_snapshot"]),
            ticket_number=2, source_status=str(final["status"]),
        )
        new_thread_job = new_thread_state["ticket_jobs"]["2"]
        assert new_thread_job["development_new_thread"] is True
        assert new_thread_job.get("development_thread_id") is None
        assert "developer-2" in new_thread_job["development_thread_history"]
        assert new_thread_job["review_budget"] == usage
        assert new_thread_state["active_agent_invocation"]["semantic_attempt"]["attempt_id"] == attempt_id

        resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
            run_id, explicit_resume=True, ticket_number=2,
        )
        assert resumed["ticket_jobs"]["2"]["development_thread_id"] == "developer-2"
        (git_repo / "2.release").touch()
        ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
        recovered = states.load_current_run(run_id)
        assert recovered is not None
        assert recovered["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
        development = [entry for entry in recovered["agent_invocation_history"]
                       if entry["work_subject"] == "ticket:2" and entry["role"] == "development"]
        assert len(development) == 2
        assert {entry["semantic_attempt"]["attempt_id"] for entry in development} == {attempt_id}
        assert recovered["ticket_jobs"]["2"]["review_budget"]["development_attempts"] == usage["development_attempts"]
        assert recovered["ticket_jobs"]["4"]["blocked_reason"] == "ticket_paused"
    finally:
        finish.set()
        for number in (2, 3, 4):
            (git_repo / f"{number}.release").touch()
        thread.join(timeout=10)
        # Only failed assertions may require this fallback; never evidence of Stop.
        for pid in pids.values():
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not thread.is_alive()
