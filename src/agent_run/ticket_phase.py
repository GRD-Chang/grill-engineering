from __future__ import annotations

from enum import StrEnum
from typing import Any


BLOCKED_MESSAGES = {
    "ticket_pr_closed_unmerged": "Current Ticket PR was closed without merging",
    "unexpected_external_merge": (
        "Ticket PR merged without a persisted Publisher merge intent"
    ),
    "published_head_mismatch": "Published-Head Gate rejected live PR state",
    "effective_revision_mismatch": (
        "Published-Head Gate rejected stale requirements"
    ),
    "acceptance_record_mismatch": (
        "Published-Head Gate rejected a stale Acceptance Record"
    ),
    "merged_result_mismatch": (
        "Merged Ticket PR does not match Publisher merge intent"
    ),
    "merged_revision_mismatch": (
        "Merged Ticket PR was integrated, but no longer matches "
        "the current revision"
    ),
    "reviewer_requires_human": "Ticket requires explicit human intervention",
    "modification_budget_exhausted": (
        "Ticket requires explicit human intervention"
    ),
}


class TicketPhase(StrEnum):
    DEVELOPING = "developing"
    REPAIRING = "repairing"
    COMMITTING_CANDIDATE = "committing_candidate"
    CANDIDATE = "candidate"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    PUBLISHING = "publishing"
    WAITING_CHECKS = "waiting_checks"
    ESCALATING = "escalating"
    MERGING = "merging"
    MERGED = "merged"
    COMPLETED = "completed"
    BLOCKED = "blocked"


def parse_ticket_phase(value: object) -> TicketPhase:
    if not isinstance(value, str):
        raise ValueError("Ticket phase must be a string")
    try:
        return TicketPhase(value)
    except ValueError as error:
        raise ValueError(f"unknown Ticket phase: {value}") from error


def sync_active_ticket_job(state: dict[str, Any]) -> None:
    active = state.get("active_ticket_job")
    if not isinstance(active, dict):
        return
    ticket_number = active.get("ticket_number")
    if not isinstance(ticket_number, int):
        raise ValueError("active Ticket Job has invalid ticket_number")
    ticket_jobs = state.setdefault("ticket_jobs", {})
    if not isinstance(ticket_jobs, dict):
        raise ValueError("ticket_jobs must be an object")
    ticket_jobs[str(ticket_number)] = active
