"""Narrow readback failures complement persisted delivery recovery tests."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.agent_work import AgentWorkScope
from agent_run.change_delivery_contracts import ChangeJobContract
from agent_run.change_delivery_review import ChangeReview
from agent_run.github import GitHubReadError
from agent_run.review_attempt import begin_review, consume_review_result
from agent_run.review_budget import TICKET_POLICY, new_budget
from agent_run.review_context import prepare_review_context
from support.run_artifacts import _passing_artifact


def test_candidate_status_read_failure_keeps_the_returned_reviewer(tmp_path: Path) -> None:
    job = {
        "base_sha": "base", "candidate_sha": "candidate", "phase": "reviewing",
        "pr_number": 11, "review_budget": new_budget(), "review_budget_history": [],
        "ticket_number": 3,
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
    state = {"active_ticket_job": job, "ticket_integration_queue": [], "ticket_integration_holder": None}

    def status_read_failure(*_args, **_kwargs):
        raise GitHubReadError("github_read_failed", "status readback unavailable")

    github = SimpleNamespace(record_agent_run_status=status_read_failure)

    def review_work(current):
        return ChangeReview(
            state=current, job=current["active_ticket_job"], checkout=tmp_path,
            contract=ChangeJobContract(label="ticket-3", branch="ticket/3", base_branch="run",
                                       review_budget_policy=TICKET_POLICY),
            git=SimpleNamespace(resolve=lambda _ref: "tree"), github=github, agents=None,
            adapter=SimpleNamespace(
                acceptance_record=lambda *_args, **_kwargs: {"artifact": artifact.raw},
                revision_changed=lambda *_args: False,
            ),
            publisher=SimpleNamespace(), state_store=SimpleNamespace(save=lambda value: value),
            agent_work=AgentWorkScope(), development_only=False,
        )

    with pytest.raises(GitHubReadError, match="status readback unavailable"):
        review_work(state).complete()
    # Failure recording may persist the mutated in-memory state. That snapshot
    # must retain the answer and its Attempt until remote readback succeeds.
    reloaded = deepcopy(state)
    pending = reloaded["active_ticket_job"]
    assert pending["phase"] == "reviewing"
    assert pending["pending_review_result"]["artifact"] == artifact.raw
    assert pending["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    github.record_agent_run_status = lambda *_args, **_kwargs: None
    review_work(reloaded).complete()
    assert pending["phase"] == "accepted"
    assert "pending_review_result" not in pending
    assert "pending_semantic_attempt" not in pending
    assert pending["review_budget"]["reviewer_invocations"] == 1
    assert len(pending["review_budget"]["review_artifacts"]) == 1
    assert pending["semantic_attempt_history"][-1]["attempt_id"] == attempt["attempt_id"]
