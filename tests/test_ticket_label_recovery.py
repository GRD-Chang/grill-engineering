from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery_loop import TicketDeliveryLoop
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.state import StateStore
from agent_run.review_budget import new_budget
from agent_run.ticket_eligibility import TicketEligibilityError
from conftest import write_fixture
from test_delivery import PassAgents, issue


@pytest.mark.parametrize(
    "reason", ["modification_budget_exhausted", "git_integrity_boundary_restore_failed"]
)
def test_ticket_escalation_preserves_github_labels(git_repo: Path, reason: str) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    job = state["active_ticket_job"]
    job.update({
        "phase": "escalating",
        "escalation_code": reason,
        "ticket_branch": f"{state['run_branch']}-ticket-3",
        "policy_snapshot": state["policy_snapshot"],
        "review_budget": new_budget(),
        "review_budget_history": [],
    })
    checkout = git_repo / "unused-checkout"
    result = TicketDeliveryLoop(
        git=git, states=states,
        github=FixtureGitHubPublisher(fixture, git), agents=PassAgents(checkout),
    ).run(state, job, checkout)

    assert result["status"] == "blocked"
    assert result["active_ticket_job"]["blocked_reason"] == reason
    assert json.loads(fixture.read_text())["issues"]["3"]["labels"] == ["ready-for-agent"]
    persisted = states.load_run(state["run_id"])
    assert persisted is not None
    assert persisted["active_ticket_job"]["phase"] == "blocked"


def test_running_ticket_keeps_its_place_when_labels_change(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"3": issue(3), "4": issue(4)}
    )
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    state["active_ticket_job"].update({
        "phase": "developing", "review_budget": new_budget(), "review_budget_history": [],
    })
    states.save_run(state["run_id"], state)
    data = json.loads(fixture.read_text())
    data["issues"]["3"]["labels"] = ["ready-for-human"]
    fixture.write_text(json.dumps(data))

    continued, _ = controller.start_or_resume_unfinished(1)

    assert continued["active_ticket_job"]["ticket_number"] == 3
    assert continued["active_ticket_job"]["phase"] == "developing"
    assert continued["status"] == "active"


@pytest.mark.parametrize("command", ["resume", "requeue"])
@pytest.mark.parametrize("labels", [["ready-for-human"], [], ["ready-for-agent", "needs-info"]])
def test_explicit_entry_rejects_labels_without_consuming_work(
    git_repo: Path, command: str, labels: list[str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    job = state["active_ticket_job"]
    job.update({
        "phase": "developing", "review_budget": new_budget(), "review_budget_history": [],
    })
    if command == "requeue":
        job.update({
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{state['run_id']}/ticket-3",
            "effective_revision": "stale",
            "base_sha": git.resolve(state["run_branch"]),
        })
    states.save_run(state["run_id"], state)
    if command == "resume":
        controller.record_execution_failure(state["run_id"], "controller_interrupted")
    else:
        stale, _ = controller.resume(state["run_id"])
        assert stale["status"] == "requeue_required"
    before = deepcopy(states.load_run(state["run_id"]))
    data = json.loads(fixture.read_text())
    data["issues"]["3"]["labels"] = labels
    fixture.write_text(json.dumps(data))

    with pytest.raises(TicketEligibilityError, match="Ticket #3.*标签不允许"):
        if command == "resume":
            controller.resume(state["run_id"], explicit_resume=True)
        else:
            controller.requeue(state["run_id"])

    assert states.load_run(state["run_id"]) == before


def test_new_ticket_is_not_started_without_ready_label(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"3": {**issue(3), "labels": []}}
    )
    states = StateStore(git_repo / ".agent-run")
    result, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    assert result["active_ticket_job"] is None
    assert result["ticket_jobs"] == {}
    assert result["status"] == "progress_exhausted"
