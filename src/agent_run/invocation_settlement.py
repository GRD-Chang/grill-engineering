"""Persist only the facts of an already admitted Invocation after its fence closes."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import copy_json_tree

if TYPE_CHECKING:
    from agent_run.state import StateStore


@dataclass
class InvocationSettlement:
    run_id: str
    limited: bool = False
    commits: int = 0


_settlement: ContextVar[InvocationSettlement | None] = ContextVar("invocation_settlement", default=None)


def current_settlement() -> InvocationSettlement | None:
    return _settlement.get()


@contextmanager
def settlement_scope(state: dict[str, Any]) -> Iterator[None]:
    # A backend without an Invocation recorder still uses the ordinary save.
    if not isinstance(state.get("active_agent_invocation"), dict):
        yield
        return
    token = _settlement.set(InvocationSettlement(str(state["run_id"])))
    try:
        yield
    finally:
        _settlement.reset(token)


def commit_settlement(
    store: StateStore, run_id: str, proposed: dict[str, Any], *, ticket_number: int | None = None,
) -> dict[str, Any]:
    """Join the caller's write transaction; this never advances a stage or grants a lease."""
    from agent_run.agent_invocation import _sync_invocation_history
    from agent_run.semantic_attempt import semantic_attempt_subjects
    from agent_run.state import ConcurrentRunStateError

    permission = current_settlement()
    if permission is None or permission.run_id != run_id or not store._repository.in_transaction:
        raise ConcurrentRunStateError("Invocation closeout requires its exact transaction")
    loaded = store._load_work_for_update(run_id, ticket_number, thin_timeline=True)
    if loaded is None:
        raise ConcurrentRunStateError("Invocation Run disappeared before closeout")
    latest, baseline = loaded
    invocation = proposed.get("active_agent_invocation")
    current = (latest.get("ticket_agent_invocations", {}).get(str(ticket_number))
               if ticket_number is not None else latest.get("active_agent_invocation"))
    if not isinstance(invocation, dict) or not isinstance(current, dict):
        raise ConcurrentRunStateError("Invocation closeout lacks its current identity")
    identity_fields = (
        "work_subject", "started_at", "generation", "role", "phase", "input_fingerprint",
        "currentness_boundary", "semantic_attempt", "requested_thread_id",
        "resume_id", "resume_sequence",
        "binding_id", "binding_role", "profile_role", "invocation_role", "model",
        "reasoning_effort", "profile_revision", "thread_execution_binding",
        "predecessor_invocation_started_at",
    )
    if any(invocation.get(key) != current.get(key) for key in identity_fields):
        raise ConcurrentRunStateError("Invocation identity changed before closeout")
    if (current.get("reported_thread_id") is not None
            and invocation.get("reported_thread_id") != current["reported_thread_id"]):
        raise ConcurrentRunStateError("Invocation Thread changed before closeout")
    attempt = current.get("semantic_attempt")
    if not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str):
        raise ConcurrentRunStateError("Invocation closeout lacks its Semantic Attempt")
    owners = [
        owner for owner in semantic_attempt_subjects(latest)
        if isinstance(owner.get("pending_semantic_attempt"), dict)
        and owner["pending_semantic_attempt"].get("attempt_id") == attempt["attempt_id"]
    ]
    source_owners = [
        owner for owner in semantic_attempt_subjects(proposed)
        if isinstance(owner.get("pending_semantic_attempt"), dict)
        and owner["pending_semantic_attempt"].get("attempt_id") == attempt["attempt_id"]
    ]
    if not owners:
        if current == invocation:
            permission.commits += 1
            return latest
        raise ConcurrentRunStateError("Invocation Attempt is no longer pending")
    result = next((owner["pending_semantic_attempt"]["returned_result"] for owner in source_owners
                   if "returned_result" in owner["pending_semantic_attempt"]), None)
    if result is not None:
        from agent_run.agent_return import restore_agent_return
        for owner in owners:
            pending = owner["pending_semantic_attempt"]
            if any(pending.get(key) != attempt.get(key)
                   for key in ("attempt_id", "work_subject", "generation", "role")):
                raise ConcurrentRunStateError("Invocation Attempt ownership changed")
            restore_agent_return(result, pending)
            previous = pending.get("returned_result")
            if previous is not None and previous != result:
                raise ConcurrentRunStateError("Invocation already has a different saved result")
            pending["returned_result"] = copy_json_tree(result)
    settled = copy_json_tree(invocation)
    if current.get("status") in {"completed", "failed"} and invocation.get("status") in {"running", "resuming"}:
        for key in ("status", "ended_at", "error", "execution_interrupted", "interruption_observed_at"):
            if key in current:
                settled[key] = current[key]
            else:
                settled.pop(key, None)
    if current.get("status") == "completed" and settled != current:
        raise ConcurrentRunStateError("Completed Invocation cannot be replaced by a late observation")
    if ticket_number is not None:
        latest["ticket_agent_invocations"][str(ticket_number)] = settled
    selected = latest.get("active_agent_invocation")
    if isinstance(selected, dict) and (
        selected.get("work_subject"), selected.get("started_at")
    ) == (settled["work_subject"], settled["started_at"]):
        latest["active_agent_invocation"] = copy_json_tree(settled)
    _sync_invocation_history(latest, settled)
    permission.commits += 1
    return store._save_work_and_notify_unlocked(run_id, latest, baseline, ticket_number=ticket_number)
