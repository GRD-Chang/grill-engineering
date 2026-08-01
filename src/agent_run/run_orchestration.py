from __future__ import annotations

from typing import Any, Protocol

from agent_run.revisions import TicketGraphDriftError


class RunController(Protocol):
    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]: ...


class TicketEngine(Protocol):
    def deliver(self, run_id: str) -> dict[str, Any]: ...


class DeliveryRunEngine:
    """Advance one Delivery Run until no automatic work remains."""

    _REVISION_RESTART_REASONS = frozenset(
        {"effective_revision_mismatch", "merged_revision_mismatch"}
    )

    def __init__(
        self, *, controller: RunController, tickets: TicketEngine
    ) -> None:
        self.controller = controller
        self.tickets = tickets

    def deliver(self, run_id: str) -> dict[str, Any]:
        state, _ = self.controller.resume(run_id)
        while state.get("active_ticket_job") is not None:
            try:
                result = self.tickets.deliver(run_id)
            except TicketGraphDriftError:
                state, _ = self.controller.resume(run_id)
                return state
            if result.get("status") == "waiting_checks":
                return result
            state, _ = self.controller.resume(run_id)
            active = state.get("active_ticket_job")
            if not isinstance(active, dict):
                return state
            if active.get("phase") != "blocked":
                continue
            if active.get("blocked_reason") in self._REVISION_RESTART_REASONS:
                continue
            return state
        return state
