from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


def reconcile_structure(
    previous: dict[str, Any], projected: dict[str, Any]
) -> dict[str, Any]:
    """Compare scope facts mechanically without asking an Agent to interpret drift."""
    observed_graph_revision = _revision(projected, "ticket_graph")
    accepted_graph_revision = previous.get("accepted_ticket_graph_revision")
    if not isinstance(accepted_graph_revision, str):
        previous_graph_revision = _mapping(previous, "ticket_graph").get(
            "revision"
        )
        if isinstance(previous_graph_revision, str):
            accepted_graph_revision = previous_graph_revision
        else:
            projected["accepted_ticket_graph_revision"] = (
                observed_graph_revision
            )
            projected["accepted_parent_spec_revision"] = _revision(
                projected, "parent"
            )
            projected["observed_parent_spec_revision"] = _revision(
                projected, "parent"
            )
            return projected

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
        return _unsupported(previous, change)

    projected["accepted_ticket_graph_revision"] = accepted_graph_revision
    projected.pop("unsupported_scope_change", None)

    observed_parent_revision = _revision(projected, "parent")
    accepted_parent_revision = previous.get("accepted_parent_spec_revision")
    if not isinstance(accepted_parent_revision, str):
        accepted_parent_revision = _revision(previous, "parent")
    projected["accepted_parent_spec_revision"] = accepted_parent_revision
    projected["observed_parent_spec_revision"] = observed_parent_revision
    return projected


def _unsupported(
    previous: dict[str, Any], change: dict[str, Any]
) -> dict[str, Any]:
    blocked = dict(previous)
    timeline = list(previous.get("timeline", []))
    already_recorded = any(
        isinstance(event, dict)
        and event.get("kind") == "unsupported_scope_change"
        and event.get("observed_graph_revision")
        == change["observed_graph_revision"]
        for event in timeline
    )
    if not already_recorded:
        timeline.append(
            {
                "at": _now(),
                "kind": "unsupported_scope_change",
                "status": "unsupported_scope_change",
                "accepted_graph_revision": change[
                    "accepted_graph_revision"
                ],
                "observed_graph_revision": change[
                    "observed_graph_revision"
                ],
                "graph_change_summary": deepcopy(
                    change["graph_change_summary"]
                ),
                "next_action": "restore_graph_or_abandon",
                "result": change["graph_change_summary"]["summary"],
            }
        )
    blocked.update(
        {
            "status": "unsupported_scope_change",
            "terminal_kind": "unsupported_scope_change",
            "frontier": [],
            "active_ticket_job": None,
            "unsupported_scope_change": change,
            "timeline": timeline,
            "diagnostics": [
                {
                    "code": "unsupported_ticket_graph_change",
                    "message": (
                        "Ticket 集合或 blockedBy 依赖已变化；当前 MVP 不支持吸收或"
                        "自动重排此范围变化，只能查看状态或放弃当前 Delivery Run"
                    ),
                }
            ],
            "updated_at": _now(),
        }
    )
    return blocked


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
