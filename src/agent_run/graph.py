from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_run.models import DeliveryGraph, Issue
from agent_run.revisions import fingerprint
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
    frontier: list[int] = []
    for number in order:
        issue = graph.issues[number]
        eligible, reason = _eligibility(issue)
        if eligible:
            frontier.append(number)
        tickets[str(number)] = _ticket_state(issue, eligible, reason)

    ticket_jobs = _retained_ticket_jobs(previous, order)
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
        unresolved = active or _first_unresolved_blocked_job(
            ticket_jobs, order
        )
        if unresolved is not None:
            status = (
                "escalating"
                if active is not None
                else "blocked"
            )
            diagnostics = [_job_diagnostic(unresolved)]
        else:
            status = "progress_exhausted"
            diagnostics = [
                {
                    "code": "no_executable_ticket",
                    "message": (
                        "No open, ready and unblocked Ticket is executable"
                    ),
                }
            ]

    state = dict(previous)
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
            "ticket_graph": _ticket_graph_state(graph, order, tickets),
            "frontier": frontier,
            "active_ticket_job": active,
            "ticket_jobs": ticket_jobs,
            "status": status,
            "diagnostics": diagnostics,
            "updated_at": _now(),
        }
    )
    return state


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


def _first_unresolved_blocked_job(
    ticket_jobs: dict[str, dict[str, Any]], order: list[int]
) -> dict[str, Any] | None:
    for number in order:
        job = ticket_jobs.get(str(number))
        if (
            isinstance(job, dict)
            and job.get("phase") == TicketPhase.BLOCKED.value
            and job.get("blocked_reason") != "no_code_changes"
        ):
            return job
    return None


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
    revision_input = {
        "tickets": order,
        "dependencies": {
            str(number): sorted(
                blocker.number for blocker in graph.issues[number].blocked_by
            )
            for number in order
            if number in graph.issues
        },
    }
    return {
        "revision": fingerprint(revision_input),
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
    if "ready-for-agent" not in issue.labels:
        return False, "missing_ready_for_agent"
    disqualifying = sorted(issue.labels & DISQUALIFYING_LABELS)
    if disqualifying:
        return False, f"disqualifying_label:{disqualifying[0]}"
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
