from __future__ import annotations

from typing import Any, Callable, Protocol

from agent_run.operator_gate import (
    has_run_operator_gate,
    is_mechanical_revision_restart,
)
from agent_run.revisions import TicketGraphDriftError
from agent_run.ticket_queue import next_ticket
from agent_run.ticket_state import RUN_STOP_STATUSES
from agent_run.state import ConcurrentRunStateError
from agent_run.task_control import TaskControlBusyError


class RunController(Protocol):
    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]: ...


class TicketEngine(Protocol):
    def supervise(self, run_id: str, lanes: frozenset[int]) -> bool: ...

    def deliver(self, run_id: str) -> dict[str, Any]: ...

    def develop_frontier(
        self, run_id: str, state: dict[str, Any],
        *, after_lane: Callable[[frozenset[int]], None] | None = None,
        supervise: Callable[[frozenset[int]], None] | None = None,
        refresh_graph: Callable[[], None] | None = None,
    ) -> bool: ...


class DeliveryRunEngine:
    """Advance one Delivery Run until no automatic work remains."""

    def __init__(
        self, *, controller: RunController, tickets: TicketEngine
    ) -> None:
        self.controller = controller
        self.tickets = tickets

    def deliver(self, run_id: str) -> dict[str, Any]:
        state, _ = self.controller.resume(run_id)
        return self.deliver_from_state(run_id, state)

    def deliver_from_state(
        self, run_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        idle_tickets: set[int] = set()
        refresh_pending = False

        def supervise(lanes: frozenset[int]) -> None:
            nonlocal refresh_pending
            refresh_pending |= self.tickets.supervise(run_id, lanes) is True
            if refresh_pending:
                try:
                    self.controller.resume(run_id)
                except TaskControlBusyError as error:
                    if not isinstance(error.__cause__, ConcurrentRunStateError):
                        raise
                    # Other lanes may finish during the remote graph read.
                    # Keep the completion signal for the next bounded pass.
                else:
                    refresh_pending = False

        def refresh_graph() -> None:
            self.controller.resume(run_id)

        while state.get("active_ticket_job") is not None:
            if state.get("status") in RUN_STOP_STATUSES:
                return state
            parallel = getattr(self.tickets, "develop_frontier", None)
            try:
                developed = callable(parallel) and parallel(
                    run_id, state,
                    supervise=supervise, refresh_graph=refresh_graph,
                )
            except TicketGraphDriftError:
                state, _ = self.controller.resume(run_id)
                return state
            if developed:
                state, _ = self.controller.resume(run_id)
                if state.get("status") in {
                    "requeue_required", "operator_stopped", "execution_failed",
                    "unsupported_scope_change", "deterministic_contradiction",
                }:
                    return state
                if state.get("active_ticket_job") is None:
                    return state
                if state["active_ticket_job"].get("phase") == "blocked":
                    return state
            active_before = state["active_ticket_job"]
            number = active_before.get("ticket_number")
            ready_for_freed_seat = (
                active_before.get("phase") == "ready_for_integration"
                and state.get("ticket_integration_holder") is None
                and isinstance(state.get("ticket_integration_queue"), list)
                and bool(state["ticket_integration_queue"])
                and state["ticket_integration_queue"][0].get("ticket_number") == number
            )
            if isinstance(number, int) and number in idle_tickets and not ready_for_freed_seat:
                return state
            try:
                result = self.tickets.deliver(run_id)
            except TicketGraphDriftError:
                state, _ = self.controller.resume(run_id)
                return state
            if result.get("status") in {"requeue_required", "waiting_external"}:
                return result
            if (
                result.get("status") == "waiting_checks"
                and isinstance(number, int)
                and next_ticket(result, result.get("frontier", [])) == number
            ):
                # Preserve the external wait and its supervision window when
                # no other Ticket can advance. A refresh may otherwise
                # project the selected Job back to an ordinary active state.
                return result
            current_result_job = result.get("active_ticket_job")
            if (
                has_run_operator_gate(result)
                and not (
                    isinstance(current_result_job, dict)
                    and current_result_job.get("phase") == "completed"
                )
            ):
                return result
            job_after = result.get("active_ticket_job")
            if (
                isinstance(number, int)
                and isinstance(job_after, dict)
                and job_after.get("ticket_number") == number
                and job_after.get("phase") in {
                    "waiting_checks", "ready_for_integration", "blocked",
                    "publication_pending",
                }
            ):
                idle_tickets.add(number)
            state, _ = self.controller.resume(run_id)
            if state.get("status") == "requeue_required":
                # A Requeue command may launch one replacement Generation, but
                # a second drift in that same top-level command needs a fresh
                # maintainer decision rather than another in-place reset.
                return state
            active = state.get("active_ticket_job")
            if not isinstance(active, dict):
                return state
            if active.get("phase") == "blocked" and not is_mechanical_revision_restart(
                active.get("blocked_reason")
            ):
                return state
        return state
