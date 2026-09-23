"""Durable, per-delivery-object allowance for external integration refreshes."""

from __future__ import annotations

from typing import Any

from agent_run.delivery_policy import parse_policy_snapshot


class ResyncBudgetExhausted(ValueError):
    """An external snapshot needs a new window authorized by explicit resume."""


def observe_integration_snapshot(
    job: dict[str, Any],
    *,
    source_sha: str,
    target_sha: str,
    policy_snapshot: object,
    own_source_write: bool = False,
    own_target_write: bool = False,
) -> bool:
    """Open a window for a live snapshot; return whether a resync was charged.

    Call after observing both live refs, before integrating. A first observation
    opens the free initial window. Further external changes while that window is
    open are folded into it. The caller must mark its own writes when reading
    back a just-pushed head, then close the window only after integration.
    """

    _sha(source_sha, "source_sha")
    _sha(target_sha, "target_sha")
    limit = parse_policy_snapshot(policy_snapshot).integration_resync_limit
    budget = job.get("integration_resync")
    if budget is None:
        job["integration_resync"] = {
            "generation": 1,
            "window": 1,
            "used": 0,
            "limit": limit,
            "open": True,
            "source_sha": source_sha,
            "target_sha": target_sha,
            "history": [],
        }
        return False
    require_resync_budget(budget, policy_snapshot=policy_snapshot)
    source_changed = source_sha != budget["source_sha"] and not own_source_write
    target_changed = target_sha != budget["target_sha"] and not own_target_write
    external_change = source_changed or target_changed
    charged = False
    if external_change and not budget["open"]:
        if budget["used"] >= budget["limit"]:
            raise ResyncBudgetExhausted("integration_resync_exhausted")
        budget["window"] += 1
        budget["used"] += 1
        budget["open"] = True
        charged = True
        budget["history"].append({
            "generation": budget["generation"],
            "window": budget["window"],
            "source_sha": source_sha,
            "target_sha": target_sha,
        })
    budget["source_sha"] = source_sha
    budget["target_sha"] = target_sha
    return charged


def complete_integration_window(job: dict[str, Any]) -> None:
    """Mark the current integrated snapshot as a completed window."""

    budget = job.get("integration_resync")
    if not isinstance(budget, dict):
        raise ValueError("integration_resync is missing")
    require_resync_budget(budget, policy_snapshot=job.get("policy_snapshot"))
    budget["open"] = False


def renew_resync_budget(job: dict[str, Any], *, policy_snapshot: object) -> None:
    """Explicit resume grants one new allowance, preserving cumulative history."""

    budget = job.get("integration_resync")
    if not isinstance(budget, dict):
        raise ValueError("integration_resync is missing")
    require_resync_budget(budget, policy_snapshot=job.get("policy_snapshot"))
    if budget["used"] < budget["limit"] or budget["open"]:
        raise ValueError("integration resync allowance is not exhausted")
    new_limit = parse_policy_snapshot(policy_snapshot).integration_resync_limit
    if budget["history"] and budget["history"][-1].get("renewed") is True:
        raise ValueError("integration resync renewal has not been used")
    budget["generation"] += 1
    budget["used"] = 0
    budget["limit"] = new_limit
    budget["history"].append({"generation": budget["generation"], "renewed": True})


def require_resync_budget(value: object, *, policy_snapshot: object) -> None:
    """Validate one persisted allowance without filling missing state."""

    if not isinstance(value, dict) or set(value) != {
        "generation", "window", "used", "limit", "open", "source_sha", "target_sha", "history"
    }:
        raise ValueError("integration_resync has an invalid field set")
    for key in ("generation", "window"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError(f"integration_resync.{key} is invalid")
    for key in ("used", "limit"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError(f"integration_resync.{key} is invalid")
    if value["used"] > value["limit"] or type(value["open"]) is not bool:
        raise ValueError("integration_resync usage is invalid")
    _sha(value["source_sha"], "source_sha")
    _sha(value["target_sha"], "target_sha")
    if not isinstance(value["history"], list):
        raise ValueError("integration_resync.history is invalid")
    for event in value["history"]:
        if not isinstance(event, dict):
            raise ValueError("integration_resync.history is invalid")
        if event.get("renewed") is True:
            if set(event) != {"generation", "renewed"}:
                raise ValueError("integration_resync.history renewal is invalid")
        elif set(event) == {"generation", "window", "source_sha", "target_sha"}:
            if type(event["window"]) is not int or event["window"] < 2:
                raise ValueError("integration_resync.history window is invalid")
            _sha(event["source_sha"], "source_sha")
            _sha(event["target_sha"], "target_sha")
        else:
            raise ValueError("integration_resync.history event is invalid")
        if type(event["generation"]) is not int or not 1 <= event["generation"] <= value["generation"]:
            raise ValueError("integration_resync.history generation is invalid")
    if policy_snapshot is not None and value["limit"] != parse_policy_snapshot(
        policy_snapshot
    ).integration_resync_limit:
        # Explicit resume may grant a new limit while the Run snapshot remains frozen.
        if value["generation"] == 1:
            raise ValueError("integration_resync.limit differs from the Run Policy")


def _sha(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty SHA")
