from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from agent_run.models import DeliveryGraph, Issue


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

    if frontier:
        status = "active"
        active: dict[str, Any] | None = {
            "ticket_number": frontier[0],
            "selection_reason": (
                "first eligible ticket by parent sub-issue order, then issue number"
            ),
        }
        diagnostics: list[dict[str, Any]] = []
    else:
        status = "progress_exhausted"
        active = None
        diagnostics = [
            {
                "code": "no_executable_ticket",
                "message": "No open, ready and unblocked Ticket is executable",
            }
        ]

    state = dict(previous)
    state.update(
        {
            "parent": {
                "number": graph.parent.number,
                "title": graph.parent.title,
                "revision": _fingerprint(
                    {"title": graph.parent.title, "body": graph.parent.body}
                ),
            },
            "ticket_graph": _ticket_graph_state(graph, order, tickets),
            "frontier": frontier,
            "active_ticket_job": active,
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
    state.update(
        {
            "parent": {
                "number": graph.parent.number,
                "title": graph.parent.title,
                "revision": _fingerprint(
                    {"title": graph.parent.title, "body": graph.parent.body}
                ),
            },
            "ticket_graph": _ticket_graph_state(
                graph, ordered_numbers, tickets
            ),
            "frontier": [],
            "active_ticket_job": None,
            "status": "blocked",
            "diagnostics": [diagnostic],
            "updated_at": _now(),
        }
    )
    return state


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
        "revision": _fingerprint(revision_input),
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
        "state": issue.state,
        "labels": sorted(issue.labels),
        "blocked_by": [
            {"number": blocker.number, "state": blocker.state}
            for blocker in sorted(issue.blocked_by, key=lambda value: value.number)
        ],
        "content_revision": _fingerprint(
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


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
