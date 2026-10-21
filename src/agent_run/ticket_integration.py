from __future__ import annotations

"""Ticket delivery integration after fixed-base Development and Review."""

from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_state import ChangeDeliveryStateStore, invalidate_ticket_publication
from agent_run.delivery_step import DeliveryStepKind
from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import ResyncBudgetExhausted
from agent_run.parent_integration import (
    ParentHistoryRewritten, finish_ticket_snapshot, own_ticket_source_write,
    prepare_ticket_snapshot,
)

if TYPE_CHECKING:
    from agent_run.change_delivery import ChangeDeliveryEngine


def _pause(stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)


def delivery_candidate_sha(job: dict[str, Any]) -> str:
    record = job.get("ticket_integration")
    if isinstance(record, dict) and record.get("candidate_sha") == job.get("candidate_sha"):
        return str(record["head_sha"])
    return str(job["candidate_sha"])


def delivery_base_sha(job: dict[str, Any]) -> str:
    record = job.get("ticket_integration")
    if isinstance(record, dict) and (
        record.get("candidate_sha") == job.get("candidate_sha")
        or (
            job.get("attempt_kind") == "ci_repair"
            and record.get("head_sha") == job.get("candidate_sha")
        )
    ):
        return str(record["target_sha"])
    return str(job["base_sha"])


def integration_record_is_valid(git: GitRepository, job: dict[str, Any]) -> bool:
    record = job.get("ticket_integration")
    if not isinstance(record, dict) or set(record) != {
        "base_sha", "candidate_sha", "candidate_tree", "source_sha", "target_sha",
        "head_sha", "head_tree", "conflict_paths",
    }:
        return False
    if (
        any(not isinstance(record[key], str) or not record[key] for key in record if key != "conflict_paths")
        or not isinstance(record["conflict_paths"], list)
        or any(not isinstance(path, str) or not path for path in record["conflict_paths"])
    ):
        return False
    try:
        promoted_ci_candidate = (
            job.get("attempt_kind") == "ci_repair"
            and record.get("head_sha") == job.get("candidate_sha")
            and record.get("head_tree") == git.resolve(f"{job['candidate_sha']}^{{tree}}")
        )
        return (
            record.get("base_sha") == job.get("base_sha")
            and (
                promoted_ci_candidate
                or (
                    record.get("candidate_sha") == job.get("candidate_sha")
                    and record.get("candidate_tree") == git.resolve(f"{job['candidate_sha']}^{{tree}}")
                )
            )
            and record.get("candidate_tree") == git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and record.get("head_tree") == git.resolve(f"{record['head_sha']}^{{tree}}")
            and git.is_ancestor(str(record["candidate_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(job["candidate_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["source_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["target_sha"]), str(record["head_sha"]))
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False


def reconcile_ticket_integration(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any],
    *, state_store: ChangeDeliveryStateStore,
) -> bool:
    """Invalidate a published candidate when a new external snapshot is observed."""

    record = job.get("ticket_integration")
    if not isinstance(record, dict) or not integration_record_is_valid(stage.git, job):
        _pause(stage, state, job, "integration_record_invalid", "Ticket integration boundary is invalid")
        return False
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    if source is None:
        _pause(stage, state, job, "remote_source_missing", "Ticket source ref is missing")
        return False
    if (
        job.get("published_sha") == job.get("publication_sha")
        and isinstance(job.get("published_sha"), str)
        and source != job["publication_sha"]
    ):
        invalidate_ticket_publication(state_store, state, job)
        return False
    own_source = own_ticket_source_write(job, source) and source == job.get("publication_sha")
    if (source == record["source_sha"] or own_source) and target == record["target_sha"]:
        return True
    invalidate_ticket_publication(state_store, state, job)
    return False


def prepare_ticket_integration(
    engine: ChangeDeliveryEngine, state: dict[str, Any], job: dict[str, Any], checkout: Path,
) -> bool:
    """Keep demand acceptance while preparing the actual delivery snapshot."""
    prior = job.get("ticket_integration")
    try:
        result = prepare_ticket_snapshot(
            git=engine.git, github=engine.github, state_store=engine.state_store,
            state=state, job=job, checkout=checkout,
        )
        while result.status == "conflict":
            step = engine.work.develop_integration(state, job, checkout, scope="ticket", allow_agent=True)
            if step.kind is not DeliveryStepKind.CONTINUE:
                return False
            result = finish_ticket_snapshot(
                git=engine.git, github=engine.github, state_store=engine.state_store,
                state=state, job=job, checkout=checkout,
            )
    except ParentHistoryRewritten as error:
        job["integration_history_rewrite"] = {
            "branch": error.branch_kind, "previous_sha": error.previous, "observed_sha": error.observed,
        }
        _pause(engine, state, job, "integration_history_rewritten", str(error))
        return False
    except ResyncBudgetExhausted:
        _pause(engine, state, job, "integration_resync_exhausted",
               "Ticket integration resync allowance is exhausted; resume explicitly to renew it")
        return False
    record = result.record
    if not isinstance(record, dict):
        raise ValueError("Ticket integration returned no record")
    # An unchanged record already proved the observed pair. Only a newly
    # integrated result needs the original final live publication gate.
    if record == prior:
        if not integration_record_is_valid(engine.git, job):
            _pause(engine, state, job, "integration_record_invalid", "Ticket integration boundary is invalid")
            return False
        return True
    return reconcile_ticket_integration(engine, state, job, state_store=engine.state_store)
