"""Actual Invocation failure events must retain their Ticket owner."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize(("failed", "workers"), [(2, 1), (2, 2), (3, 2)])
def test_worker_failure_pauses_only_its_ticket_and_preserves_original_error(
    git_repo: Path, failed: int, workers: int,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=workers)
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    agents.backends[failed].data["developments"][0]["error_after_writes"] = "original Worker failure"
    snapshots = []
    states.run_saved_observer = lambda value: snapshots.append(value["status"])
    result = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    ).deliver_from_state(str(state["run_id"]), state)

    failed_job = result["ticket_jobs"][str(failed)]
    assert failed_job["phase"] == "blocked"
    assert failed_job["blocked_reason"] == "ticket_execution_failed"
    assert failed_job["execution_resume_phase"] == "developing"
    assert failed_job["execution_failure"] == "original Worker failure"
    assert result["ticket_agent_invocations"][str(failed)]["error"] == "original Worker failure"
    checkout = states.root / "worktrees" / str(state["run_id"]) / f"ticket-{failed}"
    assert (checkout / f"ticket-{failed}.txt").read_text() == "done\n"
    other = result["ticket_jobs"][str(5 - failed)]
    assert other["phase"] == "completed" and other["ticket_closed_by_run"] is True
    assert "execution_failed" not in snapshots
    require_current_run_state(states.load_current_run(str(state["run_id"])))


@pytest.mark.parametrize("target_completed", [True, False])
@pytest.mark.parametrize("new_thread", [False, True])
def test_targeted_resume_uses_target_invocation_after_result_save_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    target_completed: bool, new_thread: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    completed_number = 3 if target_completed else 2
    failed_number = 5 - completed_number
    agents.backends[failed_number].data["developments"][0]["error_after_writes"] = "Worker failed"
    original_save = states._save_run_unlocked
    interrupted = False

    def fail_result_save_once(saved_run: str, saved: dict[str, Any]) -> None:
        nonlocal interrupted
        job = saved["ticket_jobs"].get(str(completed_number), {})
        if not interrupted and job.get("phase") == "committing_candidate":
            interrupted = True
            raise OSError("temporary result persistence failure")
        original_save(saved_run, saved)

    controller = Controller(FixtureGitHubReader(fixture), git, states)
    engine = DeliveryRunEngine(
        controller=controller, tickets=ticket_engine(git, states, fixture, agents),
    )
    with monkeypatch.context() as fault:
        fault.setattr(states, "_save_run_unlocked", fail_result_save_once)
        paused = engine.deliver_from_state(run_id, state)

    assert interrupted
    assert paused["active_ticket_job"]["ticket_number"] == 2
    assert all(job["blocked_reason"] == "ticket_execution_failed" for job in paused["ticket_jobs"].values())
    assert paused["ticket_agent_invocations"][str(completed_number)]["status"] == "completed"
    assert paused["ticket_agent_invocations"][str(failed_number)]["status"] == "failed"
    assert paused["ticket_jobs"][str(completed_number)]["execution_failure"] == "temporary result persistence failure"
    pending = deepcopy(paused["ticket_jobs"]["3"]["pending_semantic_attempt"])
    other_job = deepcopy(paused["ticket_jobs"]["2"])
    other_invocation = deepcopy(paused["ticket_agent_invocations"]["2"])
    successor_thread = "replacement-3" if new_thread else "developer-3"
    agents.backends[3].data["developments"].append({
        "expected_thread_id": None if new_thread else "developer-3",
        "thread_id": successor_thread,
        "summary": "Recovered the same attempt",
        "expected_files": {"ticket-3.txt": "done\n"},
    })
    agents.backends[3].data["publications"][0]["thread_id"] = successor_thread

    resumed, _ = controller.resume(
        run_id, explicit_resume=True, ticket_number=3, new_thread=new_thread,
    )
    result = engine.deliver_from_state(run_id, resumed)

    job = result["ticket_jobs"]["3"]
    assert job["phase"] == "completed", job.get("execution_failure")
    assert job["ticket_closed_by_run"] is True
    assert job["development_thread_id"] == successor_thread
    assert job["review_budget"]["development_attempts"] == 1
    successor = [
        item for item in result["agent_invocation_history"]
        if item["work_subject"] == "ticket:3" and item["role"] == "development"
    ]
    assert len(successor) == 2
    assert successor[-1]["status"] == "completed"
    assert successor[-1]["requested_thread_id"] == (None if new_thread else "developer-3")
    assert successor[-1]["mode"] == ("new-thread" if new_thread else "resume")
    assert successor[-1]["semantic_attempt"] == pending
    attempts = [item for item in job["semantic_attempt_history"] if item["role"] == "development"]
    assert len(attempts) == 1
    assert attempts[0]["attempt_id"] == pending["attempt_id"]
    assert result["ticket_jobs"]["2"] == other_job
    assert result["ticket_agent_invocations"]["2"] == other_invocation
    require_current_run_state(states.load_current_run(run_id))
