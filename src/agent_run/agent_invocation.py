from __future__ import annotations

from datetime import UTC, datetime
from copy import deepcopy
from typing import Any, Callable

from agent_run.semantic_attempt import canonical_fingerprint as canonical_fingerprint
from agent_run.resume_audit import bind_resume_to_successor


def _sync_invocation_history(
    state: dict[str, Any], invocation: dict[str, Any]
) -> None:
    """Keep one live history snapshot for each started Invocation."""

    history = state.setdefault("agent_invocation_history", [])
    if not isinstance(history, list):
        raise ValueError("agent_invocation_history must be an array")
    started_at = invocation.get("started_at")
    for index in range(len(history) - 1, -1, -1):
        previous = history[index]
        if (
            isinstance(previous, dict)
            and previous.get("started_at") == started_at
            and previous.get("work_subject") == invocation.get("work_subject")
        ):
            history[index] = dict(invocation)
            return
    history.append(dict(invocation))


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
    semantic_attempt: dict[str, Any],
    save: Callable[[dict[str, Any]], object],
) -> Callable[..., None]:
    """Persist the small, durable facts for one active Agent Invocation."""

    def record(kind: str, **facts: object) -> None:
        now = datetime.now(UTC).isoformat()
        if kind == "started":
            resume_binding = bind_resume_to_successor(
                state,
                semantic_attempt=semantic_attempt,
                successor_started_at=now,
            )
            resume_id, resume_sequence = (
                resume_binding if resume_binding is not None else (None, None)
            )
            invocation: dict[str, Any] = {
                "work_subject": work_subject,
                "generation": generation,
                "role": role,
                "phase": phase,
                "mode": facts.get("invocation_mode")
                or ("resume" if facts.get("requested_thread_id") else "fresh"),
                "input_fingerprint": canonical_fingerprint(invocation_input),
                "currentness_boundary": dict(currentness_boundary),
                "semantic_attempt": deepcopy(semantic_attempt),
                "status": "running",
                "requested_thread_id": facts.get("requested_thread_id"),
                "reported_thread_id": None,
                "binding_id": facts.get("binding_id"),
                "binding_role": facts.get("binding_role"),
                "profile_role": facts.get("profile_role"),
                "invocation_role": facts.get("invocation_role"),
                "model": facts.get("model"),
                "reasoning_effort": facts.get("reasoning_effort"),
                "profile_revision": facts.get("profile_revision"),
                "thread_execution_binding": facts.get("thread_execution_binding"),
                "attempt_count": 0,
                "started_at": now,
                "ended_at": None,
                "error": None,
                "return_code": None,
                "signal": None,
                "resume_id": resume_id,
                "resume_sequence": resume_sequence,
            }
            state["active_agent_invocation"] = invocation
            _sync_invocation_history(state, invocation)
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
                    _sync_invocation_history(state, invocation)
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
            _sync_invocation_history(state, invocation)
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
    state["active_agent_invocation"] = failed
    _sync_invocation_history(state, failed)
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
