from __future__ import annotations

"""The bounded Development–Review loop, before integration/publication.

Consumers keep their durable jobs and semantic requests. This loop owns only
mechanical progress between development, candidate and review; it returns at
delivery or pause boundaries. There is no second workflow or shadow job here.
"""

from enum import Enum
from typing import Any, Protocol

from agent_run.artifacts import AcceptanceArtifact
from agent_run.ci_recovery_budget import (
    checkpoint_after_reviewer_failure,
    ci_limits_for_job,
)
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    can_start_development,
    can_start_review,
)
from agent_run.semantic_attempt import pending_semantic_attempt


class ReviewDecision(str, Enum):
    ACCEPT = "accepted"
    HUMAN = "reviewer_requires_human"
    REPAIR = "repairing"
    REVIEW_EXHAUSTED = "review_budget_exhausted"
    DEVELOPMENT_EXHAUSTED = "modification_budget_exhausted"
    CI_EXHAUSTED = "ci_inner_budget_exhausted"


def review_is_due(job: dict[str, Any], policy: ReviewBudgetPolicy) -> bool:
    """An interrupted semantic review may finish even at the window ceiling."""
    return (
        pending_semantic_attempt(job, role="reviewer") is not None
        or job.get("attempt_kind") == "ci_repair"
        or can_start_review(job, policy)
    )


def after_review(
    job: dict[str, Any], policy: ReviewBudgetPolicy, artifact: AcceptanceArtifact
) -> ReviewDecision:
    """Finite outcome rules shared by demand and full Parent acceptance.

    A finding never authorizes fallback. Only a later eligible Ticket Candidate
    can obtain that authority at the pre-review gate. CI keeps its own window.
    """
    if artifact.is_accepted:
        return ReviewDecision.ACCEPT
    if artifact.requires_human:
        return ReviewDecision.HUMAN
    if job.get("attempt_kind") == "ci_repair":
        outer_limit, inner_limit = ci_limits_for_job(job)
        if checkpoint_after_reviewer_failure(
            job, outer_limit=outer_limit, inner_limit=inner_limit,
            is_ticket=policy.budget_scope == "ticket",
        ):
            return ReviewDecision.CI_EXHAUSTED
        return ReviewDecision.REPAIR
    if not policy.fallback and not can_start_review(job, policy):
        return ReviewDecision.REVIEW_EXHAUSTED
    if not can_start_development(job, policy):
        return ReviewDecision.DEVELOPMENT_EXHAUSTED
    return ReviewDecision.REPAIR


class DevelopmentReviewWork(Protocol):
    """Existing consumer operations, restricted to these four stage boundaries."""

    job: dict[str, Any]

    def pause_requested(self) -> bool: ...
    def develop(self) -> bool: ...
    def commit_candidate(self) -> bool: ...
    def review(self) -> bool: ...
    def resume_review(self) -> bool: ...


def advance_development_review(
    work: DevelopmentReviewWork, *, single_step: bool = False
) -> bool:
    """Advance to a deliverable result or pause; False means yield to caller.

    Dev-first jobs start at developing; integrated full Parent starts pending.
    Both enter the same loop, including repair/re-review and result recovery.
    Consumer operations return False when external observation must be retried.
    """
    while True:
        if work.pause_requested():
            return False
        phase = work.job["phase"]
        if phase in {"developing", "repairing"}:
            progressing = work.develop()
        elif phase == "committing_candidate":
            progressing = work.commit_candidate()
        elif phase in {"candidate", "pending"}:
            progressing = work.review()
        elif phase == "reviewing":
            progressing = work.resume_review()
        else:
            return True
        if not progressing:
            return False
        if single_step:
            return True
