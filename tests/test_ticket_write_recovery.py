"""正式写入的未知结果保留集成席位，重启先对账再继续。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery_facts import multi_ticket_progress
from agent_run.history_check_facts import checks_timeline_facts
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state_contract import require_current_run_state
from agent_run.ticket_queue import acquire, reconcile
from agent_run.ticket_state import TicketState
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("phase", ["blocked", "publication_pending"])
@pytest.mark.parametrize("action", ["publish_ticket_ref", "ensure_ticket_pr"])
def test_unresolved_write_keeps_seat_until_reconciled(phase: str, action: str) -> None:
    owner = {"ticket_number": 2, "phase": phase, "ticket_write_intent": {"action": action}}
    state: dict[str, Any] = {"ticket_integration_holder": 2, "ticket_integration_queue": []}
    reconcile(state, owner)
    sibling = {"ticket_number": 3, "phase": "accepted"}
    assert not acquire(state, sibling)
    assert state["ticket_integration_holder"] == 2
    owner.pop("ticket_write_intent")
    reconcile(state, owner)
    assert acquire(state, sibling)


@pytest.mark.parametrize("operation", ["publish_branch", "ensure_ticket_pr"])
@pytest.mark.parametrize("fault,applied", [("response", False), ("response", True), ("save", True)])
@pytest.mark.parametrize("first_ready", [2, 3])
def test_resume_reconciles_write_before_sibling_integration(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str, applied: bool,
    first_ready: int,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    publications: list[int] = []
    agents = TicketAgents(git_repo, (2, 3), lambda _: None, publications.append)
    sibling = 3 if first_ready == 2 else 2
    # This test targets write recovery after both candidates are ready.
    # Establish each legal FIFO order through the real development stages;
    # concurrent completion does not promise that Ticket 2 is ready first.
    development = ticket_engine(git, states, fixture, agents, development_only=True)
    development.deliver_ticket(run_id, first_ready)
    ready = development.deliver_ticket(run_id, sibling)
    assert [item["ticket_number"] for item in ready["ticket_integration_queue"]] == [first_ready, sibling]
    assert ready["ticket_integration_holder"] is None
    assert all(ready["ticket_jobs"][str(number)]["phase"] == "ready_for_integration" for number in (2, 3))
    engine = ticket_engine(git, states, fixture, agents)
    original = getattr(engine.github, operation)
    wrote = False

    def lose_response(*args: Any, **kwargs: Any) -> Any:
        nonlocal wrote
        if applied:
            result = original(*args, **kwargs)
            wrote = True
            if fault == "save":
                return result
        raise RuntimeError("response lost; remote result must be observed")

    monkeypatch.setattr(engine.github, operation, lose_response)
    original_save = states._save_work_and_notify_unlocked
    injected = False

    def save_after_write(identifier: str, current: dict[str, Any], baseline: dict[str, Any], **kwargs: Any) -> None:
        nonlocal injected
        job = current["ticket_jobs"][str(first_ready)]
        if fault == "save" and wrote and not injected and "ticket_write_intent" not in job:
            injected = True
            raise OSError("remote write applied before StateStore result save")
        original_save(identifier, current, baseline, **kwargs)

    monkeypatch.setattr(states, "_save_work_and_notify_unlocked", save_after_write)
    engine.deliver_ticket(run_id, first_ready)
    assert injected == (fault == "save")
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    before = deepcopy(interrupted["ticket_jobs"][str(first_ready)])
    assert before["phase"] == "blocked"
    assert before["ticket_write_intent"]["action"] == (
        "publish_ticket_ref" if operation == "publish_branch" else "ensure_ticket_pr"
    )
    assert interrupted["ticket_integration_holder"] == first_ready
    require_current_run_state(interrupted)
    unchanged = deepcopy(interrupted)
    progress = multi_ticket_progress(interrupted, executor_activity="not_running")
    assert progress is not None
    progress_job = next(item for item in progress["tickets"] if item["ticket_number"] == first_ready)
    assert progress_job["holds_integration"] is True
    assert progress_job["pending_write"]["head_sha"] == before["publication_sha"]
    assert progress_job["recovery_action"] is not None
    history_state = TicketState(states, run_id, first_ready).load_current_run(run_id)
    assert history_state is not None
    facts = checks_timeline_facts(history_state, {"kind": "ticket"})
    assert facts["github_write_action"] == before["ticket_write_intent"]["action"]
    assert interrupted == unchanged

    # The second completed Development can wait, but cannot mutate the target.
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, sibling)
    queued = states.load_current_run(run_id)
    assert queued is not None and queued["ticket_integration_holder"] == first_ready
    assert queued["ticket_jobs"][str(sibling)]["phase"] == "ready_for_integration"
    assert git.resolve(state["run_branch"]) == state["base"]["sha"]

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True, ticket_number=first_ready,
    )
    assert resumed["ticket_integration_holder"] == first_ready
    restarted = ticket_engine(git, states, fixture, agents)
    completed = restarted.deliver_ticket(run_id, first_ready)
    job = completed["ticket_jobs"][str(first_ready)]
    assert job["phase"] == "completed", completed.get("diagnostics")
    assert completed["ticket_integration_holder"] is None
    for key in ("candidate_sha", "publication_sha", "acceptance_record", "review_budget"):
        assert job[key] == before[key]
    assert job["publication_attempts"] == 1
    assert "ticket_write_intent" not in job
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["closed_issues"] == [first_ready]
    assert publications == [first_ready]
    assert [item["action"] for item in delivery["mutations"]].count("close_issue") == 1
    require_current_run_state(completed)
