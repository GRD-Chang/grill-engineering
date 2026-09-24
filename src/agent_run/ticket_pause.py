"""Durable local pause requests consumed at Ticket delivery safe boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_run.final_approval_operation import has_final_approval
from agent_run.state import StateStore
from agent_run.ticket_queue import reconcile


def request_ticket_pause(
    states: StateStore, run_id: str, ticket_number: int
) -> dict[str, Any]:
    if type(ticket_number) is not int or ticket_number < 1:
        raise ValueError("Ticket number must be positive")

    def commit() -> dict[str, Any]:
        state = states.load_current_run(run_id)
        if state is None:
            raise ValueError("Delivery Run does not exist")
        if has_final_approval(state):
            raise ValueError("Final approval is in progress; Ticket pause is unavailable")
        if state.get("status") in {
            "completed", "abandoned", "operator_stopped", "requeue_required",
            "unsupported_scope_change", "deterministic_contradiction",
        }:
            raise ValueError("Delivery Run cannot accept a Ticket pause")
        jobs = state.get("ticket_jobs")
        job = jobs.get(str(ticket_number)) if isinstance(jobs, dict) else None
        if not isinstance(job, dict) or job.get("phase") in {
            None, "blocked", "completed", "merged",
        }:
            raise ValueError("Ticket has no active delivery work to pause")
        requests = state["ticket_pause_requests"]
        if str(ticket_number) not in requests:
            requests[str(ticket_number)] = datetime.now(UTC).isoformat()
            if (
                job.get("phase") == "ready_for_integration"
                and state.get("ticket_integration_holder") != ticket_number
            ):
                job["pause_resume_phase"] = "ready_for_integration"
                job["phase"] = "blocked"
                job["blocked_reason"] = "ticket_paused"
                requests.pop(str(ticket_number), None)
                reconcile(state, job)
                active = state.get("active_ticket_job")
                if isinstance(active, dict) and active.get("ticket_number") == ticket_number:
                    state["active_ticket_job"] = job
                    state["status"] = "blocked"
                    state["diagnostics"] = [{
                        "code": "ticket_paused",
                        "message": "Ticket paused at a delivery boundary",
                        "ticket_number": ticket_number,
                    }]
            states._save_run_and_notify_unlocked(run_id, state)
        return state

    return states._commit_run(commit)
