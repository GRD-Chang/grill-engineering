from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


_UNSUPPORTED_GRAPH_CHANGE_MESSAGE = (
    "Ticket 集合或 blockedBy 依赖已变化；当前 MVP 不支持吸收或"
    "自动重排此范围变化，只能查看状态或放弃当前 Delivery Run"
)


def reconcile_structure(
    previous: dict[str, Any], projected: dict[str, Any]
) -> dict[str, Any]:
    """Compare scope facts mechanically without asking an Agent to interpret drift."""
    observed_graph_revision = _revision(projected, "ticket_graph")
    observed_parent_revision = _revision(projected, "parent")
    accepted_parent_revision = previous.get("accepted_parent_spec_revision")
    accepted_graph_revision = previous.get("accepted_ticket_graph_revision")
    if (
        accepted_parent_revision is None
        and accepted_graph_revision is None
        and previous.get("currentness_resolution_pending") is True
    ):
        projected["accepted_ticket_graph_revision"] = observed_graph_revision
        projected["accepted_parent_spec_revision"] = observed_parent_revision
        projected["observed_parent_spec_revision"] = observed_parent_revision
        return projected
    if not isinstance(accepted_parent_revision, str) or not isinstance(
        accepted_graph_revision, str
    ):
        raise ValueError("legacy state lacks accepted currentness boundaries")

    reopened = _reopened_completed_tickets(previous, projected)
    if reopened:
        blocked = dict(previous)
        blocked.update(
            {
                "parent": _mapping(projected, "parent"),
                "status": "unsupported_scope_change",
                "terminal_kind": "unsupported_scope_change",
                "frontier": [],
                "active_ticket_job": None,
                "unsupported_scope_change": {
                    "accepted_graph_revision": accepted_graph_revision,
                    "observed_graph_revision": observed_graph_revision,
                    "graph_change_summary": {
                        "reopened_completed_tickets": reopened,
                    },
                    "observed_ticket_graph": deepcopy(
                        _mapping(projected, "ticket_graph")
                    ),
                    "observed_at": _now(),
                },
                "diagnostics": [
                    {
                        "code": "completed_ticket_reopened",
                        "message": _completed_ticket_reopened_message(reopened),
                        "ticket_numbers": reopened,
                    }
                ],
                "accepted_parent_spec_revision": accepted_parent_revision,
                "observed_parent_spec_revision": observed_parent_revision,
                "updated_at": _now(),
            }
        )
        return blocked

    if observed_graph_revision != accepted_graph_revision:
        existing = previous.get("unsupported_scope_change")
        observed_graph = deepcopy(_mapping(projected, "ticket_graph"))
        if (
            isinstance(existing, dict)
            and existing.get("observed_graph_revision") == observed_graph_revision
        ):
            change = dict(existing)
            change["observed_ticket_graph"] = observed_graph
        else:
            change = {
                "accepted_graph_revision": accepted_graph_revision,
                "observed_graph_revision": observed_graph_revision,
                "graph_change_summary": _graph_change_summary(
                    _mapping(previous, "ticket_graph"), observed_graph
                ),
                "observed_ticket_graph": observed_graph,
                "observed_at": _now(),
            }
        blocked = _unsupported(previous, change)
        blocked["accepted_parent_spec_revision"] = accepted_parent_revision
        blocked["observed_parent_spec_revision"] = observed_parent_revision
        return blocked

    projected["accepted_ticket_graph_revision"] = accepted_graph_revision
    projected.pop("unsupported_scope_change", None)

    projected["accepted_parent_spec_revision"] = accepted_parent_revision
    projected["observed_parent_spec_revision"] = observed_parent_revision
    return projected


def _unsupported(
    previous: dict[str, Any], change: dict[str, Any]
) -> dict[str, Any]:
    blocked = dict(previous)
    blocked.update(
        {
            "status": "unsupported_scope_change",
            "terminal_kind": "unsupported_scope_change",
            "frontier": [],
            "active_ticket_job": None,
            "unsupported_scope_change": change,
            "diagnostics": [
                {
                    "code": "unsupported_ticket_graph_change",
                    "message": _UNSUPPORTED_GRAPH_CHANGE_MESSAGE,
                }
            ],
            "updated_at": _now(),
        }
    )
    return blocked


def unsupported_scope_change_identity_is_consistent(
    state: dict[str, Any],
) -> bool:
    """Validate one producer-specific durable scope contradiction."""

    change = state.get("unsupported_scope_change")
    accepted_graph = state.get("ticket_graph")
    diagnostics = state.get("diagnostics")
    if (
        not isinstance(change, dict)
        or set(change)
        != {
            "accepted_graph_revision",
            "observed_graph_revision",
            "graph_change_summary",
            "observed_ticket_graph",
            "observed_at",
        }
        or not isinstance(accepted_graph, dict)
        or not isinstance(diagnostics, list)
        or len(diagnostics) != 1
    ):
        return False
    accepted = change.get("accepted_graph_revision")
    observed = change.get("observed_graph_revision")
    observed_graph = change.get("observed_ticket_graph")
    summary = change.get("graph_change_summary")
    diagnostic = diagnostics[0]
    if (
        not isinstance(accepted, str)
        or accepted != state.get("accepted_ticket_graph_revision")
        or accepted_graph.get("revision") != accepted
        or not isinstance(observed, str)
        or observed == accepted
        or not isinstance(observed_graph, dict)
        or observed_graph.get("revision") != observed
        or not isinstance(observed_graph.get("ordered_ticket_numbers"), list)
        or not all(
            isinstance(number, int)
            for number in observed_graph["ordered_ticket_numbers"]
        )
        or not isinstance(observed_graph.get("tickets"), dict)
        or not isinstance(summary, dict)
        or not isinstance(change.get("observed_at"), str)
        or not isinstance(diagnostic, dict)
    ):
        return False
    try:
        reopened = _reopened_completed_tickets(
            state, {"ticket_graph": observed_graph}
        )
        expected_delta = _graph_change_summary(accepted_graph, observed_graph)
    except (TypeError, ValueError):
        return False
    if reopened:
        return (
            summary == {"reopened_completed_tickets": reopened}
            and diagnostic.get("code") == "completed_ticket_reopened"
            and diagnostic.get("message")
            == _completed_ticket_reopened_message(reopened)
            and diagnostic.get("ticket_numbers") == reopened
        )
    return (
        summary == expected_delta
        and diagnostic.get("code") == "unsupported_ticket_graph_change"
        and diagnostic.get("message") == _UNSUPPORTED_GRAPH_CHANGE_MESSAGE
    )


def _completed_ticket_reopened_message(reopened: list[int]) -> str:
    return (
        f"Completed Ticket #{reopened[0]} was reopened outside "
        "Run Abandonment Recovery"
    )


def _graph_change_summary(
    accepted: dict[str, Any], observed: dict[str, Any]
) -> dict[str, Any]:
    old_tickets = set(_integer_list(accepted, "ordered_ticket_numbers"))
    new_tickets = set(_integer_list(observed, "ordered_ticket_numbers"))
    old_edges = _edges(accepted)
    new_edges = _edges(observed)
    added = sorted(new_tickets - old_tickets)
    removed = sorted(old_tickets - new_tickets)
    added_edges = sorted(new_edges - old_edges)
    removed_edges = sorted(old_edges - new_edges)
    return {
        "summary": (
            f"Ticket 图变化：新增 {len(added)}，移除 {len(removed)}，"
            f"新增依赖 {len(added_edges)}，移除依赖 {len(removed_edges)}。"
        ),
        "added_tickets": added,
        "removed_tickets": removed,
        "added_dependencies": [
            {"ticket_number": ticket, "blocked_by": blocker}
            for ticket, blocker in added_edges
        ],
        "removed_dependencies": [
            {"ticket_number": ticket, "blocked_by": blocker}
            for ticket, blocker in removed_edges
        ],
    }


def _reopened_completed_tickets(
    previous: dict[str, Any], projected: dict[str, Any]
) -> list[int]:
    jobs = previous.get("ticket_jobs")
    tickets = _mapping(_mapping(projected, "ticket_graph"), "tickets")
    if not isinstance(jobs, dict):
        return []
    reopened: list[int] = []
    for key, job in jobs.items():
        ticket = tickets.get(key)
        if (
            isinstance(job, dict)
            and job.get("phase") == "completed"
            and isinstance(ticket, dict)
            and str(ticket.get("state", "")).upper() != "CLOSED"
        ):
            reopened.append(int(key))
    return sorted(reopened)


def _edges(graph: dict[str, Any]) -> set[tuple[int, int]]:
    tickets = _mapping(graph, "tickets")
    edges: set[tuple[int, int]] = set()
    for key, value in tickets.items():
        if not isinstance(value, dict):
            continue
        blockers = value.get("blocked_by")
        if not isinstance(blockers, list):
            continue
        for blocker in blockers:
            if isinstance(blocker, dict) and isinstance(
                blocker.get("number"), int
            ):
                edges.add((int(key), int(blocker["number"])))
    return edges


def _revision(state: dict[str, Any], key: str) -> str:
    revision = _mapping(state, key).get("revision")
    if not isinstance(revision, str):
        raise ValueError(f"{key} revision is invalid")
    return revision


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _integer_list(data: dict[str, Any], key: str) -> list[int]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int) for item in value
    ):
        raise ValueError(f"{key} must contain integers")
    return list(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()
