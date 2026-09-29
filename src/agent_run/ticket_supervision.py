"""Bounded supervision inside the current Executor's development interval."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TYPE_CHECKING

from agent_run.external_supervision import ExternalSupervisor, is_supervised_wait
from agent_run.ticket_state import RUN_STOP_STATUSES, TicketState
from agent_run.revisions import TicketGraphDriftError
from agent_run.state import SimulatedProcessCrash
from agent_run.state_contract import IncompatibleRunStateError
from agent_run.task_control import TaskControlBusyError

if TYPE_CHECKING:
    from agent_run.delivery import TicketDeliveryEngine


def development_wait_due(
    engine: TicketDeliveryEngine, run_id: str, number: int,
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
    due = _retry_due(scoped, state)
    return None if state.get("status") == "blocked" else due


def _retry_due(scoped: TicketState, state: dict[str, Any]) -> bool:
    previous = deepcopy(state.get("supervision_window"))
    due = ExternalSupervisor().retry_due(state)
    if state.get("status") == "supervision_timeout":
        pause_ticket_supervision(scoped, state)
        return False
    if previous != state.get("supervision_window"):
        scoped.save_run(str(state["run_id"]), state)
    return due


def supervise_ticket(engine: TicketDeliveryEngine, run_id: str, lanes: frozenset[int]) -> None:
    state = engine.states.load_current_run(run_id)
    if state is None or state.get("status") in RUN_STOP_STATUSES:
        return
    number = state["ticket_integration_holder"]
    if number is None and state["ticket_integration_queue"]:
        number = state["ticket_integration_queue"][0]["ticket_number"]
    if type(number) is not int or number in lanes:
        return
    scoped = TicketState(engine.states, run_id, number)
    projected = scoped.load_current_run(run_id)
    if projected is None:
        return
    if is_supervised_wait(projected) and not _retry_due(scoped, projected):
        return
    from agent_run.delivery import TicketDeliveryEngine

    try:
        TicketDeliveryEngine(
            git=engine.git, states=scoped,  # type: ignore[arg-type]
            github=engine.github, agents=engine.agents,
        ).deliver(run_id)
    except (TaskControlBusyError, TicketGraphDriftError, IncompatibleRunStateError, SimulatedProcessCrash):
        raise
    except Exception as error:
        scoped.block_execution_failure(error)


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
