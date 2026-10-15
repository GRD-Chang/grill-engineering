from __future__ import annotations

"""Live source and target integration for a Run Repair Change Job."""

from pathlib import Path
from typing import Any

from agent_run.delivery_budget import repair_integration_budget_owner
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


def needs_repair_integration(stage: Any, job: dict[str, Any]) -> bool:
    _source, target = stage.github.live_branch_heads(stage.contract.branch, stage.contract.base_branch)
    return isinstance(job.get(RECORD), dict) or target != job["base_sha"]


def reconcile_repair_integration(stage: Any, state: dict[str, Any], job: dict[str, Any]) -> bool:
    _source, target = stage.github.live_branch_heads(stage.contract.branch, stage.contract.base_branch)
    if target != job["base_sha"]:
        job["phase"] = "accepted"
        stage.save(state)
        return False
    return True


def prepare_repair_integration(
    stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path
) -> bool:
    """Integrate the exact live snapshot, then require review of changed code."""

    while True:
        try:
            result = prepare_parent_snapshot(
                git=stage.git,
                github=stage.github,
                subject=job,
                budget_owner=repair_integration_budget_owner(state, job),
                checkout=checkout,
                source_branch=stage.contract.branch,
                local_source_sha=str(job["base_sha"]),
                target_branch=stage.contract.base_branch,
                candidate_sha=str(job["candidate_sha"]),
                policy_snapshot=job["policy_snapshot"],
                record_key=RECORD,
                save=lambda: stage.save(state),
                own_source_write=False,
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
                    budget_owner=repair_integration_budget_owner(state, job),
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
