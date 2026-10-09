from __future__ import annotations

"""Live source and target integration for a Run Repair Change Job."""

import re
from pathlib import Path
from typing import Any

from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import ResyncBudgetExhausted
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    parent_integration_record_is_valid,
    prepare_parent_snapshot,
    record_parent_integration_failure,
)
from agent_run.parent_only_integration import resolve_parent_conflict
from agent_run.required_checks_observation import clear_required_checks_observation


RECORD = "run_repair_integration"


def repair_run_branch_is_current(
    git: GitRepository, job: dict[str, Any], local_head: str,
) -> bool:
    if local_head in {job.get("base_sha"), job.get("integrated_sha")}:
        return True
    record = job.get(RECORD)
    anchor = job.get("run_branch_local_sha")
    return (
        isinstance(anchor, str)
        and local_head == anchor
        and isinstance(record, dict)
        and parent_integration_record_is_valid(
            # The record proves the last confirmed target snapshot. A later
            # Reviewer finding can produce a new Candidate before another
            # integration, without changing that confirmed target or the
            # managed local Run ref.
            git, {**job, "candidate_sha": record.get("head_sha"),
                  "parent_integration": record}
        )
        and git.is_ancestor(anchor, str(record["target_sha"]))
    )


def observe_repair_source(owner: Any, state: dict[str, Any], job: dict[str, Any]) -> bool:
    """Allow an append-only live source past initial exact-ref creation.

    Return true when the source already exists and is a descendant of the
    durable publication boundary. The shared snapshot stage then integrates
    its actual contents before any new publication.
    """

    github = owner.github
    if github is None:
        raise ValueError("Run Repair requires the Publisher")
    branch = str(job["repair_branch"])
    source, _target = github.live_branch_heads(branch, str(state["run_branch"]))
    expected = str(job.get("published_sha", job["base_sha"]))
    if source is None or source == expected:
        return False
    intent = job.get("ticket_write_intent")
    if (
        isinstance(intent, dict)
        and intent.get("action") == "publish_ticket_ref"
        and intent.get("head_sha") == source
    ):
        return False
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source) is None:
        # An invalid ref value has no commit object to retry reading. Let the
        # exact branch-authority gate preserve its remote_source_changed proof.
        return False
    try:
        owner.git.fetch_remote_commit(branch, source)
    except GitError as error:
        # A transient read failure cannot prove that the remote was rewritten.
        # Keep the PR and Candidate intact for an explicit retry.
        job.update({"phase": "blocked", "blocked_reason": "integration_failed"})
        state.update({
            "status": "ready_for_human",
            "terminal_kind": "waiting_human",
            "diagnostics": [{"code": "integration_failed", "message": str(error)}],
        })
        owner._save(state)
        return True
    if not owner.git.is_ancestor(expected, source):
        return False
    pr_number = job.get("pr_number")
    if type(pr_number) is int:
        live = github.live_pull_request(pr_number)
        if (
            live.get("state") != "OPEN"
            or live.get("head_branch") != branch
            or live.get("head_repository") != state.get("repository")
            or live.get("base_repository") != state.get("repository")
            or live.get("base_branch") != state.get("run_branch")
            or live.get("head_sha") != source
        ):
            job.update({"phase": "blocked", "blocked_reason": "published_head_mismatch"})
            state["status"] = "blocked"
            state["diagnostics"] = [{
                "code": "published_head_mismatch",
                "message": "Run Repair PR identity does not match the live source branch",
            }]
            owner._save(state)
            return True
    if job.get("phase") in {"publishing", "waiting_checks", "waiting_merge"}:
        invalidate_repair_publication(state, job)
        owner._save(state)
    elif isinstance(intent, dict) and intent.get("action") == "ensure_change_branch":
        # The ref was created before its response was lost. Its descendant is
        # now the live source authority, so the old exact creation intent is
        # satisfied and must not be replayed against the older base.
        job.pop("ticket_write_intent", None)
        owner._save(state)
    return True


def invalidate_repair_publication(state: dict[str, Any], job: dict[str, Any]) -> None:
    job["phase"] = "accepted"
    for key in (
        "publication", "publication_sha", "publication_parent_sha",
        "publication_prior_published_sha", "pending_publication_result",
        "merge_intent", "ticket_write_intent", "approval_grant", "publication_commit_intent",
    ):
        job.pop(key, None)
    clear_required_checks_observation(job)
    state["status"] = "run_acceptance_pending"
    state["diagnostics"] = []


def reconcile_repair_integration(stage: Any, state: dict[str, Any], job: dict[str, Any]) -> bool:
    record = job.get(RECORD)
    if isinstance(record, dict) and not parent_integration_record_is_valid(stage.git, {
        **job, "parent_integration": record,
    }):
        stage._block(state, job, "integration_record_invalid", "Run Repair integration record is invalid")
        stage.save(state)
        return False
    source, target = stage.github.live_branch_heads(stage.contract.branch, stage.contract.base_branch)
    published = job.get("published_sha")
    if isinstance(published, str):
        try:
            if source is None:
                raise GitError("Run Repair source ref disappeared")
            stage.git.fetch_remote_commit(stage.contract.branch, source)
            if not stage.git.is_ancestor(published, source):
                raise ParentHistoryRewritten("source", published, source)
        except ParentHistoryRewritten as error:
            job["run_repair_integration_history_rewrite"] = {
                "branch": error.branch_kind,
                "previous_sha": error.previous,
                "observed_sha": error.observed,
            }
            _pause(stage, state, job, "integration_history_rewritten", str(error))
            return False
        except GitError as error:
            _pause(stage, state, job, "integration_failed", str(error))
            return False
    intent = job.get("ticket_write_intent")
    own_source = source == job.get("publication_sha") and (
        source == job.get("published_sha")
        or (isinstance(intent, dict) and intent.get("action") == "publish_ticket_ref" and intent.get("head_sha") == source)
    )
    expected_source = (
        record["source_sha"] if isinstance(record, dict)
        else str(job.get("published_sha", job["base_sha"]))
    )
    expected_target = (
        record["target_sha"] if isinstance(record, dict) else job["base_sha"]
    )
    if (source == expected_source or own_source) and target == expected_target:
        return True
    invalidate_repair_publication(state, job)
    stage.save(state)
    return False


def needs_repair_integration(stage: Any, job: dict[str, Any]) -> bool:
    if isinstance(job.get(RECORD), dict):
        return True
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    expected_source = str(job.get("published_sha", job["base_sha"]))
    return bool(source != expected_source or target != str(job["base_sha"]))


def prepare_repair_integration(
    stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path
) -> bool:
    """Integrate the exact live snapshot, then require review of changed code."""

    while True:
        try:
            intent = job.get("ticket_write_intent")
            own_source = (
                intent.get("head_sha")
                if isinstance(intent, dict) and intent.get("action") == "publish_ticket_ref"
                else job.get("published_sha")
            )
            result = prepare_parent_snapshot(
                git=stage.git,
                github=stage.github,
                subject=job,
                checkout=checkout,
                source_branch=stage.contract.branch,
                target_branch=stage.contract.base_branch,
                candidate_sha=str(job["candidate_sha"]),
                policy_snapshot=job["policy_snapshot"],
                record_key=RECORD,
                save=lambda: stage.save(state),
                own_source_write=own_source if isinstance(own_source, str) else False,
                handoff_git_failures=True,
            )
        except ParentHistoryRewritten as error:
            job["run_repair_integration_history_rewrite"] = {
                "branch": error.branch_kind,
                "previous_sha": error.previous,
                "observed_sha": error.observed,
            }
            _pause(stage, state, job, "integration_history_rewritten", str(error))
            return False
        except ResyncBudgetExhausted:
            _pause(stage, state, job, "integration_resync_exhausted", "Run Repair integration resync allowance is exhausted")
            return False
        except GitError as error:
            _pause(stage, state, job, "integration_failed", str(error))
            return False
        if result.status == "conflict":
            if not resolve_parent_conflict(stage, state, job, checkout, record_key=RECORD):
                return False
            try:
                result = finish_parent_snapshot_conflict(
                    git=stage.git,
                    subject=job,
                    checkout=checkout,
                    record_key=RECORD,
                    save=lambda: stage.save(state),
                )
            except GitError as error:
                scene = job[f"{RECORD}_conflict"]
                if not record_parent_integration_failure(job, scene, error):
                    _pause(stage, state, job, "integration_failed", str(error))
                    return False
                stage.save(state)
                continue
            if result.status == "conflict":
                continue
        record = result.record
        if record is None:
            raise ValueError("Run Repair integration returned no record")
        if record["head_sha"] != job["candidate_sha"] or record["target_sha"] != job["base_sha"]:
            job["previous_run_repair_acceptance_record"] = job.get("acceptance_record")
            local_run_head = stage.git.resolve(stage.contract.base_branch)
            if local_run_head == job["base_sha"]:
                job.setdefault("run_branch_local_sha", local_run_head)
            job["base_sha"] = record["target_sha"]
            job["repair_base_run_head_sha"] = record["target_sha"]
            job["candidate_sha"] = record["head_sha"]
            job["phase"] = "candidate"
            job.pop("acceptance_record", None)
            job.pop("approval_grant", None)
            stage.save(state)
            return False
        return reconcile_repair_integration(stage, state, job)


def _pause(stage: Any, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)
