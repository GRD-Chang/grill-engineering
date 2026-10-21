from __future__ import annotations

"""Run Repair integration policy around exact local-source preparation."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.change_delivery_state import (
    ChangeDeliveryStateStore, block_change_job, save_change_job,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.delivery_step import DeliveryStepKind
from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import ResyncBudgetExhausted
from agent_run.parent_integration import (
    ParentHistoryRewritten, finish_repair_snapshot,
    parent_integration_record_is_valid, prepare_repair_snapshot,
)

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


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


def needs_repair_integration(
    github: GitHubPublisher, state: dict[str, Any], job: dict[str, Any],
) -> bool:
    _source, target = github.live_branch_heads(str(job["repair_branch"]), str(state["run_branch"]))
    return isinstance(job.get(RECORD), dict) or target != job["base_sha"]


def reconcile_repair_integration(
    github: GitHubPublisher, state_store: ChangeDeliveryStateStore,
    state: dict[str, Any], job: dict[str, Any],
) -> bool:
    _source, target = github.live_branch_heads(str(job["repair_branch"]), str(state["run_branch"]))
    if target != job["base_sha"]:
        job["phase"] = "accepted"
        save_change_job(state_store, state, job, label=f"run-repair-{job['repair_attempt']}", development_only=True)
        return False
    return True


def prepare_repair_integration(
    owner: RunAcceptanceEngine, state_store: ChangeDeliveryStateStore,
    state: dict[str, Any], job: dict[str, Any], checkout: Path,
) -> bool:
    """Prepare the actual combination before reviewing or writing a Repair."""
    github = owner.github
    assert github is not None
    if not needs_repair_integration(github, state, job):
        return True
    while True:
        try:
            result = prepare_repair_snapshot(
                git=owner.git, github=github, state_store=state_store,
                state=state, job=job, checkout=checkout,
            )
        except ParentHistoryRewritten as error:
            job["run_repair_integration_history_rewrite"] = {
                "branch": error.branch_kind, "previous_sha": error.previous, "observed_sha": error.observed,
            }
            _pause(state_store, state, job, "integration_history_rewritten", str(error))
            return False
        except ResyncBudgetExhausted:
            _pause(state_store, state, job, "integration_resync_exhausted", "Run Repair integration resync allowance is exhausted")
            return False
        except GitError as error:
            _pause(state_store, state, job, "integration_failed", str(error))
            return False
        if result.status == "conflict":
            step = owner.work.develop_integration(state, job, checkout, scope="repair", allow_agent=True)
            if step.kind is not DeliveryStepKind.CONTINUE:
                return False
            try:
                result = finish_repair_snapshot(
                    git=owner.git, github=github, state_store=state_store,
                    state=state, job=job, checkout=checkout,
                )
            except GitError as error:
                _pause(state_store, state, job, "integration_failed", str(error))
                return False
            if result.status == "conflict":
                # The next Repair unit first observes the live snapshot.
                continue
        record = result.record
        if record is None:
            raise ValueError("Run Repair integration returned no record")
        if record["head_sha"] != job["candidate_sha"] or record["target_sha"] != job["base_sha"]:
            job["previous_run_repair_acceptance_record"] = job.get("acceptance_record")
            local_run_head = owner.git.resolve(str(state["run_branch"]))
            if local_run_head == job["base_sha"]:
                job.setdefault("run_branch_local_sha", local_run_head)
            job["base_sha"] = record["target_sha"]
            job["repair_base_run_head_sha"] = record["target_sha"]
            job["candidate_sha"] = record["head_sha"]
            job["phase"] = "candidate"
            job.pop("acceptance_record", None)
            job.pop("approval_grant", None)
            save_change_job(state_store, state, job, label=f"run-repair-{job['repair_attempt']}", development_only=True)
            return False
        return reconcile_repair_integration(github, state_store, state, job)


def _pause(
    state_store: ChangeDeliveryStateStore, state: dict[str, Any], job: dict[str, Any], code: str, message: str,
) -> None:
    label = f"run-repair-{job['repair_attempt']}"
    block_change_job(state_store, state, job, label=label, development_only=True, code=code, message=message)
    state["terminal_kind"] = "waiting_human"
    save_change_job(state_store, state, job, label=label, development_only=True)
