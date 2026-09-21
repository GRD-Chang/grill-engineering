"""Shared read-only roles, work ownership and delivery progress. No persisted state."""
from __future__ import annotations

from typing import Any

_TERMINAL_CHANGE_JOB_PHASES = {"completed", "merged", "abandoned"}

_TERMINAL_RUN_ACCEPTANCE_PHASES = {"accepted", "completed"}

_CURRENT_PUBLICATION_GATE_PHASES = {
    "blocked",
    "ready_for_human",
    "publication_pending",
}


def ticket_progress(state: dict[str, Any]) -> dict[str, int]:
    graph = state.get("ticket_graph")
    tickets = graph.get("tickets") if isinstance(graph, dict) else None
    total = len(tickets) if isinstance(tickets, dict) else 0
    jobs = state.get("ticket_jobs")
    graph_numbers = set(tickets) if isinstance(tickets, dict) else set()
    completed = (
        sum(
            1
            for number, job in jobs.items()
            if (not graph_numbers or str(number) in graph_numbers)
            if isinstance(job, dict)
            and job.get("phase") in {"completed", "merged"}
        )
        if isinstance(jobs, dict)
        else 0
    )
    return {"completed": completed, "total": total}



def role_family(role: str) -> str | None:
    if role == "development":
        return "development"
    if role in {"review", "reviewer", "fresh_acceptance"}:
        return "review"
    if role in {"publication", "final_publication"}:
        return "publication"
    return None



def current_work_subject(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Select the one Work Subject represented by the public status view."""

    active = state.get("active_ticket_job")
    if (
        isinstance(active, dict)
        and active.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        ticket_number = active.get("ticket_number")
        locator = (
            f"ticket:{ticket_number}"
            if isinstance(ticket_number, int)
            else "ticket:unknown"
        )
        return locator, active

    parent = state.get("parent_job")
    if (
        isinstance(parent, dict)
        and parent.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        return "parent", parent

    acceptance = state.get("run_acceptance")
    publication = state.get("run_publication")
    if (
        isinstance(publication, dict)
        and publication.get("phase") in _CURRENT_PUBLICATION_GATE_PHASES
    ):
        return "run_publication", publication

    if (
        isinstance(acceptance, dict)
        and acceptance.get("phase") not in _TERMINAL_RUN_ACCEPTANCE_PHASES
    ):
        repair = acceptance.get("repair_job")
        if acceptance.get("phase") == "repairing" and isinstance(repair, dict):
            return "run_repair", repair
        return "run_acceptance", acceptance

    if isinstance(publication, dict):
        return "run_publication", publication
    if isinstance(acceptance, dict):
        return "run_acceptance", acceptance
    return None



def delivery_object_label(
    state: dict[str, Any],
    locator: str,
    *,
    ticket_number: object = None,
    include_parent_for_run: bool = False,
) -> str:
    """Render one stable label for a delivery work subject or state location."""

    if locator.startswith("ticket:"):
        number = ticket_number or locator.split(":", 1)[1]
        return f"Ticket #{number}"
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if locator == "parent" or locator.startswith("parent-only:"):
        return f"Parent Issue #{parent_number}"
    if locator in {"run_acceptance", "run_repair"} or locator.startswith(
        ("run-acceptance:", "run-repair:")
    ):
        return "Run Acceptance"
    if locator == "run_publication" or locator.startswith("run-publication:"):
        return "Run Publication"
    if include_parent_for_run:
        return f"Delivery Run（Parent Issue #{parent_number}）"
    return "Delivery Run"



def active_ticket_job(state: dict[str, object]) -> dict[str, object] | None:
    active = state.get("active_ticket_job")
    if not isinstance(active, dict):
        return None
    if active.get("phase") in {"merged", "completed", "abandoned"}:
        return None
    return active



def current_delivery_identity(state: dict[str, object]) -> dict[str, object]:
    """Return the Candidate/PR boundary operators need for diagnosis."""

    active = active_ticket_job(state)
    if active is not None:
        return {
            "candidate_sha": active.get("candidate_sha"),
            "pr_number": active.get("pr_number"),
        }
    parent = state.get("parent_job")
    if isinstance(parent, dict) and parent.get("phase") not in {
        "completed",
        "merged",
        "abandoned",
    }:
        return {
            "candidate_sha": parent.get("candidate_sha"),
            "pr_number": parent.get("pr_number"),
        }
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            return {
                "candidate_sha": repair.get("candidate_sha"),
                "pr_number": repair.get("pr_number"),
            }
        candidate = acceptance.get("reviewed_head_sha") or acceptance.get(
            "candidate_sha"
        )
        publication = state.get("run_publication")
        pr_number = publication.get("pr_number") if isinstance(publication, dict) else None
        if candidate is not None or pr_number is not None:
            return {"candidate_sha": candidate, "pr_number": pr_number}
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        record = publication.get("record")
        return {
            "candidate_sha": (
                record.get("run_head_sha")
                if isinstance(record, dict)
                else publication.get("head_sha")
            ),
            "pr_number": publication.get("pr_number"),
        }
    return {"candidate_sha": None, "pr_number": None}



