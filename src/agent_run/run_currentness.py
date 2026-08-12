from __future__ import annotations

from typing import Any, Protocol

from agent_run.agent_invocation import canonical_fingerprint
from agent_run.git import GitRepository
from agent_run.graph import state_from_graph
from agent_run.models import Repository
from agent_run.scope_changes import reconcile_structure


class RunCurrentnessReader(Protocol):
    """Read the authoritative Run boundary at an apply seam."""

    def repository(self) -> Repository: ...

    def delivery_graph(self, parent_number: int) -> Any: ...


def refresh_run_currentness(
    state: dict[str, Any], *, reader: RunCurrentnessReader, git: GitRepository
) -> str | None:
    """Project live authority and return its resolved default base when usable."""
    repository = reader.repository()
    default_head = git.resolve_base(
        repository.default_branch, repository.default_head_sha
    )
    parent = _mapping(state.get("parent"), "parent")
    projected = state_from_graph(
        state, reader.delivery_graph(int(parent["number"]))
    )
    state.update(reconcile_structure(state, projected))
    return None if state.get("status") == "unsupported_scope_change" else default_head


def ticket_completion_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Project completed Ticket state into the Run currentness contract."""

    graph = _mapping(state.get("ticket_graph"), "ticket_graph")
    tickets = _mapping(graph.get("tickets"), "ticket_graph.tickets")
    jobs = _mapping(state.get("ticket_jobs"), "ticket_jobs")
    records: list[dict[str, Any]] = []
    for key, job in sorted(jobs.items(), key=lambda item: int(item[0])):
        if not isinstance(job, dict) or job.get("phase") != "completed":
            continue
        _mapping(tickets.get(key), f"ticket {key}")
        acceptance = _mapping(
            job.get("acceptance_record"), f"completed ticket {key} acceptance_record"
        )
        records.append(
            {
                "ticket_number": int(key),
                "integrated_sha": job.get("integrated_sha"),
                "effective_revision": job.get("effective_revision"),
                "reviewed_base_sha": acceptance.get("reviewed_base_sha"),
                "reviewed_candidate_tree": acceptance.get("reviewed_candidate_tree"),
            }
        )
    return records


def ticket_completion_records_fingerprint(state: dict[str, Any]) -> str:
    return canonical_fingerprint(ticket_completion_records(state))


def invalidate_run_acceptance(state: dict[str, Any]) -> dict[str, Any]:
    """Discard one stale Run Acceptance and its generation-local context."""
    run = _mapping(state.get("run_acceptance"), "run_acceptance")
    for key in (
        "acceptance_record",
        "acceptance_artifact",
        "reviewed_head_sha",
        "reviewer_resume_thread_id",
        "reviewer_new_thread",
        "human_response_history",
        "human_response_generation",
        "prior_human_blockers",
    ):
        run.pop(key, None)
    run["acceptance_generation"] = int(run.get("acceptance_generation", 1)) + 1
    run["phase"] = "pending"
    publication = state.get("run_publication")
    if isinstance(publication, dict) and publication.get("phase") not in {
        "merged",
        "abandoned",
    }:
        publication["phase"] = "stale"
    return run


def invalidate_stale_run_repair(state: dict[str, Any]) -> dict[str, Any]:
    """Discard a stale Repair so a fresh Run Acceptance chooses the next step."""
    run = _mapping(state.get("run_acceptance"), "run_acceptance")
    repair_job = run.get("repair_job")
    if isinstance(repair_job, dict):
        discarded = _string_list(run, "discarded_repair_thread_ids")
        for key in ("development_thread_id",):
            value = repair_job.get(key)
            if isinstance(value, str) and value not in discarded:
                discarded.append(value)
        for key in ("development_thread_history", "reviewer_thread_ids"):
            values = repair_job.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value not in discarded:
                        discarded.append(value)
        if discarded:
            run["discarded_repair_thread_ids"] = discarded
    run.pop("repair_job", None)
    run.pop("repair_request", None)
    state.pop("active_agent_invocation", None)
    state.pop("requeue_required", None)
    invalidate_run_acceptance(state)
    state.update(
        {
            "status": "run_acceptance_pending",
            "terminal_kind": "run_acceptance_stale",
            "diagnostics": [],
        }
    )
    return run


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


def _string_list(mapping: dict[str, Any], key: str) -> list[str]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must contain strings")
    return list(value)
