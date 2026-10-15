from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.ci_recovery_budget import new_ticket_ci_recovery_budget
from agent_run.delivery_loop import TicketDeliveryLoop
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.executor_host import _process_start_token
from agent_run.models import DeliveryGraph
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.review_budget import new_budget
from agent_run.ticket_eligibility import TicketEligibilityError
from agent_run.task_control import TaskControlStore, TaskKey
from support.seeding import write_fixture
from support.delivery_agents import PassAgents, issue
from support.state import persist_run_fixture


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
        "ci_recovery": new_ticket_ci_recovery_budget(),
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
        "phase": "developing", "review_budget": new_budget(), "review_budget_history": [], "ci_recovery": new_ticket_ci_recovery_budget(),
    })
    persist_run_fixture(states, state["run_id"], state)
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
        "phase": "developing", "review_budget": new_budget(), "review_budget_history": [], "ci_recovery": new_ticket_ci_recovery_budget(),
    })
    if command == "requeue":
        job.update({
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{state['run_id']}/ticket-3",
            "effective_revision": "stale",
            "base_sha": git.resolve(state["run_branch"]),
        })
    persist_run_fixture(states, state["run_id"], state)
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


@pytest.mark.parametrize("failure_at", ["qualification", "refresh"])
def test_resume_retry_reuses_only_successful_action_label_qualification(
    git_repo: Path, failure_at: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})

    class InterruptedReader(FixtureGitHubReader):
        reads_until_failure: int | None = None

        def delivery_graph(self, parent_number: int) -> DeliveryGraph:
            if self.reads_until_failure is not None:
                self.reads_until_failure -= 1
                if self.reads_until_failure == 0:
                    self.reads_until_failure = None
                    raise GitHubReadError("github_timeout", "temporary graph outage")
            return super().delivery_graph(parent_number)

    reader = InterruptedReader(fixture)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(reader, GitRepository(git_repo), states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    state["active_ticket_job"].update({
        "phase": "developing", "review_budget": new_budget(), "review_budget_history": [], "ci_recovery": new_ticket_ci_recovery_budget(),
    })
    persist_run_fixture(states, run_id, state)
    controller.record_execution_failure(run_id, "interrupted")
    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, "example/project", 1)
    claim = control.claim_action(
        task, kind="resume", payload={"run_id": run_id, "message": "first intent"},
    )
    assert claim.action is not None and claim.action_id is not None
    action = claim.action
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
    control.bind_run(task, claim.action_id, run_id, state_dir=states.root)
    control.mark_handshake(
        task, action_id=claim.action_id, generation=reservation.generation,
        pid=os.getpid(), process_start_token=_process_start_token(os.getpid()),
    )
    reader.reads_until_failure = 1 if failure_at == "qualification" else 2
    waiting, _ = controller.resume(
        run_id, explicit_resume=True,
        prepare_state=lambda current: prepare_action_application_receipt(current, action),
    )
    assert waiting["status"] == "waiting_external"
    data = json.loads(fixture.read_text())
    data["issues"]["3"]["labels"] = ["ready-for-human"]
    fixture.write_text(json.dumps(data))
    before = deepcopy(states.load_run(run_id))

    if failure_at == "qualification":
        with pytest.raises(TicketEligibilityError):
            controller.resume(run_id, explicit_resume=True, record_explicit_resume_audit=False)
        assert states.load_run(run_id) == before
        return

    continued, _ = controller.resume(
        run_id, explicit_resume=True, record_explicit_resume_audit=False,
    )
    assert continued["status"] == "active"
    assert continued["active_ticket_job"]["ticket_number"] == 3
    before = deepcopy(states.load_run(run_id))
    # A new explicit Action must qualify again, even for the same Ticket.
    control.complete_action(
        task, action_id=claim.action_id, generation=reservation.generation,
        result_status=continued["status"],
    )
    control.finish_executor(task, action_id=claim.action_id, generation=reservation.generation)
    successor = control.claim_action(
        task, kind="resume", payload={"run_id": run_id, "message": "new intent"},
    )
    assert successor.action is not None and successor.action_id is not None
    control.begin_executor(task, action_id=successor.action_id, run_id=run_id)
    control.bind_run(task, successor.action_id, run_id, state_dir=states.root)
    action = successor.action
    with pytest.raises(TicketEligibilityError):
        controller.resume(
            run_id, explicit_resume=True,
            prepare_state=lambda current: prepare_action_application_receipt(current, action),
        )
    assert states.load_run(run_id) == before
