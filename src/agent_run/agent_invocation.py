from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable


def invocation_event_recorder(
    state: dict[str, Any],
    *,
    role: str,
    phase: str,
    save: Callable[[dict[str, Any]], object],
) -> Callable[..., None]:
    """Persist the small, durable facts for one active Agent Invocation."""

    def record(kind: str, **facts: object) -> None:
        now = datetime.now(UTC).isoformat()
        if kind == "started":
            invocation: dict[str, Any] = {
                "role": role,
                "phase": phase,
                "mode": facts.get("invocation_mode")
                or ("resume" if facts.get("requested_thread_id") else "fresh"),
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
                    raise ValueError(
                        "reported Thread ID does not match requested Thread ID"
                    )
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
