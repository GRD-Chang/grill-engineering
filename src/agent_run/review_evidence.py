"""审核证据适用规则；对象各自提供准确事实，不搬移历史通过标记。"""

from __future__ import annotations

from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.git import GitRepository


def review_record_matches(record: object, identity: dict[str, Any]) -> bool:
    """Compare the complete identity for this review scope, including its baseline."""
    return isinstance(record, dict) and bool(identity) and all(
        value is not None and record.get(key) == value
        for key, value in identity.items()
    )


def accepted_review_matches(
    record: object, identity: dict[str, Any], artifact: object,
) -> bool:
    if not review_record_matches(record, identity) or not isinstance(record, dict):
        return False
    try:
        accepted = AcceptanceArtifact.parse(record.get("artifact")).is_accepted
    except (TypeError, ValueError):
        return False
    thread = record.get("reviewer_thread_id")
    return (accepted and isinstance(thread, str) and bool(thread.strip())
            and record.get("artifact") == artifact)


def run_review_identity(
    state: dict[str, Any], git: GitRepository, default: str, head: str,
) -> dict[str, Any]:
    """Full Parent responsibility, including the actual expected merge result."""
    from agent_run.run_currentness import ticket_completion_records

    return {
        "acceptance_scope": "run",
        "reviewed_default_base_sha": default,
        "reviewed_head_sha": head,
        "expected_merge_tree": git.expected_merge_tree(
            default_head_sha=default, run_head_sha=head,
        ),
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
    }


def candidate_run_review_identity(
    state: dict[str, Any], job: dict[str, Any], git: GitRepository,
) -> dict[str, Any]:
    """Candidate Review before promotion retains its original Run baseline."""
    candidate = str(job["candidate_sha"])
    default = str(job["default_base_sha"])
    identity = run_review_identity(state, git, default, candidate)
    identity.pop("reviewed_head_sha")
    return {
        **identity,
        "acceptance_state": "candidate",
        "reviewed_base_sha": job["base_sha"],
        "repair_base_run_head_sha": job["base_sha"],
        "reviewed_candidate_sha": candidate,
        "reviewed_candidate_tree": git.resolve(f"{candidate}^{{tree}}"),
    }


def same_review_requirements(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """A prior complete scope is usable for focus only under the same requirements."""
    return all(previous.get(key) == current.get(key) for key in (
        "acceptance_scope", "effective_revision", "parent", "ticket",
        "ticket_graph", "ticket_completion_records",
    ))
