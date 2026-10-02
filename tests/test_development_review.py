"""Low-cost outcome combinations complement the real delivery paths."""
from __future__ import annotations

from copy import deepcopy

import pytest

from agent_run.artifacts import AcceptanceArtifact
from agent_run.development_review import ReviewDecision, after_review, review_is_due
from agent_run.review_budget import (
    PARENT_ONLY_POLICY, RUN_POLICY, TICKET_POLICY, new_budget,
)
from agent_run.review_attempt import (
    begin_review, consume_review_result, finish_review_result, remember_review,
)
from support.run_artifacts import _human_artifact, _passing_artifact, _repair_artifact


@pytest.mark.parametrize("policy", [TICKET_POLICY, PARENT_ONLY_POLICY, RUN_POLICY])
@pytest.mark.parametrize("outcome", ["pass", "findings", "blocked"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_review_outcomes_keep_parent_review_and_ticket_fallback_boundaries(
    policy, outcome: str, exhausted: bool,
) -> None:
    job = {
        "review_budget": new_budget(
            reviewer_invocations=policy.review_limit if exhausted else 1,
            development_attempts=policy.development_limit - 1 if exhausted else 0,
        ),
        "review_budget_history": [],
    }
    artifact = AcceptanceArtifact.parse({
        "pass": _passing_artifact,
        "findings": _repair_artifact,
        "blocked": _human_artifact,
    }[outcome]())
    expected = {
        "pass": ReviewDecision.ACCEPT,
        "blocked": ReviewDecision.HUMAN,
        "findings": (
            ReviewDecision.REVIEW_EXHAUSTED if exhausted and not policy.fallback
            else ReviewDecision.REPAIR
        ),
    }[outcome]
    assert after_review(job, policy, artifact) == expected
    assert review_is_due(job, policy) is (not exhausted)
    # A consumed Human Blocker round can finish without reopening the budget.
    attempt = begin_review(
        job, work_subject="test", generation=1,
        boundary={"candidate_sha": "candidate"}, budget_window=1,
    )
    attempt["budget_consumed"] = True
    assert review_is_due(job, policy)
    assert begin_review(
        job, work_subject="test", generation=1,
        boundary={"candidate_sha": "candidate"}, budget_window=1,
    ) is attempt
    assert job["validation_attempts"] == 1


def test_exhausted_development_with_findings_cannot_become_fallback() -> None:
    job = {
        "review_budget": new_budget(development_attempts=TICKET_POLICY.development_limit),
        "review_budget_history": [],
    }
    assert after_review(job, TICKET_POLICY, AcceptanceArtifact.parse(_repair_artifact())) == (
        ReviewDecision.DEVELOPMENT_EXHAUSTED
    )


def test_resumed_result_replaces_only_the_same_reviewed_version() -> None:
    budget = new_budget()
    record = {"reviewer_thread_id": "first", "candidate_sha": "candidate",
              "review_identity": {"base": "a"}, "artifact": _human_artifact()}
    remember_review(budget, record, limit=3)
    replacement = {**deepcopy(record), "reviewer_thread_id": "replacement",
                   "artifact": _passing_artifact()}
    remember_review(budget, replacement, limit=3, human_blocker_resume=True)
    assert budget["review_artifacts"] == [replacement]
    changed_base = {**replacement, "review_identity": {"base": "b"}}
    remember_review(budget, changed_base, limit=3, human_blocker_resume=True)
    assert budget["review_artifacts"] == [replacement, changed_base]


@pytest.mark.parametrize("resume_phase,blocked_phase", [
    ("candidate", "blocked"), ("pending", "ready_for_human"),
])
def test_human_review_continues_the_same_attempt_and_paid_round(
    resume_phase: str, blocked_phase: str,
) -> None:
    job = {"review_budget": new_budget(), "review_budget_history": []}
    attempt = begin_review(
        job, work_subject="test", generation=1,
        boundary={"candidate_sha": "candidate"}, budget_window=1,
    )
    state = {"active_agent_invocation": {"semantic_attempt": deepcopy(attempt)}}
    invocation = deepcopy(state["active_agent_invocation"])
    artifact = consume_review_result(job, TICKET_POLICY, _human_artifact())
    job["pending_review_result"] = {"artifact": artifact.raw}
    finish_review_result(
        state, job, artifact, resume_phase=resume_phase, blocked_phase=blocked_phase,
    )
    assert job["phase"] == blocked_phase
    assert job["human_blocker_phase"] == resume_phase
    assert job["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    assert job["human_blocker_history"] == [{
        "phase": resume_phase, "human_blockers": list(artifact.blocker_evidence),
    }]
    assert state["status"] == "ready_for_human"
    assert state["diagnostics"] == [{
        "code": "reviewer_requires_human", "message": artifact.blocker_evidence[0],
    }]
    assert "pending_review_result" not in job
    # The resumed answer settles the original result without allocating or
    # charging another semantic review, even if the transport thread changed.
    accepted = consume_review_result(job, TICKET_POLICY, _passing_artifact())
    finish_review_result(
        state, job, accepted, resume_phase=resume_phase, blocked_phase=blocked_phase,
        history_facts={"reviewer_thread_id": "resumed-reviewer"},
    )
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert job["validation_attempts"] == 1
    assert "pending_semantic_attempt" not in job
    completed = job["semantic_attempt_history"][-1]
    assert completed["attempt_id"] == attempt["attempt_id"]
    assert completed["history_facts"]["reviewer_thread_id"] == "resumed-reviewer"
    assert state["active_agent_invocation"] == invocation
    assert "human_blockers" not in job
    assert job["human_blocker_history"][0]["human_blockers"] == list(artifact.blocker_evidence)
