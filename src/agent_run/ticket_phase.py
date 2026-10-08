from __future__ import annotations

from enum import StrEnum
from typing import Any


BLOCKED_MESSAGES = {
    "ci_investigation_paused": "CI investigation produced no code changes; explicit resume is required",
    "ticket_supervision_timeout": "Ticket external supervision timed out; explicit resume is required",
    "ticket_execution_failed": "Ticket execution failed; explicit resume is required",
    "ticket_paused": "Ticket was paused by the operator; explicit resume is required",
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
    "agent_requires_human": "Ticket requires explicit human intervention",
    "reviewer_requires_human": "Ticket requires explicit human intervention",
    "modification_budget_exhausted": (
        "Ticket requires explicit human intervention"
    ),
    "review_budget_exhausted": (
        "Ticket Review budget requires an explicit new window"
    ),
    "ci_development_budget_exhausted": "Ticket CI Development allowance requires explicit resume",
    "ci_inner_budget_exhausted": "Ticket CI repair allowance requires explicit resume",
    "ci_outer_budget_exhausted": "Ticket CI recovery allowance requires explicit resume",
    "ticket_close_external_conflict": (
        "Ticket close evidence conflicts with the Publisher intent"
    ),
    "integration_development_exhausted": "Ticket integration Development allowance requires explicit resume",
    "integration_resync_exhausted": "Ticket integration resync allowance requires explicit resume",
    "integration_history_rewritten": "Remote branch history was rewritten; Ticket integration paused",
    "integration_conflict_unresolved": "Ticket integration conflict requires intervention",
    "integration_snapshot_changed_during_conflict": "Remote branches changed during conflict resolution",
    "integration_record_invalid": "Ticket integration record is invalid",
    "remote_source_missing": "Ticket source ref is missing",
}


class TicketPhase(StrEnum):
    DEVELOPING = "developing"
    REPAIRING = "repairing"
    COMMITTING_CANDIDATE = "committing_candidate"
    CANDIDATE = "candidate"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    READY_FOR_INTEGRATION = "ready_for_integration"
    PUBLICATION_PENDING = "publication_pending"
    PUBLISHING = "publishing"
    WAITING_CHECKS = "waiting_checks"
    WAITING_MERGE = "waiting_merge"
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
