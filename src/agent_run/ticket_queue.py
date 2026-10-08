from __future__ import annotations

"""Durable, single-writer integration admission for Ticket Jobs."""

from datetime import UTC, datetime
from typing import Any


INTEGRATION_HOLDER_PHASES = frozenset({
    "accepted", "publishing", "waiting_checks",
    "waiting_merge", "merging", "merged",
})
DEVELOPMENT_WORKER_PHASES = frozenset({
    "developing", "repairing", "committing_candidate", "candidate", "reviewing",
})


def enqueue(state: dict[str, Any], job: dict[str, Any]) -> None:
    """Record the first readiness time for this candidate."""
    number = int(job["ticket_number"])
    queue = state["ticket_integration_queue"]
    if state["ticket_integration_holder"] == number:
        return
    if not any(item["ticket_number"] == number for item in queue):
        queue.append({"ticket_number": number, "ready_at": datetime.now(UTC).isoformat()})
        queue.sort(key=lambda item: (
            datetime.fromisoformat(item["ready_at"]).astimezone(UTC),
            item["ticket_number"],
        ))
    job["phase"] = "ready_for_integration"


def acquire(state: dict[str, Any], job: dict[str, Any]) -> bool:
    """Grant the only slot to the oldest ready Ticket, if available."""
    number = int(job["ticket_number"])
    holder = state["ticket_integration_holder"]
    if holder == number:
        return True
    resume_phase = job.get("integration_resume_phase") or "accepted"
    enqueue(state, job)
    queue = state["ticket_integration_queue"]
    if holder is not None or queue[0]["ticket_number"] != number:
        return False
    state["ticket_integration_holder"] = number
    queue.pop(0)
    job["phase"] = resume_phase
    job.pop("integration_resume_phase", None)
    return True


def reconcile(state: dict[str, Any], job: dict[str, Any]) -> None:
    """Release on repair/pause, preserving the candidate only when still ready."""
    number = int(job["ticket_number"])
    phase = job.get("phase")
    keep_slot = requires_integration_seat(job)
    if state["ticket_integration_holder"] == number and not keep_slot:
        state["ticket_integration_holder"] = None
    if phase != "ready_for_integration":
        state["ticket_integration_queue"] = [
            item for item in state["ticket_integration_queue"]
            if item["ticket_number"] != number
        ]


def requires_integration_seat(job: dict[str, Any]) -> bool:
    """An unresolved merge owns the seat even when its retries are paused."""
    phase = job.get("phase")
    if (
        (phase == "publication_pending" or (
            phase == "blocked"
            and job.get("blocked_reason") == "ticket_supervision_timeout"
        ))
        and job.get("execution_resume_phase") in {"merging", "merged"}
    ):
        return True
    return phase in INTEGRATION_HOLDER_PHASES


def reconcile_all(state: dict[str, Any]) -> None:
    jobs = state.get("ticket_jobs")
    if not isinstance(jobs, dict):
        return
    holder = state.get("ticket_integration_holder")
    if holder is not None and str(holder) not in jobs:
        state["ticket_integration_holder"] = None
    for job in jobs.values():
        if isinstance(job, dict) and type(job.get("ticket_number")) is int:
            reconcile(state, job)


def next_ticket(state: dict[str, Any], frontier: list[int]) -> int | None:
    """Pick one serial Agent task while a different Ticket may hold CI's slot."""
    holder = state["ticket_integration_holder"]
    jobs = state["ticket_jobs"]
    queue = state["ticket_integration_queue"]
    # An exhausted Publication operation is an operator pause, not runnable
    # work. It must not hide a holder that can still observe CI or a waiter.
    frontier = [
        number for number in frontier
        if jobs.get(str(number), {}).get("phase") not in {
            "blocked", "completed", "publication_pending",
        }
    ]
    for item in state.get("ticket_work_queue", []):
        if item["lease_id"] is None and item["ticket_number"] in frontier:
            return int(item["ticket_number"])
    if holder is None and queue:
        first = queue[0]["ticket_number"]
        if type(first) is int and first in frontier:
            return first
    active = state.get("active_ticket_job")
    active_number = active.get("ticket_number") if isinstance(active, dict) else None
    active_phase = active.get("phase") if isinstance(active, dict) else None
    if (
        type(active_number) is int
        and active_number in frontier
        and active_number != holder
        and active_phase not in {"ready_for_integration", "waiting_checks", "blocked"}
    ):
        return active_number
    for number in frontier:
        if number == holder:
            continue
        phase = jobs.get(str(number), {}).get("phase")
        if phase != "ready_for_integration":
            return number
    if type(holder) is int and holder in frontier:
        return holder
    return None


def has_other_ticket_work(state: dict[str, Any], number: int) -> bool:
    frontier = state.get("frontier")
    if not isinstance(frontier, list):
        return False
    jobs = state.get("ticket_jobs")
    if not isinstance(jobs, dict):
        return False
    return any(
        type(other) is int
        and other != number
        and jobs.get(str(other), {}).get("phase") not in {"blocked", "completed"}
        for other in frontier
    )
