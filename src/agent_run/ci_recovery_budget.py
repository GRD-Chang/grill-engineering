from __future__ import annotations

"""Durable independent quotas for Required Checks recovery.

The caller owns phase changes and persistence. This module is the sole writer
of the CI recovery counters; ordinary development and integration budgets are
never modified here.
"""

from collections.abc import Mapping
from typing import Any, Literal, TypedDict, cast

from agent_run.delivery_policy import parse_policy_snapshot
from agent_run.ticket_ci_budget import (
    EXHAUSTED, ensure_ticket_ci_budget, mark_ticket_ci_development,
    new_ticket_ci_recovery_budget as new_ticket_ci_recovery_budget,
    observe_ticket_ci_failure, renew_ticket_ci_budget,
)


CIRecoveryDecision = Literal["repair", "inner_exhausted", "outer_exhausted", "exhausted"]
_CHECKPOINTS = {None, "ci_inner_budget_exhausted", "ci_outer_budget_exhausted"}
_FIELDS = frozenset(
    {
        "outer_window",
        "rounds_used",
        "inner_window",
        "repairs_used",
        "failure_head",
        "repair_pending",
        "review_required",
        "checkpoint_reason",
        "history",
        "previous_publication_authorization",
        "reviewer_invocations",
        "review_artifacts",
    }
)


class CIRecoveryBudget(TypedDict):
    outer_window: int
    rounds_used: int
    inner_window: int
    repairs_used: int
    failure_head: str | None
    repair_pending: bool
    review_required: bool
    checkpoint_reason: str | None
    history: list[dict[str, Any]]
    previous_publication_authorization: dict[str, Any] | None
    reviewer_invocations: int
    review_artifacts: list[dict[str, Any]]


def new_ci_recovery_budget() -> CIRecoveryBudget:
    return {
        "outer_window": 1,
        "rounds_used": 0,
        "inner_window": 1,
        "repairs_used": 0,
        "failure_head": None,
        "repair_pending": False,
        "review_required": False,
        "checkpoint_reason": None,
        "history": [],
        "previous_publication_authorization": None,
        "reviewer_invocations": 0,
        "review_artifacts": [],
    }


def ci_limits_for_job(
    job: dict[str, Any], *, state_snapshot: object | None = None
) -> tuple[int, int]:
    """Read limits from this Job's frozen Delivery Policy."""

    policy = parse_policy_snapshot(job.get("policy_snapshot") or state_snapshot)
    if "ticket_number" in job or isinstance(job.get("ci_recovery"), dict) and "developments_used" in job["ci_recovery"]:
        return policy.ticket_ci_development_limit, policy.ticket_ci_development_limit
    return policy.ci_recovery_rounds, policy.ci_repairs_per_round


def _limits(outer_limit: int, inner_limit: int) -> None:
    if type(outer_limit) is not int or outer_limit < 1:
        raise ValueError("CI outer limit must be a positive integer")
    if type(inner_limit) is not int or inner_limit < 1:
        raise ValueError("CI inner limit must be a positive integer")


def _inner_limit(budget: Mapping[str, Any], inner_limit: int, is_ticket: bool) -> int:
    return inner_limit


def ensure_ci_recovery_budget(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool
) -> dict[str, Any]:
    """Reject missing or incompatible state rather than infer used attempts."""

    _limits(outer_limit, inner_limit)
    if is_ticket:
        return ensure_ticket_ci_budget(job, inner_limit)
    raw = job.get("ci_recovery")
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError("missing canonical ci_recovery")
    budget = cast(CIRecoveryBudget, raw)
    for key in ("outer_window", "inner_window"):
        value = raw[key]
        if type(value) is not int or value < 1:
            raise ValueError(f"ci_recovery.{key} must be a positive integer")
    for key in ("rounds_used", "repairs_used"):
        value = raw[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"ci_recovery.{key} must be a non-negative integer")
    if budget["rounds_used"] > outer_limit:
        raise ValueError("ci_recovery.rounds_used exceeds the outer limit")
    if budget["repairs_used"] > _inner_limit(budget, inner_limit, is_ticket):
        raise ValueError("ci_recovery.repairs_used exceeds the inner limit")
    if budget["rounds_used"] == 0 and (
        budget["repairs_used"] != 0 or budget["failure_head"] is not None
    ):
        raise ValueError("unused CI recovery cannot contain a repair")
    if budget["rounds_used"] > 0 and not (
        isinstance(budget["failure_head"], str) and budget["failure_head"]
    ):
        raise ValueError("active CI recovery is missing its failure head")
    if type(budget["repair_pending"]) is not bool:
        raise ValueError("ci_recovery.repair_pending must be boolean")
    if type(budget["review_required"]) is not bool:
        raise ValueError("ci_recovery.review_required must be boolean")
    if budget["repair_pending"] and budget["repairs_used"] == 0:
        raise ValueError("pending CI repair has no charged attempt")
    if budget["checkpoint_reason"] not in _CHECKPOINTS:
        raise ValueError("ci_recovery.checkpoint_reason is invalid")
    if not isinstance(budget["history"], list) or not all(
        isinstance(item, dict) for item in budget["history"]
    ):
        raise ValueError("ci_recovery.history must contain objects")
    previous_authorization = budget["previous_publication_authorization"]
    if previous_authorization is not None and not isinstance(
        previous_authorization, dict
    ):
        raise ValueError("ci_recovery.previous_publication_authorization is invalid")
    if type(budget["reviewer_invocations"]) is not int or budget["reviewer_invocations"] < 0:
        raise ValueError("ci_recovery.reviewer_invocations must be non-negative")
    artifacts = budget["review_artifacts"]
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ValueError("ci_recovery.review_artifacts must contain objects")
    if len(artifacts) > budget["reviewer_invocations"]:
        raise ValueError("CI Review Artifacts exceed Reviewer invocations")
    if not is_ticket and budget["rounds_used"] > 0 and not budget["review_required"]:
        raise ValueError("Parent CI recovery requires Reviewer")
    return cast(dict[str, Any], budget)


def observe_repairable_ci_failure(
    job: dict[str, Any], *, head_sha: str, outer_limit: int,
    inner_limit: int, is_ticket: bool
) -> CIRecoveryDecision:
    """Open a round only for a new failed version following a charged repair."""

    if is_ticket:
        return cast(CIRecoveryDecision, observe_ticket_ci_failure(job, head_sha=head_sha, limit=inner_limit))
    if not isinstance(head_sha, str) or not head_sha:
        raise ValueError("CI failure must identify its published head")
    budget = ensure_ci_recovery_budget(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    )
    if budget["checkpoint_reason"] == "ci_outer_budget_exhausted":
        return "outer_exhausted"
    if budget["checkpoint_reason"] == "ci_inner_budget_exhausted":
        return "inner_exhausted"
    if budget["rounds_used"] == 0:
        budget["rounds_used"] = 1
        budget["failure_head"] = head_sha
        budget["review_required"] = True
    elif head_sha != budget["failure_head"]:
        if not budget["repair_pending"]:
            raise ValueError("new CI failure has no preceding repair version")
        budget["history"].append(
            {"kind": "round", "round": budget["rounds_used"],
             "failure_head": budget["failure_head"], "repairs_used": budget["repairs_used"]}
        )
        budget["failure_head"] = head_sha
        budget["repair_pending"] = False
        # Parent always retains Reviewer authority.
        budget["review_required"] = True
        if budget["rounds_used"] >= outer_limit:
            budget["checkpoint_reason"] = "ci_outer_budget_exhausted"
            return "outer_exhausted"
        budget["rounds_used"] += 1
        budget["inner_window"] = 1
        budget["repairs_used"] = 0
    if budget["repairs_used"] >= _inner_limit(budget, inner_limit, is_ticket):
        budget["checkpoint_reason"] = "ci_inner_budget_exhausted"
        return "inner_exhausted"
    return "repair"


def can_start_ci_repair(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool
) -> bool:
    if is_ticket:
        ticket_budget = ensure_ticket_ci_budget(job, inner_limit)
        return ticket_budget["failure_head"] is not None and ticket_budget["checkpoint_reason"] is None and ticket_budget["developments_used"] < inner_limit
    budget = ensure_ci_recovery_budget(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    )
    return (
        budget["rounds_used"] > 0
        and budget["checkpoint_reason"] is None
        and budget["repairs_used"] < _inner_limit(budget, inner_limit, is_ticket)
    )


def mark_ci_repair(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool,
    semantic_work_id: str | None = None,
) -> int:
    if is_ticket:
        return mark_ticket_ci_development(job, limit=inner_limit, semantic_work_id=semantic_work_id)
    if not can_start_ci_repair(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    ):
        raise ValueError("CI repair budget is exhausted")
    budget = cast(CIRecoveryBudget, job["ci_recovery"])
    budget["repairs_used"] += 1
    budget["repair_pending"] = True
    return budget["repairs_used"]


def refund_ci_repair_without_candidate(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool
) -> None:
    """Settle a no-Candidate investigation; only legacy Parent refunds work."""
    if is_ticket:
        ticket_budget = ensure_ticket_ci_budget(job, inner_limit)
        if not ticket_budget["developments_used"]:
            raise ValueError("no CI repair attempt to refund")
        # A completed investigation consumed actual Agent work, even if it
        # produced no code Candidate. Only observation/waiting is free.
        ticket_budget["repair_pending"] = False
        return
    budget = ensure_ci_recovery_budget(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    )
    if budget["repairs_used"] == 0:
        raise ValueError("no CI repair attempt to refund")
    budget["repairs_used"] -= 1
    if budget["repairs_used"] == 0:
        budget["repair_pending"] = False


def ci_review_required(job: dict[str, Any], *, is_ticket: bool) -> bool:
    budget = job.get("ci_recovery")
    if not isinstance(budget, dict) or type(budget.get("review_required")) is not bool:
        raise ValueError("missing canonical ci_recovery")
    return not is_ticket or budget["review_required"]


def ci_reviewer_budget_context(
    job: dict[str, Any], *, inner_limit: int, is_ticket: bool
) -> dict[str, int]:
    """Describe the charged CI Development, without ordinary quota."""

    budget = job.get("ci_recovery")
    if not isinstance(budget, dict):
        raise ValueError("missing canonical ci_recovery")
    repairs = budget.get("developments_used" if is_ticket else "repairs_used")
    if type(repairs) is not int or repairs < 1:
        raise ValueError("CI Reviewer requires a charged repair")
    limit = inner_limit
    return {
        "current_review_attempt": int(budget["reviewer_invocations"]) + 1 if is_ticket else repairs,
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
    job: dict[str, Any], *, inner_limit: int, is_ticket: bool
) -> dict[str, int]:
    """Project remaining CI Development capacity."""

    budget = job.get("ci_recovery")
    if not isinstance(budget, dict):
        raise ValueError("missing canonical ci_recovery")
    repairs = budget.get("developments_used" if is_ticket else "repairs_used")
    if type(repairs) is not int or repairs < 1:
        raise ValueError("CI repair context requires a charged repair")
    limit = inner_limit
    return {
        "completed_review_attempts": int(budget["reviewer_invocations"]) if is_ticket else max(0, repairs - 1),
        "remaining_review_attempts": limit - repairs,
    }


def checkpoint_after_reviewer_failure(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool
) -> bool:
    """Return True when another CI Development is unavailable."""

    if is_ticket:
        ticket_budget = ensure_ticket_ci_budget(job, inner_limit)
        if ticket_budget["developments_used"] < inner_limit:
            return False
        ticket_budget["checkpoint_reason"] = EXHAUSTED
        return True
    budget = ensure_ci_recovery_budget(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    )
    if budget["repairs_used"] < _inner_limit(budget, inner_limit, is_ticket):
        return False
    budget["checkpoint_reason"] = "ci_inner_budget_exhausted"
    return True


def reset_exhausted_ci_budget(
    job: dict[str, Any], *, outer_limit: int, inner_limit: int, is_ticket: bool
) -> str:
    """Renew only the exhausted CI allowance, retaining history."""

    if is_ticket:
        return renew_ticket_ci_budget(job, limit=inner_limit)
    budget = ensure_ci_recovery_budget(
        job, outer_limit=outer_limit, inner_limit=inner_limit, is_ticket=is_ticket
    )
    reason = budget["checkpoint_reason"]
    if reason not in {"ci_inner_budget_exhausted", "ci_outer_budget_exhausted"}:
        raise ValueError("CI recovery has no exhausted budget")
    budget["history"].append(
        {"kind": "resume", "reason": reason,
         "outer_window": budget["outer_window"], "rounds_used": budget["rounds_used"],
         "inner_window": budget["inner_window"], "repairs_used": budget["repairs_used"],
         "failure_head": budget["failure_head"]}
    )
    if reason == "ci_inner_budget_exhausted":
        budget["inner_window"] += 1
    else:
        budget["outer_window"] += 1
        budget["rounds_used"] = 1
        budget["inner_window"] = 1
    budget["repairs_used"] = 0
    budget["repair_pending"] = False
    budget["checkpoint_reason"] = None
    return str(reason)
