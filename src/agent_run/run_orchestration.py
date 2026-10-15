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

    def current_result(self, run_id: str) -> dict[str, Any]: ...

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
                # The dispatcher also owns integration-only and final Agent
                # work. Consume its result without starting a foreground lane.
                result = (
                    self.tickets.current_result(run_id) if developed
                    else self.tickets.deliver(run_id)
                )
            except TicketGraphDriftError:
                state, _ = self.controller.resume(run_id)
                return state
            if result.get("status") == "waiting_external" and developed:
                active_result = result.get("active_ticket_job") or {}
                wait = active_result.get("external_wait") or {}
                window = wait.get("supervision_window") or {}
                if window and window.get("next_retry_at") is None:
                    # A lost response has not had its first readback yet.
                    # Perform it through the same bounded observer; once it
                    # plans a retry, the Driver owns the persisted backoff.
                    state = result
                    continue
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


def parent_stage_state(
    state: dict[str, Any], *, parent_only: bool,
) -> dict[str, Any]:
    """Project the ready Parent's stage from its existing delivery records.

    Graph scheduling proves readiness; this layer interprets the business
    successor. Pause phases stay visible instead of using their saved
    continuation as authority to proceed.
    """
    from agent_run.publication_pending import publication_pending_diagnostic
    from agent_run.work_facts import work_phase

    diagnostics: list[dict[str, Any]] = []
    if parent_only:
        subject = state.get("parent_job")
        default_status = "parent_delivery_pending"
    else:
        acceptance = state.get("run_acceptance")
        subject = state.get("run_publication")
        default_status = (
            "run_publication_pending"
            if isinstance(acceptance, dict) and acceptance.get("phase") == "accepted"
            else "run_acceptance_pending"
        )
        if default_status == "run_acceptance_pending":
            subject = None

    phase = (
        subject.get("phase")
        if isinstance(subject, dict) and subject.get("phase") in {
            "blocked", "ready_for_human", "escalating",
        }
        else work_phase(subject) if isinstance(subject, dict) else None
    )
    if parent_only and isinstance(subject, dict) and subject.get("phase") in {
        "blocked", "escalating",
    }:
        status = "blocked"
        diagnostics = list(state.get("diagnostics", []))
    elif phase == "completed" and parent_only:
        status = "completed"
    elif phase == "ready_for_approval":
        status = "parent_approval_pending" if parent_only else "run_approval_pending"
    elif phase == "waiting_checks":
        status = "waiting_checks"
    elif phase == "publication_pending":
        status = "publication_pending"
        diagnostics = [publication_pending_diagnostic(
            subject_key="change_job" if parent_only else "delivery_run",
            subject="parent-only" if parent_only else str(state["run_id"]),
        )]
    elif phase == ("merging" if parent_only else "merged"):
        status = "parent_closeout_pending"
    else:
        status = default_status
    return {
        "status": status,
        "diagnostics": diagnostics,
        "terminal_kind": (
            "all_tickets_completed" if status == "run_acceptance_pending"
            else "completed" if status == "completed"
            else "publication_pending" if status == "publication_pending"
            else None
        ),
    }
