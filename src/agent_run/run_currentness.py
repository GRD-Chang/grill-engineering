from __future__ import annotations

from typing import Any

from agent_run.agent_invocation import canonical_fingerprint
from agent_run.revisions import effective_revision


def ticket_completion_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Project completed Ticket state into the Run currentness contract."""

    parent = _mapping(state.get("parent"), "parent")
    graph = _mapping(state.get("ticket_graph"), "ticket_graph")
    tickets = _mapping(graph.get("tickets"), "ticket_graph.tickets")
    jobs = _mapping(state.get("ticket_jobs"), "ticket_jobs")
    parent_revision = str(parent["revision"])
    graph_revision = str(graph["revision"])
    records: list[dict[str, Any]] = []
    for key, job in sorted(jobs.items()):
        if not isinstance(job, dict) or job.get("phase") != "completed":
            continue
        ticket = _mapping(tickets.get(key), f"ticket {key}")
        records.append(
            {
                "ticket_number": int(key),
                "integrated_sha": job.get("integrated_sha"),
                "effective_revision": effective_revision(
                    ticket_revision=str(ticket["content_revision"]),
                    parent_revision=parent_revision,
                    graph_revision=graph_revision,
                ),
                "acceptance_record": job.get("acceptance_record"),
            }
        )
    return records


def ticket_completion_records_fingerprint(state: dict[str, Any]) -> str:
    return canonical_fingerprint(ticket_completion_records(state))


def run_currentness_boundary(
    state: dict[str, Any],
    *,
    reviewed_head_sha: str,
    reviewed_default_base_sha: str,
    expected_merge_tree: str,
) -> dict[str, Any]:
    """Build the durable facts that make a Run invocation resumable."""

    return {
        "reviewed_head_sha": reviewed_head_sha,
        "reviewed_default_base_sha": reviewed_default_base_sha,
        "expected_merge_tree": expected_merge_tree,
        "parent_revision": _mapping(state.get("parent"), "parent")["revision"],
        "ticket_graph_revision": _mapping(
            state.get("ticket_graph"), "ticket_graph"
        )["revision"],
        "ticket_completion_records_fingerprint": ticket_completion_records_fingerprint(
            state
        ),
    }


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
