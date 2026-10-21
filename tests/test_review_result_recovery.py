"""Narrow readback failures complement persisted delivery recovery tests."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from agent_run.development_review import DevelopmentReview
from agent_run.change_delivery import ChangeDeliveryEngine
from agent_run.change_delivery_contracts import ChangeJobContract
from agent_run.change_delivery_state import ChangeDeliveryStateStore
from agent_run.delivery_step import DeliveryStepKind
from agent_run.github import GitHubReadError
from agent_run.review_attempt import begin_review, consume_review_result
from agent_run.review_budget import TICKET_POLICY, new_budget
from agent_run.review_context import prepare_review_context
from support.run_artifacts import _passing_artifact


def test_candidate_status_read_failure_keeps_the_returned_reviewer(tmp_path: Path) -> None:
    job = {
        "base_sha": "base", "candidate_sha": "candidate", "phase": "reviewing",
        "pr_number": 11, "review_budget": new_budget(), "review_budget_history": [],
        "ticket_number": 3, "ticket_branch": "ticket/3",
        "effective_revision": "revision",
    }
    attempt = begin_review(
        job, work_subject="ticket-3", generation=1,
        boundary={"candidate_sha": "candidate"}, budget_window=1,
    )
    prepare_review_context(
        {"acceptance_scope": "ticket", "current_review_identity": {
            "reviewed_base_sha": "base", "reviewed_candidate_sha": "candidate",
            "reviewed_candidate_tree": "tree",
        }}, attempt, job, SimpleNamespace(resolve=lambda ref: ref), {},
    )
    artifact = consume_review_result(job, TICKET_POLICY, _passing_artifact())
    job["pending_review_result"] = {"reviewer_thread_id": "reviewer", "artifact": artifact.raw}
    state = {"run_id": "review-run", "run_branch": "run", "parent": {"number": 1},
             "active_ticket_job": job, "ticket_integration_queue": [], "ticket_integration_holder": None}

    status_reads: list[int] = []

    def status_read_failure(number, *_args, **_kwargs):
        status_reads.append(number)
        raise GitHubReadError("github_read_failed", "status readback unavailable")

    github = SimpleNamespace(
        record_agent_run_status=status_read_failure,
        current_effective_revision=lambda **_kwargs: "revision",
    )

    def review_engine():
        git = SimpleNamespace(resolve=lambda _ref: "tree")
        states = SimpleNamespace(save_run=lambda *_args: None,
                                 ticket_pause_requested=lambda *_args: False)
        work = DevelopmentReview(git=git, github=github, agents=None, states=states)
        return ChangeDeliveryEngine(
            git=git, github=github, agents=None,
            contract=ChangeJobContract(label="ticket-3", branch="ticket/3", base_branch="run",
                                       review_budget_policy=TICKET_POLICY),
            adapter=SimpleNamespace(), publisher=SimpleNamespace(),
            state_store=ChangeDeliveryStateStore(states, "review-run"), work=work,
        )

    step = review_engine().advance(state, job, tmp_path)
    assert step.kind == DeliveryStepKind.WAITING
    assert status_reads == [11]
    assert state["external_wait"]["code"] == "github_read_failed"
    # Failure recording may persist the mutated in-memory state. That snapshot
    # must retain the answer and its Attempt until remote readback succeeds.
    reloaded = deepcopy(state)
    pending = reloaded["active_ticket_job"]
    assert pending["phase"] == "reviewing"
    assert pending["pending_review_result"]["artifact"] == artifact.raw
    assert pending["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    github.record_agent_run_status = lambda *_args, **_kwargs: None
    review_engine().advance(reloaded, pending, tmp_path)
    assert pending["phase"] == "accepted"
    assert "pending_review_result" not in pending
    assert "pending_semantic_attempt" not in pending
    assert pending["review_budget"]["reviewer_invocations"] == 1
    assert len(pending["review_budget"]["review_artifacts"]) == 1
    assert pending["semantic_attempt_history"][-1]["attempt_id"] == attempt["attempt_id"]
