"""Mechanical currentness checks for a persisted Change Job Generation."""

from __future__ import annotations

from typing import Any, Protocol

from agent_run.git import GitRepository
from agent_run.revisions import effective_revision
from agent_run.run_currentness import ticket_completion_records


class PullRequestReader(Protocol):
    """Read the live facts needed to validate a persisted Change PR."""

    def live_pull_request(self, pr_number: int) -> dict[str, Any]: ...


def stale_change_job_reason(
    state: dict[str, Any], subject: str, job: dict[str, Any], git: GitRepository
) -> str | None:
    """Return a mechanically provable stale reason, if a fresh start exists."""
    parent = _mapping(state, "parent")
    graph = _mapping(state, "ticket_graph")
    if subject.startswith("ticket:"):
        ticket = _mapping(_mapping(graph, "tickets"), subject.removeprefix("ticket:"))
        expected = effective_revision(
            ticket_revision=str(ticket["content_revision"]),
            parent_revision=str(parent["revision"]),
            graph_revision=str(graph["revision"]),
        )
        if job.get("effective_revision") != expected:
            return "ticket_requirements_changed"
        run_branch = state.get("run_branch")
        if isinstance(run_branch, str) and job.get("base_sha") != git.resolve(run_branch):
            return "ticket_base_changed"
        return None
    if subject.startswith("parent-only:"):
        if job.get("effective_revision") != parent.get("revision"):
            return "parent_requirements_changed"
        base = _mapping(state, "base")
        branch = base.get("branch")
        if not isinstance(branch, str) or job.get("base_sha") != git.resolve(branch):
            return "parent_base_changed"
        return None
    if job.get("parent_revision") != parent.get("revision"):
        return "run_repair_parent_changed"
    if job.get("ticket_graph_revision") != graph.get("revision"):
        return "run_repair_graph_changed"
    if job.get("ticket_completion_records") != ticket_completion_records(state):
        return "run_repair_ticket_completion_changed"
    run_branch = state.get("run_branch")
    if isinstance(run_branch, str):
        if job.get("base_sha") != git.resolve(run_branch):
            return "run_repair_base_changed"
    return None


def has_currentness_facts(subject: str, job: dict[str, Any]) -> bool:
    """Whether a Generation has enough persisted facts for stale routing."""
    if not isinstance(job.get("base_sha"), str):
        return False
    if subject.startswith(("ticket:", "parent-only:")):
        return isinstance(job.get("effective_revision"), str)
    return (
        isinstance(job.get("parent_revision"), str)
        and isinstance(job.get("ticket_graph_revision"), str)
        and isinstance(job.get("ticket_completion_records"), list)
    )


def unknown_pr_mutation(
    state: dict[str, Any],
    subject: str,
    job: dict[str, Any],
    github: PullRequestReader,
    git: GitRepository,
) -> str | None:
    """Return a Human-Blocker reason for an untrusted live Change PR state."""
    pr_number = job.get("pr_number")
    if not isinstance(pr_number, int):
        return None
    live = github.live_pull_request(pr_number)
    if live.get("state") != "OPEN":
        return "change_pr_closed_or_merged_externally"
    if live.get("head_sha") != job.get("publication_sha"):
        return "change_pr_head_changed_externally"
    expected_base_branch = (
        state.get("run_branch")
        if subject.startswith(("ticket:", "run-repair:"))
        else _mapping(state, "base").get("branch")
    )
    if not isinstance(expected_base_branch, str):
        return "change_pr_base_unknown"
    if live.get("base_branch") != expected_base_branch:
        return "change_pr_base_changed_externally"
    if live.get("base_sha") != git.resolve(expected_base_branch):
        return "change_pr_base_changed_externally"
    return None


def candidate_or_acceptance_is_inconsistent(job: dict[str, Any]) -> bool:
    """Whether a persisted acceptance cannot be bound to its candidate."""
    candidate = job.get("candidate_sha")
    record = job.get("acceptance_record")
    if record is None:
        return False
    if not isinstance(record, dict) or not isinstance(candidate, str):
        return True
    return record.get("reviewed_candidate_sha") != candidate


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value
