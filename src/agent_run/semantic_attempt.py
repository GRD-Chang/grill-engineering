"""Canonical identity and lifecycle for one semantic unit of Agent work."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


def semantic_attempt_subjects(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return durable Attempt owners; consumers deduplicate mirrored jobs."""

    subjects: list[dict[str, Any]] = []

    def add(value: object) -> None:
        if isinstance(value, dict):
            subjects.append(value)

    add(state.get("active_ticket_job"))
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for key in sorted(jobs, key=str):
            add(jobs[key])
    add(state.get("parent_job"))
    acceptance = state.get("run_acceptance")
    add(acceptance)
    if isinstance(acceptance, dict):
        add(acceptance.get("repair_job"))
    retired = state.get("retired_semantic_attempt_owners")
    if isinstance(retired, list):
        for owner in retired:
            add(owner)
    add(state.get("run_publication"))
    return subjects


def invocation_attempt_is_pending(
    state: dict[str, Any], invocation: dict[str, Any]
) -> bool:
    """Return whether an Invocation snapshot still owns exact pending work."""

    bound = invocation.get("semantic_attempt")
    attempt_id = bound.get("attempt_id") if isinstance(bound, dict) else None
    return isinstance(attempt_id, str) and any(
        isinstance((pending := owner.get("pending_semantic_attempt")), dict)
        and pending.get("attempt_id") == attempt_id
        for owner in semantic_attempt_subjects(state)
    )


def invocation_is_explicitly_resumable(state: dict[str, Any]) -> bool:
    """Return whether the active Invocation has one exact public Resume path."""

    invocation = state.get("active_agent_invocation")
    if not isinstance(invocation, dict) or not invocation_attempt_is_pending(
        state, invocation
    ):
        return False
    invocation_status = invocation.get("status")
    if invocation_status == "resuming":
        return True
    return invocation_status in {"failed", "completed"} and (
        state.get("status") == "execution_failed"
        or state.get("github_refresh_pending") is True
    )


def canonical_fingerprint(value: object) -> str:
    """Return a deterministic digest without persisting private request fields."""

    if isinstance(value, dict):
        value = {
            str(key): item
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def allocate_semantic_attempt(
    subject: dict[str, Any],
    *,
    role: str,
    work_subject: str,
    generation: int,
    currentness_boundary: dict[str, Any],
    ordinal: int,
    budget_window: int | None = None,
) -> dict[str, Any]:
    """Allocate an Attempt or return the current resumable identity."""

    pending = subject.get("pending_semantic_attempt")
    if isinstance(pending, dict):
        require_semantic_attempt(
            pending,
            role=role,
            work_subject=work_subject,
            generation=generation,
            currentness_boundary=currentness_boundary,
            budget_window=budget_window,
        )
        return pending
    if pending is not None:
        raise ValueError("pending Semantic Agent Attempt must be an object")
    if type(generation) is not int or generation < 1:
        raise ValueError("Semantic Agent Attempt generation must be positive")
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("Semantic Agent Attempt ordinal must be positive")
    if budget_window is not None and (
        type(budget_window) is not int or budget_window < 1
    ):
        raise ValueError("Semantic Agent Attempt budget window must be positive")
    boundary_fingerprint = canonical_fingerprint(currentness_boundary)
    identity = {
        "role": role,
        "work_subject": work_subject,
        "generation": generation,
        "currentness_boundary_fingerprint": boundary_fingerprint,
        "ordinal": ordinal,
        "budget_window": budget_window,
    }
    attempt = {
        "attempt_id": canonical_fingerprint(identity),
        **identity,
        "status": "pending",
    }
    budget = subject.get("review_budget")
    if budget_window is not None and isinstance(budget, dict):
        # Capture only the bounded counters that were visible when this
        # Attempt was allocated. History must not later substitute the
        # owner's newer cumulative budget for this historical fact.
        attempt["budget_snapshot"] = {
            "window": budget_window,
            "development_attempts": budget.get("development_attempts"),
            "reviewer_invocations": budget.get("reviewer_invocations"),
        }
    subject["pending_semantic_attempt"] = attempt
    history = subject.setdefault("semantic_attempt_history", [])
    if not isinstance(history, list):
        raise ValueError("semantic_attempt_history must be an array")
    return attempt


def require_semantic_attempt(
    attempt: dict[str, Any],
    *,
    role: str,
    work_subject: str,
    generation: int,
    currentness_boundary: dict[str, Any],
    budget_window: int | None,
) -> None:
    """Fail closed when a pending Attempt no longer binds the same work."""

    if attempt.get("status") != "pending":
        raise ValueError("Semantic Agent Attempt is not pending")
    if attempt.get("role") != role:
        raise ValueError(
            "Semantic Agent Attempt role is invalid: "
            f"expected {role}, got {attempt.get('role')}"
        )
    if attempt.get("work_subject") != work_subject:
        raise ValueError("Semantic Agent Attempt work subject is invalid")
    if attempt.get("generation") != generation:
        raise ValueError("Semantic Agent Attempt generation is stale")
    if attempt.get("budget_window") != budget_window:
        raise ValueError("Semantic Agent Attempt budget window is stale")
    expected_boundary = canonical_fingerprint(currentness_boundary)
    if attempt.get("currentness_boundary_fingerprint") != expected_boundary:
        raise ValueError("Semantic Agent Attempt currentness boundary is stale")
    identity = {
        key: attempt.get(key)
        for key in (
            "role",
            "work_subject",
            "generation",
            "currentness_boundary_fingerprint",
            "ordinal",
            "budget_window",
        )
    }
    if attempt.get("attempt_id") != canonical_fingerprint(identity):
        raise ValueError("Semantic Agent Attempt identity is invalid")


def require_semantic_attempt_record(
    attempt: dict[str, Any], *, expected_status: str
) -> None:
    """Validate one persisted Attempt without reconstructing private inputs."""

    if attempt.get("status") != expected_status:
        raise ValueError("Semantic Agent Attempt status is invalid")
    if attempt.get("role") not in {"development", "reviewer", "publication"}:
        raise ValueError("Semantic Agent Attempt role is invalid")
    work_subject = attempt.get("work_subject")
    if not isinstance(work_subject, str) or not work_subject:
        raise ValueError("Semantic Agent Attempt work subject is invalid")
    for key in ("generation", "ordinal"):
        if type(attempt.get(key)) is not int or int(attempt[key]) < 1:
            raise ValueError(f"Semantic Agent Attempt {key} is invalid")
    budget_window = attempt.get("budget_window")
    if budget_window is not None and (
        type(budget_window) is not int or budget_window < 1
    ):
        raise ValueError("Semantic Agent Attempt budget window is invalid")
    boundary = attempt.get("currentness_boundary_fingerprint")
    if (
        not isinstance(boundary, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", boundary) is None
    ):
        raise ValueError("Semantic Agent Attempt currentness boundary is invalid")
    identity = {
        key: attempt.get(key)
        for key in (
            "role",
            "work_subject",
            "generation",
            "currentness_boundary_fingerprint",
            "ordinal",
            "budget_window",
        )
    }
    if attempt.get("attempt_id") != canonical_fingerprint(identity):
        raise ValueError("Semantic Agent Attempt identity is invalid")
    if expected_status == "completed" and (
        not isinstance(attempt.get("outcome"), str) or not attempt["outcome"]
    ):
        raise ValueError("completed Semantic Agent Attempt outcome is invalid")


def pending_semantic_attempt(
    subject: dict[str, Any], *, role: str | None = None
) -> dict[str, Any] | None:
    value = subject.get("pending_semantic_attempt")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending Semantic Agent Attempt must be an object")
    if value.get("status") != "pending":
        raise ValueError("pending Semantic Agent Attempt is not pending")
    if role is not None and value.get("role") != role:
        return None
    return value


def close_semantic_attempt(
    subject: dict[str, Any],
    attempt: dict[str, Any],
    *,
    outcome: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Close the exact pending Attempt after a canonical role result exists."""

    pending = pending_semantic_attempt(subject)
    if pending is None or pending.get("attempt_id") != attempt.get("attempt_id"):
        raise ValueError("Semantic Agent Attempt closeout does not match pending work")
    completed = deepcopy(pending)
    completed.update({"status": "completed", "outcome": outcome})
    if isinstance(result, dict):
        # Keep only the small, human-facing result facts needed when the
        # owner later advances to another Attempt.  Agent payloads remain
        # outside the Attempt record.
        for key in ("development_summary", "publication"):
            value = result.get(key)
            if key == "development_summary" and isinstance(value, str) and value.strip():
                completed[key] = value
            elif key == "publication" and isinstance(value, dict):
                publication_facts = {
                    field: deepcopy(value[field])
                    for field in ("commit_message", "pr_title", "pr_body_markdown")
                    if isinstance(value.get(field), str) and value[field].strip()
                }
                if set(publication_facts) == {
                    "commit_message",
                    "pr_title",
                    "pr_body_markdown",
                }:
                    completed[key] = publication_facts
    operation_retry = subject.get("publication_operation_retry")
    if completed.get("role") == "publication" and isinstance(
        operation_retry, dict
    ):
        completed["publication_operation_retry"] = deepcopy(operation_retry)
    history = subject.setdefault("semantic_attempt_history", [])
    if not isinstance(history, list):
        raise ValueError("semantic_attempt_history must be an array")
    history.append(completed)
    subject.pop("pending_semantic_attempt", None)


def release_semantic_attempt(subject: dict[str, Any]) -> None:
    """Release a reservation mechanically proven not to have started a Worker."""

    subject.pop("pending_semantic_attempt", None)


def detach_active_invocation(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    """Clear an active pointer whose Attempt was deterministically retired.

    The Invocation remains in ``agent_invocation_history`` as audit evidence;
    only the pointer that requires a live pending owner is removed.
    """

    active = state.get("active_agent_invocation")
    bound = active.get("semantic_attempt") if isinstance(active, dict) else None
    if isinstance(bound, dict) and bound.get("attempt_id") == attempt.get("attempt_id"):
        state["active_agent_invocation"] = None


def retire_semantic_attempt_owner(
    state: dict[str, Any],
    owner: dict[str, Any],
    *,
    owner_kind: str,
    work_subject: str,
    generation: int,
) -> None:
    """Keep bounded audit records when a current owner is removed from state."""

    history = owner.get("semantic_attempt_history", [])
    if not isinstance(history, list):
        raise ValueError("semantic_attempt_history must be an array")
    if not history:
        return
    retired = state.setdefault("retired_semantic_attempt_owners", [])
    if not isinstance(retired, list):
        raise ValueError("retired_semantic_attempt_owners must be an array")
    retired.append(
        {
            "owner_kind": owner_kind,
            "work_subject": work_subject,
            "generation": generation,
            "review_budget": deepcopy(owner.get("review_budget")),
            "semantic_attempt_history": deepcopy(history),
        }
    )
    del retired[:-64]


def controller_reprepare_intent(job: dict[str, Any]) -> dict[str, Any]:
    """Build a tamper-evident intent for Controller-only Candidate replay."""

    identity = {
        "kind": "controller_currentness_reprepare",
        "work_subject": f"run-repair:{job.get('run_id')}",
        "generation": job.get("repair_generation"),
        "base_sha": job.get("base_sha"),
        "default_base_sha": job.get("default_base_sha"),
        "expected_head": (
            job.get("managed_checkout_head")
            or job.get("candidate_sha")
            or job.get("base_sha")
        ),
        "attempt": job.get("pending_attempt"),
    }
    return {**identity, "intent_id": canonical_fingerprint(identity)}


def require_controller_reprepare_intent(job: dict[str, Any]) -> None:
    """Validate the narrow non-Agent Candidate replay authorization."""

    intent = job.get("controller_candidate_reprepare")
    if not isinstance(intent, dict):
        raise ValueError("Controller Candidate reprepare intent is missing")
    if job.get("phase") != "committing_candidate":
        raise ValueError("Controller Candidate reprepare phase is invalid")
    if intent != controller_reprepare_intent(job):
        raise ValueError("Controller Candidate reprepare intent is invalid")
