from __future__ import annotations

"""Shared cumulative CI Development allowance and review accounting."""

from typing import Any, Literal, cast

from agent_run.delivery_policy import parse_policy_snapshot
from agent_run.ci_development_budget import (
    EXHAUSTED, ensure_ci_development_budget, mark_ci_development,
    new_ci_development_budget, observe_ci_failure, renew_ci_development_budget,
)

CIRecoveryDecision = Literal["repair", "exhausted"]


def new_ci_recovery_budget() -> dict[str, Any]:
    return new_ci_development_budget(review_required=True)


def new_ticket_ci_recovery_budget(*, review_required: bool = False) -> dict[str, Any]:
    return new_ci_development_budget(review_required=review_required)


def ci_limit_for_job(job: dict[str, Any], *, state_snapshot: object | None = None) -> int:
    policy = parse_policy_snapshot(job.get("policy_snapshot") or state_snapshot)
    limit = policy.ticket_ci_development_limit if "ticket_number" in job else policy.parent_ci_development_limit
    return limit


def ensure_ci_recovery_budget(job: dict[str, Any], *, limit: int, is_ticket: bool) -> dict[str, Any]:
    budget = ensure_ci_development_budget(job, limit)
    if not is_ticket and not budget["review_required"]:
        raise ValueError("Parent CI recovery requires Reviewer")
    return budget


def observe_repairable_ci_failure(job: dict[str, Any], *, head_sha: str, limit: int, is_ticket: bool) -> CIRecoveryDecision:
    ensure_ci_recovery_budget(job, limit=limit, is_ticket=is_ticket)
    return cast(CIRecoveryDecision, observe_ci_failure(job, head_sha=head_sha, limit=limit))


def can_start_ci_repair(job: dict[str, Any], *, limit: int, is_ticket: bool) -> bool:
    budget = ensure_ci_recovery_budget(job, limit=limit, is_ticket=is_ticket)
    return budget["failure_head"] is not None and budget["checkpoint_reason"] is None and budget["developments_used"] < limit


def mark_ci_repair(job: dict[str, Any], *, limit: int, is_ticket: bool, semantic_work_id: str | None = None) -> int:
    ensure_ci_recovery_budget(job, limit=limit, is_ticket=is_ticket)
    return mark_ci_development(job, limit=limit, semantic_work_id=semantic_work_id)


def refund_ci_repair_without_candidate(job: dict[str, Any], *, limit: int, is_ticket: bool) -> None:
    """Settle completed investigation without refunding actual Agent work."""
    budget = ensure_ci_development_budget(job, limit)
    if not budget["developments_used"]:
        raise ValueError("no CI repair attempt to refund")
    budget["repair_pending"] = False


def ci_review_required(job: dict[str, Any], *, is_ticket: bool) -> bool:
    budget = job.get("ci_recovery")
    if not isinstance(budget, dict) or type(budget.get("review_required")) is not bool:
        raise ValueError("missing canonical ci_recovery")
    return not is_ticket or budget["review_required"]


def ci_reviewer_budget_context(
    job: dict[str, Any], *, limit: int, is_ticket: bool
) -> dict[str, int]:
    """Describe the charged CI Development, without ordinary quota."""

    budget = job.get("ci_recovery")
    if not isinstance(budget, dict):
        raise ValueError("missing canonical ci_recovery")
    repairs = budget.get("developments_used")
    if type(repairs) is not int or repairs < 1:
        raise ValueError("CI Reviewer requires a charged repair")
    return {
        "current_review_attempt": int(budget["reviewer_invocations"]) + 1,
        "remaining_review_attempts": limit - repairs,
    }


def mark_ci_review_invocation(job: dict[str, Any]) -> int:
    """Count one completed CI Reviewer invocation outside ordinary quota."""

    budget = job.get("ci_recovery")
    if not isinstance(budget, dict):
        raise ValueError("missing canonical ci_recovery")
    used = budget.get("reviewer_invocations")
    if type(used) is not int or used < 0:
        raise ValueError("CI Reviewer invocation counter is invalid")
    budget["reviewer_invocations"] = used + 1
    return used + 1


def ci_repair_budget_context(
    job: dict[str, Any], *, limit: int, is_ticket: bool
) -> dict[str, int]:
    """Project remaining CI Development capacity."""

    budget = job.get("ci_recovery")
    if not isinstance(budget, dict):
        raise ValueError("missing canonical ci_recovery")
    repairs = budget.get("developments_used")
    if type(repairs) is not int or repairs < 1:
        raise ValueError("CI repair context requires a charged repair")
    return {
        "completed_review_attempts": int(budget["reviewer_invocations"]),
        "remaining_review_attempts": limit - repairs,
    }


def checkpoint_after_reviewer_failure(job: dict[str, Any], *, limit: int, is_ticket: bool) -> bool:
    budget = ensure_ci_development_budget(job, limit)
    if budget["developments_used"] < limit:
        return False
    budget["checkpoint_reason"] = EXHAUSTED
    return True


def reset_exhausted_ci_budget(job: dict[str, Any], *, limit: int, is_ticket: bool) -> str:
    return renew_ci_development_budget(job, limit=limit)
