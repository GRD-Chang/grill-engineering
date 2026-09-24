from __future__ import annotations

from typing import Any, Protocol

from agent_run.operator_gate import (
    has_run_operator_gate,
    is_mechanical_revision_restart,
)
from agent_run.revisions import TicketGraphDriftError
from agent_run.ticket_queue import next_ticket


class RunController(Protocol):
    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]: ...


class TicketEngine(Protocol):
    def deliver(self, run_id: str) -> dict[str, Any]: ...


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
        while state.get("active_ticket_job") is not None:
            active_before = state["active_ticket_job"]
            number = active_before.get("ticket_number")
            if isinstance(number, int) and number in idle_tickets:
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
