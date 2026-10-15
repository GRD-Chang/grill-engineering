"""Mechanical currentness checks for a persisted Change Job Generation."""

from __future__ import annotations

from typing import Any, Protocol

from agent_run.git import GitError, GitRepository
from agent_run.revisions import effective_revision
from agent_run.run_currentness import ticket_completion_records
from agent_run.run_repair_integration import repair_run_branch_is_current
from agent_run.parent_integration import parent_integration_record_is_valid


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
        # Ticket Development and original Review keep their fixed base.
        # A newer Run Branch is integrated at the delivery boundary.
        return None
    if subject.startswith("parent-only:"):
        if job.get("effective_revision") != parent.get("revision"):
            return "parent_requirements_changed"
        base = _mapping(state, "base")
        fixed_base = base.get("sha")
        job_base = job.get("base_sha")
        if not isinstance(fixed_base, str) or not isinstance(job_base, str):
            return "parent_base_changed"
        try:
            if not git.is_ancestor(fixed_base, job_base):
                return "parent_base_changed"
        except (GitError, ValueError):
            return "parent_base_changed"
        if job_base != fixed_base and not parent_integration_record_is_valid(git, job):
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
        if not repair_run_branch_is_current(git, job, git.resolve(run_branch)):
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
    known_heads = {job.get("publication_sha")}
    if job.get("phase") in {
        "developing", "repairing", "committing_candidate", "candidate",
        "reviewing", "accepted", "publication_pending", "publishing",
    } and isinstance(job.get("published_sha"), str):
        known_heads.add(job["published_sha"])
    if not subject.startswith("ticket:") and live.get("head_sha") not in known_heads:
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
    if subject.startswith("parent-only:"):
        expected_base = job.get("base_sha")
        observed_base = live.get("base_sha")
        try:
            target_is_append_only = (
                isinstance(expected_base, str)
                and isinstance(observed_base, str)
                and git.is_ancestor(expected_base, observed_base)
            )
        except (GitError, ValueError):
            target_is_append_only = False
        if not target_is_append_only:
            return "change_pr_base_changed_externally"
    elif not subject.startswith("ticket:"):
        local_base = git.resolve(expected_base_branch)
        expected_base = local_base
        if live.get("base_sha") != expected_base:
            return "change_pr_base_changed_externally"
    return None


def candidate_or_acceptance_is_inconsistent(
    job: dict[str, Any], *, budget_owner: dict[str, Any] | None = None,
) -> bool:
    """Whether a persisted acceptance cannot be bound to its candidate."""
    candidate = job.get("candidate_sha")
    record = job.get("acceptance_record")
    if record is None:
        return False
    if not isinstance(record, dict) or not isinstance(candidate, str):
        return True
    reviewed_candidate = record.get("reviewed_candidate_sha")
    if reviewed_candidate == candidate:
        return False
    # During a repair cycle the latest Acceptance remains bounded history for
    # the next Reviewer and fallback receipt; it is not authority for the new
    # Candidate.  Accept that shape only when both persisted history indexes
    # prove the exact prior Candidate and Artifact.
    if (
        job.get("phase")
        not in {
            "developing",
            "repairing",
            "committing_candidate",
            "candidate",
            "reviewing",
            "ready_for_integration",
        }
        or (
            job.get("phase") == "ready_for_integration"
            and job.get("attempt_kind") != "ci_repair"
        )
        or reviewed_candidate != job.get("last_review_candidate_sha")
    ):
        return True
    owner = budget_owner if budget_owner is not None else job
    budget = owner.get("ci_recovery" if job.get("attempt_kind") == "ci_repair" else "review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    latest = artifacts[-1] if isinstance(artifacts, list) and artifacts else None
    return not (
        isinstance(latest, dict)
        and latest.get("candidate_sha") == reviewed_candidate
        and latest.get("artifact") == record.get("artifact")
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value
