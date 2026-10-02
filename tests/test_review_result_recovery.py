"""Narrow readback failures complement persisted delivery recovery tests."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.change_delivery_review import complete_review
from agent_run.github import GitHubReadError
from agent_run.review_attempt import begin_review, consume_review_result
from agent_run.review_budget import TICKET_POLICY, new_budget
from support.run_artifacts import _passing_artifact


def test_candidate_status_read_failure_keeps_the_returned_reviewer(tmp_path: Path) -> None:
    job = {
        "base_sha": "base", "candidate_sha": "candidate", "phase": "reviewing",
        "pr_number": 11, "review_budget": new_budget(), "review_budget_history": [],
    }
    attempt = begin_review(
        job, work_subject="ticket-3", generation=1,
        boundary={"candidate_sha": "candidate"}, budget_window=1,
    )
    artifact = consume_review_result(job, TICKET_POLICY, _passing_artifact())
    job["pending_review_result"] = {"reviewer_thread_id": "reviewer", "artifact": artifact.raw}
    state = {"active_ticket_job": job}

    def status_read_failure(*_args, **_kwargs):
        raise GitHubReadError("github_read_failed", "status readback unavailable")

    stage = SimpleNamespace(
        contract=SimpleNamespace(label="ticket-3"),
        git=SimpleNamespace(resolve=lambda _ref: "tree"),
        adapter=SimpleNamespace(acceptance_record=lambda *_args: {"artifact": artifact.raw}),
        review_budget_policy=lambda: TICKET_POLICY,
        _reject_stale=lambda *_args: None,
        _record_agent_run_status=status_read_failure,
        save=lambda current: current,
    )
    with pytest.raises(GitHubReadError, match="status readback unavailable"):
        complete_review(stage, state, job, tmp_path)
    # Failure recording may persist the mutated in-memory state. That snapshot
    # must retain the answer and its Attempt until remote readback succeeds.
    reloaded = deepcopy(state)
    pending = reloaded["active_ticket_job"]
    assert pending["phase"] == "reviewing"
    assert pending["pending_review_result"]["artifact"] == artifact.raw
    assert pending["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    stage._record_agent_run_status = lambda *_args, **_kwargs: None
    complete_review(stage, reloaded, pending, tmp_path)
    assert pending["phase"] == "accepted"
    assert "pending_review_result" not in pending
    assert "pending_semantic_attempt" not in pending
    assert pending["review_budget"]["reviewer_invocations"] == 1
    assert len(pending["review_budget"]["review_artifacts"]) == 1
    assert pending["semantic_attempt_history"][-1]["attempt_id"] == attempt["attempt_id"]
