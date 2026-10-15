"""Unclassified merge failures use real reconciliation before releasing the seat."""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("boundary", ["before_merge", "after_merge", "sync_run_branch"])
def test_merge_failure_reconciles_before_sibling_delivery(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), blocked_by={4: [2]})
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2, 3, 4), lambda _: None)
    engine = ticket_engine(git, states, fixture, agents)
    development = TicketDeliveryEngine(
        git=git, states=states, github=engine.github, agents=agents, development_only=True,
    )
    development.deliver_ticket(run_id, 2)
    development.deliver_ticket(run_id, 3)
    operation = "sync_run_branch" if boundary == "sync_run_branch" else "squash_merge"
    original = getattr(engine.github, operation)
    failed = False

    def lose_response(**kwargs: Any) -> Any:
        nonlocal failed
        if failed:
            return original(**kwargs)
        failed = True
        if boundary != "before_merge":
            original(**kwargs)
        raise RuntimeError(f"controlled {boundary} failure")

    monkeypatch.setattr(engine.github, operation, lose_response)
    waiting = engine.deliver(run_id)
    job = waiting["ticket_jobs"]["2"]
    assert waiting["status"] == "waiting_external"
    assert job["phase"] == "merging"
    assert waiting["ticket_integration_holder"] == 2
    assert job["merge_intent"]["attempts"] == 1
    assert waiting["diagnostics"][0]["code"] == "merge_reconciliation_pending"
    original_job = deepcopy(job)

    # The independently developed candidate cannot write the shared Run Branch
    # before the first Ticket's merge outcome has been reconciled.
    queued = engine.deliver_ticket(run_id, 3)
    assert queued["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert queued["ticket_integration_holder"] == 2
    assert "4" not in queued["ticket_jobs"]
    assert len(json.loads(fixture.read_text())["delivery"]["pull_requests"]) == 1

    engine.deliver_ticket(run_id, 2)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    completed = DeliveryRunEngine(controller=controller, tickets=engine).deliver(run_id)
    assert all(completed["ticket_jobs"][str(n)]["phase"] == "completed" for n in (2, 3, 4))
    recovered = completed["ticket_jobs"]["2"]
    assert recovered["candidate_sha"] == original_job["candidate_sha"]
    assert recovered["review_budget"] == original_job["review_budget"]
    assert recovered["merge_intent"]["attempts"] == (2 if boundary == "before_merge" else 1)
    live = json.loads(fixture.read_text())["delivery"]
    assert sorted(live["closed_issues"]) == [2, 3, 4]
    assert len(live["pull_requests"]) == 3
    assert completed["ticket_integration_holder"] is None
    require_current_run_state(states.load_current_run(run_id))


def test_merge_sync_retry_budget_preserves_seat_and_exact_resume_phase(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None))
    original = engine.github.sync_run_branch

    def unavailable(**kwargs: Any) -> None:
        raise OSError("Run Branch sync unavailable")

    monkeypatch.setattr(engine.github, "sync_run_branch", unavailable)
    for _ in range(5):
        paused = engine.deliver(run_id)
        assert paused["ticket_integration_holder"] == 2
    job = paused["ticket_jobs"]["2"]
    assert job["phase"] == "publication_pending"
    assert job["execution_resume_phase"] == "merging"
    assert job["publication_operation_retry"] == {"attempts": 5, "limit": 5}
    assert job["merge_intent"]["attempts"] == 1
    require_current_run_state(states.load_current_run(run_id))

    monkeypatch.setattr(engine.github, "sync_run_branch", original)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    resumed, _ = controller.resume(run_id, explicit_resume=True, ticket_number=2)
    assert resumed["ticket_integration_holder"] == 2
    assert resumed["ticket_jobs"]["2"]["phase"] == "merging"
    assert "integration_resume_phase" not in resumed["ticket_jobs"]["2"]
    completed = engine.deliver(run_id)
    assert completed["ticket_jobs"]["2"]["phase"] == "completed"
    assert completed["ticket_jobs"]["2"]["publication_operation_retry_history"] == [{"attempts": 5, "limit": 5}]
    assert json.loads(fixture.read_text())["delivery"]["closed_issues"] == [2]
