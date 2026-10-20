from __future__ import annotations

"""Durable Change Delivery state persistence and shape validation."""

from dataclasses import dataclass
from typing import Any

from agent_run.state import StateStore


@dataclass(frozen=True)
class ChangeDeliveryStateStore:
    """Persist one Run through the existing atomic StateStore seam."""

    states: StateStore
    run_id: str

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(self.run_id, state)
        return state


def save_change_job(
    store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any] | None, *, label: str, development_only: bool,
) -> dict[str, Any]:
    """Commit the exact Change with its existing Ticket integration admission."""
    if label.startswith("ticket-") and job is not None:
        from agent_run.ticket_queue import acquire, enqueue, reconcile

        if job.get("phase") == "accepted":
            if development_only:
                enqueue(state, job)
            else:
                acquire(state, job)
        reconcile(state, job)
    return store.save(state)


def block_change_job(
    store: ChangeDeliveryStateStore, state: dict[str, Any], job: dict[str, Any],
    *, label: str, development_only: bool, code: str, message: str,
) -> bool:
    """Retain the continuation before committing a Change's local gate."""
    if job.get("phase") != "blocked":
        job["execution_resume_phase"] = job["phase"]
    job.update({"phase": "blocked", "blocked_reason": code})
    state["status"] = "blocked"
    state["diagnostics"] = [{"code": code, "message": message, "change_job": label}]
    save_change_job(store, state, job, label=label, development_only=development_only)
    return False


def require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def require_string_list(data: dict[str, Any], key: str) -> list[str]:
    from agent_run.work_projection import materialize_history

    value = materialize_history(data, key)
    if value is None and key not in data:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must contain strings")
    return list(value)
