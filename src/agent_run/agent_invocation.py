from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable


def canonical_fingerprint(value: object) -> str:
    """Return a deterministic digest without persisting the underlying input."""

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


def select_publication_thread(
    job: dict[str, Any], *, max_context_attempts: int
) -> str | None:
    """Select Publication context using the shared, bounded fallback order."""

    if job.get("publication_new_thread") is True:
        return None
    publication_thread = job.get("publication_thread_id")
    if isinstance(publication_thread, str) and publication_thread:
        return publication_thread
    development_thread = job.get("development_thread_id")
    if (
        isinstance(development_thread, str)
        and development_thread
        and int(job.get("publication_attempts", 0)) < max_context_attempts
    ):
        return development_thread
    return None


def invocation_event_recorder(
    state: dict[str, Any],
    *,
    role: str,
    phase: str,
    work_subject: str,
    generation: int,
    invocation_input: dict[str, Any],
    currentness_boundary: dict[str, Any],
    save: Callable[[dict[str, Any]], object],
) -> Callable[..., None]:
    """Persist the small, durable facts for one active Agent Invocation."""

    def record(kind: str, **facts: object) -> None:
        now = datetime.now(UTC).isoformat()
        if kind == "started":
            invocation: dict[str, Any] = {
                "work_subject": work_subject,
                "generation": generation,
                "role": role,
                "phase": phase,
                "mode": facts.get("invocation_mode")
                or ("resume" if facts.get("requested_thread_id") else "fresh"),
                "input_fingerprint": canonical_fingerprint(invocation_input),
                "currentness_boundary": dict(currentness_boundary),
                "status": "running",
                "requested_thread_id": facts.get("requested_thread_id"),
                "reported_thread_id": None,
                "attempt_count": 0,
                "started_at": now,
                "ended_at": None,
                "error": None,
                "return_code": None,
                "signal": None,
            }
            state["active_agent_invocation"] = invocation
        else:
            active = state.get("active_agent_invocation")
            if not isinstance(active, dict):
                raise ValueError("active Agent Invocation is missing")
            invocation = active
            invocation.update(facts)
            if kind == "thread_started":
                requested = invocation.get("requested_thread_id")
                if requested is not None and requested != invocation.get(
                    "reported_thread_id"
                ):
                    error = "reported Thread ID does not match requested Thread ID"
                    invocation.update(
                        {"status": "failed", "ended_at": now, "error": error}
                    )
                    history = state.setdefault("agent_invocation_history", [])
                    if not isinstance(history, list):
                        raise ValueError("agent_invocation_history must be an array")
                    history.append(dict(invocation))
                    state.update(
                        {
                            "status": "execution_failed",
                            "terminal_kind": "execution_failed",
                            "diagnostics": [
                                {"code": "agent_invocation_failed", "message": error}
                            ],
                        }
                    )
                    save(state)
                    raise ValueError(error)
            elif kind in {"completed", "failed"}:
                invocation["status"] = kind
                invocation["ended_at"] = now
                history = state.setdefault("agent_invocation_history", [])
                if not isinstance(history, list):
                    raise ValueError("agent_invocation_history must be an array")
                history.append(dict(invocation))
                if kind == "failed":
                    state.update(
                        {
                            "status": "execution_failed",
                            "terminal_kind": "execution_failed",
                            "diagnostics": [
                                {
                                    "code": "agent_invocation_failed",
                                    "message": str(
                                        invocation.get("error")
                                        or "Codex invocation failed"
                                    ),
                                }
                            ],
                        }
                    )
        save(state)

    return record


def fail_interrupted_invocation(
    state: dict[str, Any], *, role: str, save: Callable[[dict[str, Any]], object]
) -> bool:
    """Close a durable running Invocation after its controller disappeared."""

    active = state.get("active_agent_invocation")
    if (
        not isinstance(active, dict)
        or active.get("role") != role
        or active.get("status") != "running"
    ):
        return False
    failed = dict(active)
    failed.update(
        {
            "status": "failed",
            "ended_at": datetime.now(UTC).isoformat(),
            "error": "controller_interrupted",
        }
    )
    history = state.setdefault("agent_invocation_history", [])
    if not isinstance(history, list):
        raise ValueError("agent_invocation_history must be an array")
    history.append(failed)
    state["active_agent_invocation"] = failed
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "diagnostics": [
                {
                    "code": "agent_invocation_failed",
                    "message": "controller_interrupted",
                }
            ],
        }
    )
    save(state)
    return True
