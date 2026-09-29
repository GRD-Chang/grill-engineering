from __future__ import annotations

"""Shared Reviewer attempt and bounded result bookkeeping."""

from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.ci_recovery_budget import mark_ci_review_invocation
from agent_run.review_budget import ReviewBudget, ReviewBudgetPolicy, mark_review
from agent_run.semantic_attempt import (
    allocate_semantic_attempt, canonical_fingerprint, pending_semantic_attempt,
)
from agent_run.state_errors import IncompatibleRunStateError


def review_ordinal(job: dict[str, Any]) -> int:
    pending = pending_semantic_attempt(job, role="reviewer")
    return (
        int(pending["ordinal"]) if pending is not None
        else int(job.get("validation_attempts", 0)) + 1
    )


def begin_review(
    job: dict[str, Any], *, work_subject: str, generation: int,
    boundary: dict[str, Any], budget_window: int,
) -> dict[str, Any]:
    ordinal = review_ordinal(job)
    job["validation_attempts"] = ordinal
    return allocate_semantic_attempt(
        job, role="reviewer", work_subject=work_subject, generation=generation,
        currentness_boundary=boundary, ordinal=ordinal, budget_window=budget_window,
    )


def consume_review_result(
    job: dict[str, Any], policy: ReviewBudgetPolicy, raw: object,
) -> AcceptanceArtifact:
    """Only a valid role result consumes the existing attempt, exactly once."""
    artifact = AcceptanceArtifact.parse(raw)
    attempt = pending_semantic_attempt(job, role="reviewer")
    if attempt is None:
        raise ValueError("Reviewer result is missing its Semantic Attempt")
    if attempt.get("budget_consumed") is not True:
        if job.get("attempt_kind") == "ci_repair":
            mark_ci_review_invocation(job)
        else:
            mark_review(job, policy)
        attempt["budget_consumed"] = True
    return artifact


def remember_review(
    budget: ReviewBudget | dict[str, Any], record: dict[str, Any], *,
    limit: int, human_blocker_resume: bool = False,
) -> None:
    """Replace the same semantic result; retain raw blockers in their history.

    Candidate and review identity must both match. A permitted new-thread human
    resume can replace the blocked artifact, but never another reviewed version.
    """
    records = budget["review_artifacts"]
    matching = next((
        index for index in range(len(records) - 1, -1, -1)
        if records[index].get("candidate_sha") == record["candidate_sha"]
        and records[index].get("review_identity") == record["review_identity"]
        and (human_blocker_resume or records[index].get("reviewer_thread_id")
             == record["reviewer_thread_id"])
    ), None)
    if matching is None:
        records.append(record)
    else:
        records[matching] = record
    del records[:-limit]


def require_pending_run_review(run: dict[str, Any]) -> None:
    """A saved result belongs to exactly the completed semantic Reviewer."""
    if "pending_review_result" not in run:
        return
    try:
        pending = run["pending_review_result"]
        record = pending["record"]
        attempt = pending_semantic_attempt(run, role="reviewer")
        fields = (
            "reviewed_head_sha", "reviewed_default_base_sha", "expected_merge_tree",
            "parent_revision", "ticket_graph_revision", "reviewer_thread_id",
        )
        if not all(isinstance(record[key], str) and record[key] for key in fields):
            raise ValueError("missing reviewed identity")
        boundary = {key: record[key] for key in fields if key != "reviewer_thread_id"}
        boundary["ticket_completion_records_fingerprint"] = canonical_fingerprint(
            record["ticket_completion_records"]
        )
        AcceptanceArtifact.parse(record["artifact"])
        if (
            run.get("phase") != "reviewing"
            or record.get("acceptance_scope") != "run"
            or record.get("reviewed_base_sha") != record["reviewed_default_base_sha"]
            or record["reviewer_thread_id"] not in run.get("reviewer_thread_ids", [])
            or attempt is None
            or attempt.get("budget_consumed") is not True
            or attempt["currentness_boundary_fingerprint"] != canonical_fingerprint(boundary)
        ):
            raise ValueError("returned result does not match its Reviewer attempt")
    except (KeyError, TypeError, ValueError) as error:
        raise IncompatibleRunStateError("invalid pending Run Review result") from error
