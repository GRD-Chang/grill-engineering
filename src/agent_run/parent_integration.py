from __future__ import annotations

"""Exact remote snapshot integration shared by the two Parent delivery modes."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import (
    complete_integration_window,
    observe_integration_snapshot,
)


PARENT_INTEGRATION_COMMIT_MESSAGE = "chore: resolve Parent integration conflict"


def parent_conflict_commit_boundary(conflict: dict[str, Any]) -> tuple[str, str | None]:
    """Use the saved closeout checkpoint for a follow-up on a resolved stage."""
    if conflict["stage"] in conflict["resolved_stages"]:
        return str(conflict["resolution_sha"]), None
    return str(conflict["head_sha"]), conflict["merge_head_sha"]


class ParentHistoryRewritten(GitError):
    def __init__(self, branch_kind: str, previous: str, observed: str) -> None:
        super().__init__(f"{branch_kind} history changed from {previous} to {observed}")
        self.branch_kind = branch_kind
        self.previous = previous
        self.observed = observed


@dataclass(frozen=True)
class ParentSnapshotResult:
    status: str
    record: dict[str, Any] | None = None
    conflict: dict[str, Any] | None = None


def prepare_parent_snapshot(
    *,
    git: GitRepository,
    github: Any,
    subject: dict[str, Any],
    checkout: Path,
    source_branch: str,
    target_branch: str,
    candidate_sha: str,
    policy_snapshot: object,
    record_key: str,
    save: Callable[[], object],
    own_source_write: bool | str = False,
    confirmed_source_sha: str | None = None,
    handoff_git_failures: bool = False,
    budget_owner: dict[str, Any] | None = None,
) -> ParentSnapshotResult:
    """Integrate one exact live snapshot and record its provenance.

    The caller owns conflict repair and any Reviewer decision. This function
    owns only Git integration and the external snapshot allowance.
    """

    owner = budget_owner if budget_owner is not None else subject
    conflict_key = f"{record_key}_conflict"
    source, target = github.live_branch_heads(source_branch, target_branch)
    if source is None:
        raise GitError(f"remote source ref {source_branch!r} is missing")
    # A caller may know that its last publication was pushed, but that does
    # not make every later source head an own write.
    own_source_write = (
        source == own_source_write
        if isinstance(own_source_write, str)
        else own_source_write and source in {
            subject.get("published_sha"), subject.get("publication_sha")
        }
    )
    try:
        git.fetch_remote_commit(source_branch, source)
        git.fetch_remote_commit(target_branch, target)
    except GitError as error:
        error.evidence.update(source_sha=source, target_sha=target, candidate_sha=candidate_sha)
        raise
    prior = subject.get(record_key)
    previous_source = (
        str(prior["source_sha"])
        if isinstance(prior, dict)
        else str(subject.get("published_sha") or subject["base_sha"])
    )
    previous_target = (
        str(prior["target_sha"])
        if isinstance(prior, dict)
        else str(subject["base_sha"])
    )
    # The integration record remembers the head observed *before* publication.
    # Once its result has been published, that newer head is also an authority:
    # resetting the remote back to the old source must not be accepted as a
    # fresh snapshot and pushed again.
    published_source = confirmed_source_sha or subject.get("published_sha")
    if isinstance(published_source, str) and not git.is_ancestor(published_source, source):
        raise ParentHistoryRewritten("source", published_source, source)
    if not git.is_ancestor(previous_source, source):
        raise ParentHistoryRewritten("source", previous_source, source)
    if not git.is_ancestor(previous_target, target):
        raise ParentHistoryRewritten("target", previous_target, target)
    pending = subject.get(conflict_key)
    if isinstance(pending, dict):
        # Finish the recorded merge scene first. A later live snapshot is
        # observed and charged after that result is safely recorded.
        return ParentSnapshotResult(status="conflict", conflict=pending)
    if (
        isinstance(prior, dict)
        and candidate_sha in {prior.get("candidate_sha"), prior.get("head_sha")}
        and (prior.get("source_sha") == source or own_source_write)
        and prior.get("target_sha") == target
    ):
        return ParentSnapshotResult(status="clean", record=prior)
    observe_integration_snapshot(
        owner,
        source_sha=source,
        target_sha=target,
        policy_snapshot=policy_snapshot,
        own_source_write=own_source_write,
    )
    save()
    start = candidate_sha
    if isinstance(prior, dict) and git.is_ancestor(candidate_sha, str(prior["head_sha"])):
        start = str(prior["head_sha"])
    if git.checkout_head(checkout) != start:
        git.seed_managed_checkout(checkout, expected_head=start)
    try:
        merged = git.integrate_remote_heads(
            checkout,
            candidate_sha=start,
            source_sha=source,
            target_sha=target,
        )
    except GitError as error:
        error.evidence.update(source_sha=source, target_sha=target, candidate_sha=candidate_sha)
        if not handoff_git_failures:
            raise
        head = error.evidence.get("head_sha")
        if not isinstance(head, str):
            # Without a reliable editable scene, the caller must pause.
            raise
        conflict = {
            "source_sha": source, "target_sha": target,
            "candidate_sha": candidate_sha, "stage": "git_check",
            "head_sha": head, "merge_head_sha": error.evidence.get("merge_head_sha"),
            "paths": [], "conflict_paths": [], "resolved_stages": [],
            "failure": error.evidence | {"error": str(error)},
        }
        subject[conflict_key] = conflict
        save()
        return ParentSnapshotResult(status="conflict", conflict=conflict)
    if merged.status == "conflict":
        conflict = {
            "source_sha": source,
            "target_sha": target,
            "candidate_sha": candidate_sha,
            "stage": merged.conflict_stage,
            "head_sha": merged.head_sha,
            "merge_head_sha": merged.merge_head_sha,
            "paths": list(merged.conflict_paths),
            "conflict_paths": list(merged.conflict_paths),
            "resolved_stages": [],
            "failure": merged.failure_evidence,
        }
        subject[conflict_key] = conflict
        save()
        return ParentSnapshotResult(status="conflict", conflict=conflict)
    record = {
        "candidate_sha": candidate_sha,
        "candidate_tree": git.resolve(f"{candidate_sha}^{{tree}}"),
        "source_sha": source,
        "target_sha": target,
        "head_sha": merged.head_sha,
        "head_tree": git.resolve(f"{merged.head_sha}^{{tree}}"),
        "conflict_paths": [],
    }
    if isinstance(prior, dict) and prior != record:
        subject.setdefault(f"{record_key}_history", []).append(prior)
    subject[record_key] = record
    complete_integration_window(budget_owner if budget_owner is not None else subject)
    save()
    return ParentSnapshotResult(status="clean", record=record)


def parent_integration_record_is_valid(git: GitRepository, job: dict[str, Any]) -> bool:
    record = job.get("parent_integration")
    if not isinstance(record, dict) or set(record) != {
        "candidate_sha", "candidate_tree", "source_sha", "target_sha",
        "head_sha", "head_tree", "conflict_paths",
    }:
        return False
    try:
        return (
            job.get("candidate_sha") == record["head_sha"]
            and job.get("base_sha") == record["target_sha"]
            and record["candidate_tree"] == git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and record["head_tree"] == git.resolve(f"{record['head_sha']}^{{tree}}")
            and git.is_ancestor(record["candidate_sha"], record["head_sha"])
            and git.is_ancestor(record["source_sha"], record["head_sha"])
            and git.is_ancestor(record["target_sha"], record["head_sha"])
            and isinstance(record["conflict_paths"], list)
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False


def finish_parent_snapshot_conflict(
    *,
    git: GitRepository,
    subject: dict[str, Any],
    checkout: Path,
    record_key: str,
    save: Callable[[], object],
    budget_owner: dict[str, Any] | None = None,
) -> ParentSnapshotResult:
    """Commit an Agent-resolved conflict and finish the exact snapshot."""

    conflict_key = f"{record_key}_conflict"
    conflict = subject.get(conflict_key)
    if not isinstance(conflict, dict):
        raise ValueError("Parent integration has no pending conflict")
    resolved_stages = list(conflict["resolved_stages"])
    stage = str(conflict["stage"])
    if stage in resolved_stages:
        head = str(conflict["resolution_sha"])
        if not git.is_ancestor(head, git.checkout_head(checkout)):
            raise GitError("Parent conflict resolution checkout changed")
        if conflict.get("failure") and conflict.get("development_summary"):
            head = git.commit_integration_work(
                checkout, expected_head_sha=head, expected_merge_sha=None,
                message=PARENT_INTEGRATION_COMMIT_MESSAGE,
            )
            conflict["resolution_sha"] = head
            save()
    else:
        if len([item for item in resolved_stages if item != "git_check"]) >= 2:
            raise GitError("Parent integration still conflicts after bounded resolution")
        head = git.checkout_head(checkout)
        if stage == "git_check":
            head = git.commit_integration_work(
                checkout, expected_head_sha=str(conflict["head_sha"]),
                expected_merge_sha=conflict["merge_head_sha"],
                message=PARENT_INTEGRATION_COMMIT_MESSAGE,
            )
        elif git.commit_parents(head) != [conflict["head_sha"], conflict["merge_head_sha"]]:
            head = git.commit_integration_resolution(
                checkout,
                expected_head_sha=str(conflict["head_sha"]),
                expected_merge_sha=str(conflict["merge_head_sha"]),
                message=PARENT_INTEGRATION_COMMIT_MESSAGE,
            )
        resolved_stages.append(stage)
        conflict["resolution_sha"] = head
        conflict["resolved_stages"] = resolved_stages
        save()
    merged = git.integrate_remote_heads(
        checkout,
        candidate_sha=head,
        source_sha=str(conflict["source_sha"]),
        target_sha=str(conflict["target_sha"]),
    )
    if merged.status == "conflict":
        if merged.conflict_stage in resolved_stages or len([item for item in resolved_stages if item != "git_check"]) >= 2:
            raise GitError("Parent integration still conflicts after bounded resolution")
        conflict.update({
            "stage": merged.conflict_stage,
            "head_sha": merged.head_sha,
            "merge_head_sha": merged.merge_head_sha,
            "paths": list(merged.conflict_paths),
            "attempts": 0,
            "failure": merged.failure_evidence,
        })
        conflict.pop("development_summary", None)
        for path in merged.conflict_paths:
            if path not in conflict["conflict_paths"]:
                conflict["conflict_paths"].append(path)
        save()
        return ParentSnapshotResult(status="conflict", conflict=conflict)
    candidate_sha = str(conflict["candidate_sha"])
    record = {
        "candidate_sha": candidate_sha,
        "candidate_tree": git.resolve(f"{candidate_sha}^{{tree}}"),
        "source_sha": str(conflict["source_sha"]),
        "target_sha": str(conflict["target_sha"]),
        "head_sha": merged.head_sha,
        "head_tree": git.resolve(f"{merged.head_sha}^{{tree}}"),
        "conflict_paths": list(conflict["conflict_paths"]),
    }
    prior = subject.get(record_key)
    if isinstance(prior, dict) and prior != record:
        subject.setdefault(f"{record_key}_history", []).append(prior)
    subject[record_key] = record
    subject.pop(conflict_key, None)
    complete_integration_window(budget_owner if budget_owner is not None else subject)
    save()
    return ParentSnapshotResult(status="clean", record=record)


def record_parent_integration_failure(subject: dict[str, Any], scene: dict[str, Any], error: GitError) -> bool:
    """Keep the failed operation and continue only a provable editable scene."""
    subject.setdefault("integration_work_history", []).append(deepcopy(scene))
    scene["failure"] = error.evidence | {"error": str(error)}
    observed_head = error.evidence.get("head_sha")
    observed_merge = error.evidence.get("merge_head_sha")
    if not isinstance(observed_head, str):
        return False
    if scene["stage"] in scene["resolved_stages"]:
        # The Controller's resolution commit succeeded before a later Git
        # operation failed. Subsequent work must use that newer actual head.
        scene.update(stage="git_check", head_sha=observed_head,
                     merge_head_sha=observed_merge)
        scene["resolved_stages"] = [stage for stage in scene["resolved_stages"] if stage != "git_check"]
    elif observed_head != scene["head_sha"] or observed_merge != scene["merge_head_sha"]:
        return False
    scene.pop("development_summary", None)
    scene["attempts"] = 0
    return True
