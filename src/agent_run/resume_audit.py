"""Bounded audit facts for explicit maintainer Resume authorization."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agent_run.external_supervision import is_github_refresh_wait
from agent_run.review_budget import budget_checkpoint_subjects
from agent_run.resume_audit_contract import (
    RESUME_AUDIT_KINDS,
    resume_event_digest,
    resume_history_digest,
    resume_identity,
)
from agent_run.semantic_attempt import (
    invocation_attempt_is_pending,
)
from agent_run.state_contract import human_blocker_subject_count


def append_explicit_resume_audit(
    state: dict[str, Any],
    *,
    new_thread: bool,
    human_response_supplied: bool,
) -> dict[str, Any]:
    """Record one public ``resume`` without creating a Resume budget."""

    audit = state.setdefault(
        "resume_audit",
        {
            "total": 0,
            "compacted": 0,
            "rolling_digest": None,
            "history": [],
        },
    )
    if not isinstance(audit, dict):
        raise ValueError("resume_audit must be an object")
    history = audit.get("history")
    if not isinstance(history, list):
        raise ValueError("resume_audit.history must be an array")
    total = audit.get("total")
    compacted = audit.get("compacted")
    if type(total) is not int or total < 0:
        raise ValueError("resume_audit.total must be non-negative")
    if compacted != 0:
        raise ValueError("resume_audit.compacted must remain zero")

    invocation = state.get("active_agent_invocation")
    active = (
        invocation
        if isinstance(invocation, dict)
        and invocation_attempt_is_pending(state, invocation)
        else None
    )
    bound = active.get("semantic_attempt") if active is not None else None
    attempt = bound if isinstance(bound, dict) else None
    requested_at = datetime.now(UTC).isoformat()
    sequence = total + 1
    event: dict[str, Any] = {
        "sequence": sequence,
        "requested_at": requested_at,
        "kind": _resume_kind(state, active),
        "source_status": str(state.get("status", "unknown")),
        "failure_code": _failure_code(state),
        "work_subject": active.get("work_subject") if active is not None else None,
        "generation": active.get("generation") if active is not None else None,
        "semantic_attempt_id": (
            attempt.get("attempt_id") if attempt is not None else None
        ),
        "source_invocation_started_at": (
            active.get("started_at") if active is not None else None
        ),
        "source_invocation_status": (
            active.get("status") if active is not None else None
        ),
        "thread_id": (
            active.get("reported_thread_id")
            or active.get("requested_thread_id")
            if active is not None
            else None
        ),
        "new_thread": new_thread,
        "human_response_supplied": human_response_supplied,
        "successor_invocation_started_at": None,
    }
    event["resume_id"] = resume_identity(str(state["run_id"]), event)
    event["event_digest"] = resume_event_digest(event)
    history.append(event)
    audit["rolling_digest"] = resume_history_digest(history)
    audit["total"] = sequence
    return deepcopy(event)


def latest_resume_audit(state: dict[str, Any]) -> dict[str, Any] | None:
    audit = state.get("resume_audit")
    history = audit.get("history") if isinstance(audit, dict) else None
    if not isinstance(history, list) or not history:
        return None
    assert isinstance(audit, dict)
    latest = history[-1]
    return deepcopy(latest) if isinstance(latest, dict) else None


def bind_resume_to_successor(
    state: dict[str, Any],
    *,
    semantic_attempt: dict[str, Any],
    successor_started_at: str,
) -> tuple[str, int] | None:
    """Link the next Invocation to the latest compatible public Resume."""

    audit = state.get("resume_audit")
    history = audit.get("history") if isinstance(audit, dict) else None
    if not isinstance(history, list) or not history:
        return None
    assert isinstance(audit, dict)
    event = history[-1]
    if not isinstance(event, dict) or event.get("kind") not in {
        "agent_invocation",
        "budget_checkpoint",
        "github_refresh_retry",
        "human_blocker",
    }:
        return None
    if event.get("successor_invocation_started_at") is not None:
        return None
    attempt_id = semantic_attempt.get("attempt_id")
    bound_attempt_id = event.get("semantic_attempt_id")
    if bound_attempt_id is not None and bound_attempt_id != attempt_id:
        return None
    resume_id = event.get("resume_id")
    sequence = event.get("sequence")
    if not isinstance(resume_id, str) or type(sequence) is not int:
        return None
    event.update(
        {
            "work_subject": semantic_attempt.get("work_subject"),
            "generation": semantic_attempt.get("generation"),
            "semantic_attempt_id": attempt_id,
            "successor_invocation_started_at": successor_started_at,
        }
    )
    event["event_digest"] = resume_event_digest(event)
    audit["rolling_digest"] = resume_history_digest(history)
    return resume_id, sequence


def _resume_kind(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    if state.get("status") == "supervision_timeout":
        return "supervision_timeout"
    if is_github_refresh_wait(state):
        return "github_refresh_retry"
    if len(budget_checkpoint_subjects(state)) == 1:
        return "budget_checkpoint"
    if human_blocker_subject_count(state) == 1:
        return "human_blocker"
    if (
        invocation is not None
        and invocation.get("status") in {"failed", "completed", "resuming"}
        and invocation_attempt_is_pending(state, invocation)
    ):
        return "agent_invocation"
    raise ValueError("explicit Resume has no canonical recovery boundary")


def _failure_code(state: dict[str, Any]) -> str | None:
    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, list):
        return None
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        if isinstance(code, str) and code:
            return code
    return None
