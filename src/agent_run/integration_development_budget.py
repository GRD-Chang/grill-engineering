"""Cumulative integration Development allowance, separate from demand work."""

from __future__ import annotations

from typing import Any

from agent_run.delivery_policy import parse_policy_snapshot


class IntegrationDevelopmentExhausted(ValueError):
    """A new integration Development requires explicit renewal."""


def integration_development_limit(job: dict[str, Any], policy_snapshot: object) -> int:
    policy = parse_policy_snapshot(policy_snapshot)
    return policy.run_repair_rounds if "parent_branch" in job else policy.ticket_integration_development_limit


def ensure_integration_development_available(
    job: dict[str, Any], *, policy_snapshot: object,
) -> dict[str, Any]:
    """Check before allocating new work; an already charged pending attempt resumes."""
    budget = job.get("integration_development_budget")
    if budget is None:
        budget = {
            "window": 1, "used": 0,
            "limit": integration_development_limit(job, policy_snapshot),
            "attempt_ids": [], "renewals": [],
        }
        job["integration_development_budget"] = budget
    require_integration_development_budget(budget, policy_snapshot=policy_snapshot,
        expected_limit=integration_development_limit(job, policy_snapshot))
    pending = job.get("pending_semantic_attempt")
    pending_id = pending.get("attempt_id") if isinstance(pending, dict) else None
    if pending_id not in budget["attempt_ids"] and budget["used"] >= budget["limit"]:
        raise IntegrationDevelopmentExhausted("integration_development_exhausted")
    return budget


def acquire_integration_development(
    job: dict[str, Any], *, attempt_id: str, policy_snapshot: object,
) -> bool:
    """Charge each semantic attempt once, including retries after process restart."""
    existing = job.get("integration_development_budget")
    if isinstance(existing, dict):
        require_integration_development_budget(existing, policy_snapshot=policy_snapshot,
            expected_limit=integration_development_limit(job, policy_snapshot))
        if attempt_id in existing["attempt_ids"]:
            return False
    budget = ensure_integration_development_available(job, policy_snapshot=policy_snapshot)
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("integration Development attempt identity is missing")
    if attempt_id in budget["attempt_ids"]:
        return False
    if budget["used"] >= budget["limit"]:
        raise IntegrationDevelopmentExhausted("integration_development_exhausted")
    budget["attempt_ids"].append(attempt_id)
    budget["used"] += 1
    return True


def renew_integration_development_budget(
    job: dict[str, Any], *, policy_snapshot: object,
) -> bool:
    """Explicit resume extends only this allowance; duplicate renewal is a no-op."""
    budget = job.get("integration_development_budget")
    require_integration_development_budget(budget, policy_snapshot=job.get("policy_snapshot"),
        expected_limit=integration_development_limit(job, job.get("policy_snapshot") or policy_snapshot))
    assert isinstance(budget, dict)
    if budget["used"] < budget["limit"]:
        return False
    grant = integration_development_limit(job, policy_snapshot)
    budget["window"] += 1
    budget["limit"] += grant
    budget["renewals"].append({
        "window": budget["window"], "used": budget["used"], "grant": grant,
    })
    return True


def require_integration_development_budget(
    value: object, *, policy_snapshot: object, expected_limit: int | None = None,
) -> None:
    """Reject malformed or reset cumulative counters without repairing state."""
    if not isinstance(value, dict) or set(value) != {"window", "used", "limit", "attempt_ids", "renewals"}:
        raise ValueError("integration_development_budget has invalid fields")
    for key in ("window", "used", "limit"):
        if type(value[key]) is not int or value[key] < (0 if key == "used" else 1):
            raise ValueError(f"integration_development_budget.{key} is invalid")
    ids = value["attempt_ids"]
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
        raise ValueError("integration Development attempt identities are invalid")
    if len(set(ids)) != len(ids) or len(ids) != value["used"] or value["used"] > value["limit"]:
        raise ValueError("integration Development cumulative usage is invalid")
    renewals = value["renewals"]
    if not isinstance(renewals, list) or len(renewals) != value["window"] - 1:
        raise ValueError("integration Development renewal history is invalid")
    total_grants = 0
    previous_used = 0
    for window, event in enumerate(renewals, 2):
        if not isinstance(event, dict) or set(event) != {"window", "used", "grant"}:
            raise ValueError("integration Development renewal event is invalid")
        if event["window"] != window or any(type(event[key]) is not int for key in event):
            raise ValueError("integration Development renewal sequence is invalid")
        if event["grant"] < 1 or not previous_used < event["used"] <= value["used"]:
            raise ValueError("integration Development renewal usage is invalid")
        initial = event["used"] - total_grants
        if window > 2 and initial != renewals[0]["used"]:
            raise ValueError("integration Development renewal allowance is inconsistent")
        previous_used = event["used"]
        total_grants += event["grant"]
    initial_limit = value["limit"] - total_grants
    if initial_limit < 1 or renewals and renewals[0]["used"] != initial_limit:
        raise ValueError("integration Development initial allowance is invalid")
    if policy_snapshot is not None and initial_limit != (expected_limit if expected_limit is not None
        else parse_policy_snapshot(policy_snapshot).ticket_integration_development_limit):
        raise ValueError("integration Development allowance differs from frozen policy")


def semantic_attempt_budget(owner: dict[str, Any], attempt: dict[str, Any]) -> object:
    """Select the independent allowance for CI or charged integration work."""
    ci_budget = owner.get("ci_recovery")
    ci_snapshot = attempt.get("budget_snapshot")
    if isinstance(ci_snapshot, dict) and "ci_developments_used" in ci_snapshot:
        return ci_budget if isinstance(ci_budget, dict) and "developments_used" in ci_budget else None
    budget = owner.get("integration_development_budget")
    charged = isinstance(budget, dict) and attempt.get("attempt_id") in budget.get("attempt_ids", [])
    snapshot = attempt.get("budget_snapshot")
    if isinstance(snapshot, dict) and "integration_development_used" in snapshot:
        return budget if charged else None
    if charged:
        return budget
    from agent_run.review_budget import review_budget_owner
    return review_budget_owner(owner).get("review_budget")
