"""Durable Ticket Stop intent and the shared local pause transition."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_run.final_approval_operation import has_final_approval
from agent_run.sqlite_encoding import copy_json_tree
from agent_run.state import StateStore
from agent_run.ticket_queue import reconcile


class TicketStopRequested(Exception):
    """The owning invocation must unwind after terminating its Worker."""


def can_pause_ticket(job: dict[str, Any]) -> bool:
    if job.get("phase") in {"completed", "merged", "blocked", "escalating", "publication_pending"}:
        return False
    # A sent remote write cannot be cancelled. Keep its seat until reconciled.
    if job.get("ticket_write_intent") is not None:
        return False
    if job.get("phase") in {"merging", "waiting_merge"}:
        intent = job.get("merge_intent")
        return isinstance(intent, dict) and intent.get("attempts") == 0
    return True


def pause_ticket(state: dict[str, Any], job: dict[str, Any]) -> None:
    job["pause_resume_phase"] = (
        "waiting_checks" if job["phase"] in {"merging", "waiting_merge"} else job["phase"]
    )
    job["phase"] = "blocked"
    job["blocked_reason"] = "ticket_paused"
    job.pop("external_wait", None)
    reconcile(state, job)
    state["ticket_pause_requests"].pop(str(job["ticket_number"]), None)
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and active.get("ticket_number") == job["ticket_number"]:
        state["active_ticket_job"] = job
        state.update({"status": "blocked", "terminal_kind": None, "diagnostics": [{
            "code": "ticket_paused",
            "message": "Ticket paused; explicit resume is required",
            "ticket_number": job["ticket_number"],
        }]})
        for key in ("supervision_window", "supervision_wait"):
            state.pop(key, None)


def request_ticket_pause(
    states: StateStore, run_id: str, ticket_number: int
) -> dict[str, Any]:
    if type(ticket_number) is not int or ticket_number < 1:
        raise ValueError("Ticket number must be positive")

    def commit() -> dict[str, Any]:
        state = states.load_work_run(run_id, ticket_number)
        if state is None:
            raise ValueError("Delivery Run does not exist")
        baseline = copy_json_tree(state)
        if has_final_approval(state):
            raise ValueError("Final approval is in progress; Ticket pause is unavailable")
        if state.get("status") in {
            "completed", "abandoned", "operator_stopped", "requeue_required",
            "unsupported_scope_change", "deterministic_contradiction",
        }:
            raise ValueError("Delivery Run cannot accept a Ticket pause")
        jobs = state.get("ticket_jobs")
        job = jobs.get(str(ticket_number)) if isinstance(jobs, dict) else None
        if isinstance(job, dict) and job.get("phase") in {"blocked", "completed", "publication_pending"}:
            # Repeated Stop and an already established independent gate are
            # no-ops: Stop must never replace budget/human authorization.
            return state
        if str(ticket_number) in state["ticket_pause_requests"]:
            return state
        queued = any(item["ticket_number"] == ticket_number for item in state["ticket_work_queue"])
        if (not isinstance(job, dict) or job.get("phase") is None) and not queued:
            raise ValueError("Ticket has no active delivery work to pause")
        requests = state["ticket_pause_requests"]
        if str(ticket_number) not in requests:
            requests[str(ticket_number)] = datetime.now(UTC).isoformat()
            state["ticket_work_queue"] = [
                item for item in state["ticket_work_queue"]
                if item["ticket_number"] != ticket_number or item["lease_id"] is not None
            ]
            if (
                isinstance(job, dict) and job.get("phase") == "ready_for_integration"
                and state.get("ticket_integration_holder") != ticket_number
            ):
                pause_ticket(state, job)
            states._save_work_and_notify_unlocked(
                run_id, state, baseline, ticket_number=ticket_number,
            )
        return state

    states._commit_run(commit)
    # The public operation returns the complete query view after releasing
    # submission locks; its business commit only used the target's objects.
    saved = states.load_current_run(run_id)
    assert saved is not None
    return saved
