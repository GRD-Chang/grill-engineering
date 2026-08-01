from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class ScopeImpactAssessor(Protocol):
    def assess_scope(self, request: dict[str, Any]) -> dict[str, Any]: ...


def reconcile_structure(
    previous: dict[str, Any],
    projected: dict[str, Any],
    *,
    assessor: ScopeImpactAssessor | None,
    checkout: Path,
) -> dict[str, Any]:
    graph_result = _reconcile_ticket_graph(previous, projected)
    if graph_result is not projected:
        return graph_result
    return _reconcile_parent_spec(
        previous,
        projected,
        assessor=assessor,
        checkout=checkout,
    )


def _reconcile_ticket_graph(
    previous: dict[str, Any], projected: dict[str, Any]
) -> dict[str, Any]:
    observed_revision = _graph_revision(projected)
    accepted_revision = previous.get("accepted_ticket_graph_revision")
    if not isinstance(accepted_revision, str):
        previous_revision = _mapping(previous, "ticket_graph").get("revision")
        if isinstance(previous_revision, str):
            accepted_revision = previous_revision
        else:
            projected["accepted_ticket_graph_revision"] = observed_revision
            return projected
    if observed_revision == accepted_revision:
        projected["accepted_ticket_graph_revision"] = accepted_revision
        return projected

    existing = previous.get("pending_structure_change")
    if (
        isinstance(existing, dict)
        and existing.get("kind") in {None, "ticket_graph"}
        and existing.get("proposed_revision") == observed_revision
    ):
        pending = dict(existing)
        pending.setdefault("kind", "ticket_graph")
        legacy_summary = pending.pop("scope_impact_assessment", None)
        if (
            "graph_change_summary" not in pending
            and isinstance(legacy_summary, dict)
        ):
            pending["graph_change_summary"] = legacy_summary
        pending["proposed_ticket_graph"] = deepcopy(
            _mapping(projected, "ticket_graph")
        )
    else:
        pending = {
            "kind": "ticket_graph",
            "accepted_revision": accepted_revision,
            "proposed_revision": observed_revision,
            "graph_change_summary": _graph_change_summary(
                _mapping(previous, "ticket_graph"),
                _mapping(projected, "ticket_graph"),
            ),
            "proposed_ticket_graph": deepcopy(
                _mapping(projected, "ticket_graph")
            ),
            "observed_at": _now(),
        }
    return _paused(
        previous,
        pending,
        code="ticket_graph_change_requires_confirmation",
        message=(
            "Ticket 集合或依赖关系已变化；确认准确的新图版本后才能继续推进"
        ),
    )


def _reconcile_parent_spec(
    previous: dict[str, Any],
    projected: dict[str, Any],
    *,
    assessor: ScopeImpactAssessor | None,
    checkout: Path,
) -> dict[str, Any]:
    observed_revision = _parent_revision(projected)
    accepted_revision = previous.get("accepted_parent_spec_revision")
    if not isinstance(accepted_revision, str):
        previous_revision = _mapping(previous, "parent").get("revision")
        if isinstance(previous_revision, str):
            accepted_revision = previous_revision
        else:
            projected["accepted_parent_spec_revision"] = observed_revision
            projected.pop("pending_structure_change", None)
            return projected
    if observed_revision == accepted_revision:
        projected["accepted_parent_spec_revision"] = accepted_revision
        projected.pop("pending_structure_change", None)
        return projected

    existing = previous.get("pending_structure_change")
    if (
        isinstance(existing, dict)
        and existing.get("kind") == "parent_spec"
        and existing.get("proposed_revision") == observed_revision
    ):
        pending = dict(existing)
        pending["proposed_parent"] = deepcopy(
            _mapping(projected, "parent")
        )
        return _paused(
            previous,
            pending,
            code="parent_spec_change_requires_confirmation",
            message=(
                "Parent Spec 的结构性变化会影响交付范围；确认准确的新版本后"
                "才能继续推进"
            ),
        )
    if assessor is None:
        raise ValueError(
            "Parent Spec revision changed but no Scope Impact Assessor is configured"
        )
    assessment = _validated_assessment(
        assessor.assess_scope(
            {
                "checkout": str(checkout),
                "accepted_parent_revision": accepted_revision,
                "proposed_parent_revision": observed_revision,
                "accepted_parent": _parent_snapshot(previous),
                "proposed_parent": _parent_snapshot(projected),
                "ticket_graph": _mapping(projected, "ticket_graph"),
                "completed_work": _completed_work(previous),
            }
        )
    )
    if not assessment["structural_change"]:
        projected["accepted_parent_spec_revision"] = observed_revision
        projected["latest_scope_impact_assessment"] = {
            "accepted_revision": accepted_revision,
            "proposed_revision": observed_revision,
            "assessment": assessment,
            "observed_at": _now(),
        }
        projected.pop("pending_structure_change", None)
        return projected

    pending = {
        "kind": "parent_spec",
        "accepted_revision": accepted_revision,
        "proposed_revision": observed_revision,
        "scope_impact_assessment": assessment,
        "proposed_parent": deepcopy(_mapping(projected, "parent")),
        "observed_at": _now(),
    }
    return _paused(
        previous,
        pending,
        code="parent_spec_change_requires_confirmation",
        message=(
            "Parent Spec 的结构性变化会影响交付范围；确认准确的新版本后"
            "才能继续推进"
        ),
    )


def _paused(
    previous: dict[str, Any],
    pending: dict[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    paused = dict(previous)
    paused.update(
        {
            "status": "structure_change_pending",
            "terminal_kind": "structure_change_pending",
            "frontier": [],
            "active_ticket_job": None,
            "pending_structure_change": pending,
            "diagnostics": [{"code": code, "message": message}],
            "updated_at": _now(),
        }
    )
    return paused


def _graph_change_summary(
    accepted: dict[str, Any], proposed: dict[str, Any]
) -> dict[str, Any]:
    old_tickets = set(_integer_list(accepted, "ordered_ticket_numbers"))
    new_tickets = set(_integer_list(proposed, "ordered_ticket_numbers"))
    old_edges = _edges(accepted)
    new_edges = _edges(proposed)
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


def _completed_work(state: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = state.get("ticket_jobs")
    if not isinstance(jobs, dict):
        return []
    completed: list[dict[str, Any]] = []
    for key, value in jobs.items():
        if not isinstance(value, dict) or value.get("phase") != "completed":
            continue
        try:
            number = int(key)
        except ValueError:
            continue
        record: dict[str, Any] = {"ticket_number": number}
        for field in ("effective_revision", "integrated_sha", "pr_number"):
            if field in value:
                record[field] = value[field]
        completed.append(record)
    return sorted(completed, key=lambda item: int(item["ticket_number"]))


def _validated_assessment(value: dict[str, Any]) -> dict[str, Any]:
    expected_text = (
        "summary",
        "ticket_set_impact",
        "dependency_impact",
        "delivery_boundary_impact",
        "completed_work_impact",
    )
    if not isinstance(value.get("structural_change"), bool):
        raise ValueError(
            "Scope Impact Assessment structural_change must be boolean"
        )
    for field in expected_text:
        content = value.get(field)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"Scope Impact Assessment {field} must be non-empty text"
            )
    return {
        "structural_change": value["structural_change"],
        **{field: value[field].strip() for field in expected_text},
    }


def _parent_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    parent = _mapping(state, "parent")
    return {
        key: parent.get(key)
        for key in ("number", "title", "body", "revision")
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


def _graph_revision(state: dict[str, Any]) -> str:
    revision = _mapping(state, "ticket_graph").get("revision")
    if not isinstance(revision, str):
        raise ValueError("projected Ticket Graph revision is invalid")
    return revision


def _parent_revision(state: dict[str, Any]) -> str:
    revision = _mapping(state, "parent").get("revision")
    if not isinstance(revision, str):
        raise ValueError("projected Parent Spec revision is invalid")
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
