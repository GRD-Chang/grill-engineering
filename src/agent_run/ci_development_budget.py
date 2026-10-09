from __future__ import annotations

"""CI Development uses one durable quota, across failed versions."""

from typing import Any

EXHAUSTED = "ci_development_budget_exhausted"


def new_ci_development_budget(*, review_required: bool = False) -> dict[str, Any]:
    return {
        "window": 1,
        "developments_used": 0,
        "failure_head": None,
        "repair_pending": False,
        "review_required": review_required,
        "checkpoint_reason": None,
        "history": [],
        "semantic_work_ids": [],
        "previous_publication_authorization": None,
        "reviewer_invocations": 0,
        "review_artifacts": [],
    }


def ensure_ci_development_budget(job: dict[str, Any], limit: int) -> dict[str, Any]:
    if type(limit) is not int or limit < 1:
        raise ValueError("CI Development limit must be a positive integer")
    budget = job.get("ci_recovery")
    if not isinstance(budget, dict) or set(budget) != set(new_ci_development_budget()):
        raise ValueError("missing canonical ci_recovery")
    for key, minimum in (("window", 1), ("developments_used", 0), ("reviewer_invocations", 0)):
        if type(budget[key]) is not int or budget[key] < minimum:
            raise ValueError(f"ci_recovery.{key} is invalid")
    if budget["developments_used"] > limit:
        raise ValueError("CI Development usage exceeds its limit")
    for key in ("repair_pending", "review_required"):
        if type(budget[key]) is not bool:
            raise ValueError(f"ci_recovery.{key} must be boolean")
    if "ticket_number" in job and job.get("policy_snapshot") is not None:
        from agent_run.delivery_policy import parse_policy_snapshot
        if budget["review_required"] != parse_policy_snapshot(job["policy_snapshot"]).ticket_ci_review:
            raise ValueError("Ticket CI Review differs from its frozen policy")
    if budget["checkpoint_reason"] not in (None, EXHAUSTED):
        raise ValueError("ci_recovery.checkpoint_reason is invalid")
    head = budget["failure_head"]
    if head is not None and (not isinstance(head, str) or not head):
        raise ValueError("ci_recovery.failure_head is invalid")
    if budget["developments_used"] and head is None:
        raise ValueError("charged CI Development is missing its failure head")
    if budget["repair_pending"] and not budget["developments_used"]:
        raise ValueError("pending CI repair has no charged attempt")
    for key in ("history", "review_artifacts"):
        if not isinstance(budget[key], list) or not all(isinstance(item, dict) for item in budget[key]):
            raise ValueError(f"ci_recovery.{key} must contain objects")
    ids = budget["semantic_work_ids"]
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("ci_recovery.semantic_work_ids is invalid")
    if len(budget["review_artifacts"]) > budget["reviewer_invocations"]:
        raise ValueError("CI Review Artifacts exceed Reviewer invocations")
    prior = budget["previous_publication_authorization"]
    if prior is not None and not isinstance(prior, dict):
        raise ValueError("ci_recovery.previous_publication_authorization is invalid")
    return budget


def observe_ci_failure(job: dict[str, Any], *, head_sha: str, limit: int) -> str:
    if not isinstance(head_sha, str) or not head_sha:
        raise ValueError("CI failure must identify its published head")
    budget = ensure_ci_development_budget(job, limit)
    if budget["failure_head"] != head_sha:
        budget["history"].append({"kind": "failure", "failure_head": head_sha, "developments_used": budget["developments_used"]})
        budget["failure_head"] = head_sha
        budget["repair_pending"] = False
    if budget["checkpoint_reason"] == EXHAUSTED or budget["developments_used"] >= limit:
        budget["checkpoint_reason"] = EXHAUSTED
        return "exhausted"
    return "repair"


def mark_ci_development(job: dict[str, Any], *, limit: int, semantic_work_id: str | None = None) -> int:
    budget = ensure_ci_development_budget(job, limit)
    if semantic_work_id is not None and (not isinstance(semantic_work_id, str) or not semantic_work_id):
        raise ValueError("CI semantic work id must be a non-empty string")
    if semantic_work_id is not None and semantic_work_id in budget["semantic_work_ids"]:
        return int(budget["developments_used"])
    if budget["failure_head"] is None or budget["checkpoint_reason"] is not None or budget["developments_used"] >= limit:
        raise ValueError("CI repair budget is exhausted")
    budget["developments_used"] += 1
    budget["repair_pending"] = True
    if semantic_work_id is not None:
        budget["semantic_work_ids"].append(semantic_work_id)
    return int(budget["developments_used"])


def renew_ci_development_budget(job: dict[str, Any], *, limit: int) -> str:
    budget = ensure_ci_development_budget(job, limit)
    if budget["checkpoint_reason"] is None:
        return ""  # A repeated request cannot open a second window.
    budget["history"].append({"kind": "resume", "reason": EXHAUSTED, "window": budget["window"], "developments_used": budget["developments_used"], "failure_head": budget["failure_head"]})
    budget["window"] += 1
    budget["developments_used"] = 0
    budget["semantic_work_ids"] = []
    budget["repair_pending"] = False
    budget["checkpoint_reason"] = None
    return EXHAUSTED
