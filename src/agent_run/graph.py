from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_run.models import DeliveryGraph, Issue
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.revisions import (
    effective_revision,
    fingerprint,
    ticket_graph_revision,
)
from agent_run.ticket_phase import BLOCKED_MESSAGES, TicketPhase


DISQUALIFYING_LABELS = frozenset(
    {"needs-triage", "needs-info", "ready-for-human"}
)


def state_from_graph(
    previous: dict[str, Any], graph: DeliveryGraph
) -> dict[str, Any]:
    missing = [
        number
        for number in graph.parent.sub_issue_numbers
        if number not in graph.issues
    ]
    if missing:
        number = min(missing)
        return _blocked_graph_state(
            previous,
            graph,
            {
                "code": "missing_ticket",
                "message": f"GitHub did not return sub-issue #{number}",
                "ticket_number": number,
            },
        )

    order = list(graph.parent.sub_issue_numbers)
    if not graph.parent.sub_issue_order_reliable:
        order.sort()
    cycles = _cycle_members(graph)
    if cycles:
        return _blocked_graph_state(
            previous,
            graph,
            {
                "code": "dependency_cycle",
                "message": "Ticket DAG contains a dependency cycle",
                "ticket_numbers": cycles,
            },
            order,
        )

    tickets: dict[str, Any] = {}
    for number in order:
        issue = graph.issues[number]
        eligible, reason = _eligibility(issue)
        tickets[str(number)] = _ticket_state(issue, eligible, reason)

    ticket_jobs = _retained_ticket_jobs(previous, order)
    parent_revision = fingerprint(
        {"title": graph.parent.title, "body": graph.parent.body}
    )
    ticket_graph = _ticket_graph_state(graph, order, tickets)
    frontier = [
        number
        for number in order
        if tickets[str(number)]["eligibility"]["eligible"]
        and _job_is_executable(
            ticket_jobs.get(str(number)),
            ticket=tickets[str(number)],
            parent_revision=parent_revision,
            graph_revision=str(ticket_graph["revision"]),
        )
    ]
    active: dict[str, Any] | None
    if frontier:
        selected = frontier[0]
        active = ticket_jobs.get(str(selected), {"ticket_number": selected})
        active["selection_reason"] = (
            "first eligible ticket by parent sub-issue order, then issue number"
        )
        previous_graph = previous.get("ticket_graph")
        if str(selected) in ticket_jobs and isinstance(previous_graph, dict):
            previous_tickets = previous_graph.get("tickets")
            previous_ticket = (
                previous_tickets.get(str(selected))
                if isinstance(previous_tickets, dict)
                else None
            )
            if isinstance(previous_ticket, dict):
                active["source_revision_changed"] = (
                    previous_ticket.get("content_revision")
                    != tickets[str(selected)]["content_revision"]
                )
        ticket_jobs[str(selected)] = active
        if active.get("phase") == TicketPhase.ESCALATING.value:
            status = "escalating"
            diagnostics = [_job_diagnostic(active)]
        elif active.get("phase") == TicketPhase.PUBLICATION_PENDING.value:
            status = "publication_pending"
            diagnostics = [
                publication_pending_diagnostic(
                    subject_key="ticket_number", subject=active["ticket_number"]
                )
            ]
        elif (
            active.get("phase") == TicketPhase.BLOCKED.value
            and active.get("blocked_reason") != "no_code_changes"
        ):
            status = "blocked"
            diagnostics = [_job_diagnostic(active)]
        else:
            status = "active"
            diagnostics = []
    else:
        active = _first_job_in_phase(
            ticket_jobs, order, TicketPhase.ESCALATING
        )
        if active is None:
            active = _first_job_in_phase(
                ticket_jobs, order, TicketPhase.MERGED
            )
        if not order:
            parent_job = previous.get("parent_job")
            phase = parent_job.get("phase") if isinstance(parent_job, dict) else None
            if phase == "completed":
                status = "completed"
                diagnostics = []
            elif phase == "ready_for_approval":
                status = "parent_approval_pending"
                diagnostics = []
            elif phase == "waiting_checks":
                status = "waiting_checks"
                diagnostics = []
            elif phase == TicketPhase.PUBLICATION_PENDING.value:
                status = "publication_pending"
                diagnostics = [
                    publication_pending_diagnostic(
                        subject_key="change_job", subject="parent-only"
                    )
                ]
            elif phase == "merging":
                status = "parent_closeout_pending"
                diagnostics = []
            elif phase in {"blocked", "escalating"}:
                status = "blocked"
                diagnostics = list(previous.get("diagnostics", []))
            else:
                status = "parent_delivery_pending"
                diagnostics = []
        elif _all_ticket_jobs_completed(ticket_jobs, order):
            status, diagnostics = _run_completion_status(previous)
        elif active is not None:
            if active.get("phase") == TicketPhase.ESCALATING.value:
                status = "escalating"
                diagnostics = [_job_diagnostic(active)]
            else:
                status = "active"
                diagnostics = []
        else:
            status = "progress_exhausted"
            remaining = _remaining_ticket_reasons(
                tickets, ticket_jobs, order
            )
            diagnostics = [
                {
                    "code": "no_executable_ticket",
                    "message": (
                        "No open, ready and unblocked Ticket is executable"
                    ),
                    "remaining_tickets": remaining,
                }
            ]

    state = dict(previous)
    state.update(
        {
            "parent": {
                "number": graph.parent.number,
                "title": graph.parent.title,
                "body": graph.parent.body,
                "revision": parent_revision,
            },
            "ticket_graph": ticket_graph,
            "frontier": frontier,
            "active_ticket_job": active,
            "ticket_jobs": ticket_jobs,
            "status": status,
            "diagnostics": diagnostics,
            "terminal_kind": (
                "all_tickets_completed"
                if status == "run_acceptance_pending"
                else "completed" if status == "completed"
                else "publication_pending" if status == "publication_pending"
                else (
                    _exhaustion_kind(diagnostics[0]["remaining_tickets"])
                    if status == "progress_exhausted"
                    else None
                )
            ),
            "updated_at": _now(),
        }
    )
    return state


def _run_completion_status(previous: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    acceptance = previous.get("run_acceptance")
    publication = previous.get("run_publication")
    if (
        not isinstance(acceptance, dict)
        or acceptance.get("phase") != "accepted"
    ):
        return "run_acceptance_pending", []
    if not isinstance(publication, dict):
        return "run_publication_pending", []
    phase = publication.get("phase")
    if phase == "publication_pending":
        return (
            "publication_pending",
            [
                publication_pending_diagnostic(
                    subject_key="delivery_run", subject=str(previous["run_id"])
                )
            ],
        )
    if phase == "waiting_checks":
        return "waiting_checks", []
    if phase == "ready_for_approval":
        return "run_approval_pending", []
    if phase == "merged":
        return "parent_closeout_pending", []
    return "run_publication_pending", []


def _blocked_graph_state(
    previous: dict[str, Any],
    graph: DeliveryGraph,
    diagnostic: dict[str, Any],
    order: list[int] | None = None,
) -> dict[str, Any]:
    ordered_numbers = order or list(graph.parent.sub_issue_numbers)
    tickets = {
        str(number): _ticket_state(issue, False, "graph_invalid")
        for number, issue in graph.issues.items()
    }
    state = dict(previous)
    ticket_jobs = _retained_ticket_jobs(previous, ordered_numbers)
    state.update(
        {
            "parent": {
                "number": graph.parent.number,
                "title": graph.parent.title,
                "body": graph.parent.body,
                "revision": fingerprint(
                    {"title": graph.parent.title, "body": graph.parent.body}
                ),
            },
            "ticket_graph": _ticket_graph_state(
                graph, ordered_numbers, tickets
            ),
            "frontier": [],
            "active_ticket_job": None,
            "ticket_jobs": ticket_jobs,
            "status": "blocked",
            "terminal_kind": "permanent_blocked",
            "diagnostics": [diagnostic],
            "updated_at": _now(),
        }
    )
    return state


def _retained_ticket_jobs(
    previous: dict[str, Any], order: list[int]
) -> dict[str, dict[str, Any]]:
    allowed = {str(number) for number in order}
    retained: dict[str, dict[str, Any]] = {}
    previous_jobs = previous.get("ticket_jobs")
    if isinstance(previous_jobs, dict):
        for key, value in previous_jobs.items():
            if key in allowed and isinstance(value, dict):
                retained[key] = dict(value)
    previous_active = previous.get("active_ticket_job")
    if isinstance(previous_active, dict):
        number = previous_active.get("ticket_number")
        key = str(number)
        if isinstance(number, int) and key in allowed:
            retained[key] = dict(previous_active)
    return retained


def _first_job_in_phase(
    ticket_jobs: dict[str, dict[str, Any]],
    order: list[int],
    phase: TicketPhase,
) -> dict[str, Any] | None:
    for number in order:
        job = ticket_jobs.get(str(number))
        if isinstance(job, dict) and job.get("phase") == phase.value:
            return job
    return None


def _remaining_ticket_reasons(
    tickets: dict[str, Any],
    ticket_jobs: dict[str, dict[str, Any]],
    order: list[int],
) -> list[dict[str, Any]]:
    remaining: list[dict[str, Any]] = []
    for number in order:
        job = ticket_jobs.get(str(number))
        if isinstance(job, dict) and job.get("phase") == TicketPhase.COMPLETED:
            continue
        reason = job.get("blocked_reason") if isinstance(job, dict) else None
        if not isinstance(reason, str):
            eligibility = tickets[str(number)]["eligibility"]
            reason = str(eligibility["reason"])
        remaining.append({"ticket_number": number, "reason": reason})
    return remaining


def _exhaustion_kind(remaining: list[dict[str, Any]]) -> str:
    human_reasons = {
        "reviewer_requires_human",
        "modification_budget_exhausted",
        "ticket_pr_closed_unmerged",
        "unexpected_external_merge",
        "published_head_mismatch",
        "acceptance_record_mismatch",
        "merged_result_mismatch",
        "no_code_changes",
    }
    if any(
        item.get("reason") in human_reasons
        or str(item.get("reason", "")).startswith("disqualifying_label:")
        for item in remaining
    ):
        return "waiting_human"
    return "temporarily_no_work"


def _all_ticket_jobs_completed(
    ticket_jobs: dict[str, dict[str, Any]], order: list[int]
) -> bool:
    return bool(order) and all(
        ticket_jobs.get(str(number), {}).get("phase")
        == TicketPhase.COMPLETED.value
        for number in order
    )


def _job_is_executable(
    job: dict[str, Any] | None,
    *,
    ticket: dict[str, Any],
    parent_revision: str,
    graph_revision: str,
) -> bool:
    if job is None:
        return True
    phase = job.get("phase")
    if phase == TicketPhase.COMPLETED.value:
        return False
    if phase != TicketPhase.BLOCKED.value:
        return True
    if job.get("blocked_reason") == "effective_revision_mismatch":
        # The blocker itself is durable evidence that prior artifacts were
        # invalidated. A later ABA edit can restore the same fingerprint, but
        # it must not resurrect the discarded Candidate or Acceptance.
        return True
    expected = effective_revision(
        ticket_revision=str(ticket["content_revision"]),
        parent_revision=parent_revision,
        graph_revision=graph_revision,
    )
    current = job.get("effective_revision")
    return isinstance(current, str) and current != expected


def _job_diagnostic(job: dict[str, Any]) -> dict[str, Any]:
    reason = job.get("blocked_reason") or job.get("escalation_code")
    if not isinstance(reason, str) or reason not in BLOCKED_MESSAGES:
        raise ValueError("unresolved Ticket Job has unknown blocker")
    return {
        "code": reason,
        "message": BLOCKED_MESSAGES[reason],
        "ticket_number": job["ticket_number"],
    }


def _ticket_graph_state(
    graph: DeliveryGraph,
    order: list[int],
    tickets: dict[str, Any],
) -> dict[str, Any]:
    return {
        "revision": ticket_graph_revision(graph),
        "ordered_ticket_numbers": order,
        "order_source": (
            "github_sub_issues"
            if graph.parent.sub_issue_order_reliable
            else "issue_number_fallback"
        ),
        "tickets": tickets,
    }


def _ticket_state(issue: Issue, eligible: bool, reason: str) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": sorted(issue.labels),
        "blocked_by": [
            {"number": blocker.number, "state": blocker.state}
            for blocker in sorted(issue.blocked_by, key=lambda value: value.number)
        ],
        "content_revision": fingerprint(
            {"title": issue.title, "body": issue.body}
        ),
        "eligibility": {"eligible": eligible, "reason": reason},
    }


def _eligibility(issue: Issue) -> tuple[bool, str]:
    if issue.state.upper() != "OPEN":
        return False, "ticket_closed"
    disqualifying = sorted(issue.labels & DISQUALIFYING_LABELS)
    if disqualifying:
        return False, f"disqualifying_label:{disqualifying[0]}"
    if "ready-for-agent" not in issue.labels:
        return False, "missing_ready_for_agent"
    if any(
        blocker.state.upper() != "CLOSED" for blocker in issue.blocked_by
    ):
        return False, "blocked_by_open_issues"
    return True, "eligible"


def _cycle_members(graph: DeliveryGraph) -> list[int]:
    ticket_numbers = set(graph.parent.sub_issue_numbers)
    dependencies = {
        number: {
            blocker.number
            for blocker in graph.issues[number].blocked_by
            if blocker.number in ticket_numbers
        }
        for number in ticket_numbers
        if number in graph.issues
    }
    visiting: set[int] = set()
    visited: set[int] = set()
    cycle_nodes: set[int] = set()

    def visit(number: int, path: list[int]) -> None:
        if number in visiting:
            cycle_start = path.index(number)
            cycle_nodes.update(path[cycle_start:])
            return
        if number in visited:
            return
        visiting.add(number)
        path.append(number)
        for dependency in dependencies.get(number, set()):
            visit(dependency, path)
        path.pop()
        visiting.remove(number)
        visited.add(number)

    for number in sorted(dependencies):
        visit(number, [])
    return sorted(cycle_nodes)


def _now() -> str:
    return datetime.now(UTC).isoformat()
