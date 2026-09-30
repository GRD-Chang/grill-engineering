"""Bounded supervision inside the current Executor's development interval."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TYPE_CHECKING

from agent_run.external_supervision import ExternalSupervisor, is_supervised_wait
from agent_run.ticket_state import RUN_STOP_STATUSES, TicketState

if TYPE_CHECKING:
    from agent_run.delivery import TicketDeliveryEngine


def development_wait_due(
    engine: TicketDeliveryEngine, run_id: str, number: int, *, already_ready: bool = False,
) -> bool | None:
    """Observe admitted development waits without external I/O or Agent work.

    None means this Ticket no longer has an automatic development wait. This
    local check stays responsive even while the integration observer is in I/O.
    """
    scoped = TicketState(engine.states, run_id, number)
    state = scoped.load_current_run(run_id)
    if state is None or state.get("status") in RUN_STOP_STATUSES:
        return None
    job = state["active_ticket_job"]
    if job.get("phase") not in {
        "developing", "repairing", "committing_candidate", "candidate", "reviewing",
    } or not isinstance(job.get("external_wait"), dict):
        return None
    due = _retry_due(scoped, state, already_ready=already_ready)
    return None if state.get("status") == "blocked" else due


def _retry_due(scoped: TicketState, state: dict[str, Any], *, already_ready: bool = False) -> bool:
    from agent_run.ticket_pause import can_pause_ticket, pause_ticket

    job = state["active_ticket_job"]
    if str(job["ticket_number"]) in state["ticket_pause_requests"] and can_pause_ticket(job):
        pause_ticket(state, job)
        scoped.save_run(str(state["run_id"]), state)
        return False
    previous = deepcopy(state.get("supervision_window"))
    supervisor = ExternalSupervisor()
    if already_ready and isinstance(previous, dict) and supervisor.now() < previous["deadline"]:
        # A ready retry waiting for Agent capacity keeps its FIFO position;
        # it does not spend another retry, but its absolute deadline still runs.
        return True
    due = supervisor.retry_due(state)
    if state.get("status") == "supervision_timeout":
        pause_ticket_supervision(scoped, state)
        return False
    if previous != state.get("supervision_window"):
        scoped.save_run(str(state["run_id"]), state)
    return due


def supervise_ticket(engine: TicketDeliveryEngine, run_id: str, lanes: frozenset[int]) -> bool:
    state = engine.states.load_current_run(run_id)
    if state is None or state.get("status") in RUN_STOP_STATUSES:
        return False
    number = state["ticket_integration_holder"]
    if number is None and state["ticket_integration_queue"]:
        number = state["ticket_integration_queue"][0]["ticket_number"]
    if type(number) is not int or number in lanes:
        return False
    scoped = TicketState(engine.states, run_id, number)
    projected = scoped.load_current_run(run_id)
    if projected is None:
        return False
    if is_supervised_wait(projected) and not _retry_due(scoped, projected):
        return False
    result = engine.deliver_ticket(run_id, number)
    return bool(result["active_ticket_job"].get("phase") == "completed")


def pause_ticket_supervision(scoped: TicketState, state: dict[str, Any]) -> None:
    """Persist a local timeout at either the batch or foreground boundary."""
    job = state["active_ticket_job"]
    job["execution_resume_phase"] = job["phase"]
    job["supervision_wait"] = state["supervision_wait"]
    job["phase"] = "blocked"
    job["blocked_reason"] = "ticket_supervision_timeout"
    state["status"] = "blocked"
    state["terminal_kind"] = None
    for diagnostic in state.get("diagnostics", []):
        if diagnostic.get("code") == "supervision_timeout":
            diagnostic["next_action"] = f"agent-run resume {state['run_id']} --ticket {job['ticket_number']}"
    scoped.save_run(str(state["run_id"]), state)
