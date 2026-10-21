from __future__ import annotations

"""Parent-only integration policy after exact Git snapshot preparation."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.delivery_step import DeliveryStepKind
from agent_run.final_approval_operation import record_approval_update
from agent_run.git import GitError
from agent_run.github import GitHubReadError
from agent_run.integration_resync_budget import ResyncBudgetExhausted
from agent_run.parent_integration import (
    ParentHistoryRewritten, finish_parent_snapshot,
    parent_integration_record_is_valid, prepare_parent_snapshot,
)
from agent_run.required_checks_observation import clear_required_checks_observation

if TYPE_CHECKING:
    from agent_run.change_delivery import ChangeDeliveryEngine


def reconcile_parent_integration(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any],
    *, live: dict[str, Any] | None = None,
) -> bool:
    """Invalidate Parent publication if either remote ref gained outside work."""

    if not parent_integration_record_is_valid(stage.git, job):
        stage._block(state, job, "integration_record_invalid", "Parent integration record is invalid")
        stage.save(state)
        return False
    record = job["parent_integration"]
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    if live is not None and (
        live.get("state") != "OPEN"
        or live.get("head_branch") != stage.contract.branch
        or live.get("head_repository") != state.get("repository")
        or live.get("base_repository") != state.get("repository")
        or live.get("base_branch") != stage.contract.base_branch
        or not isinstance(live.get("head_sha"), str) or not live["head_sha"]
        or not isinstance(live.get("base_sha"), str) or not live["base_sha"]
    ):
        _pause(stage, state, job, "published_head_mismatch", "Parent PR identity does not match its remote refs")
        return False
    published = job.get("published_sha")
    if source is None:
        _pause(stage, state, job, "remote_source_missing", "Parent source ref is missing")
        return False
    if live is not None and (
        live["head_sha"] != source or live["base_sha"] != target
    ):
        # These reads are not atomic. Let the existing bounded supervisor
        # retry the whole publication gate, including PR identity/merge state;
        # do not let callers continue with their earlier live PR snapshot.
        raise GitHubReadError(
            "github_pr_head_observation_pending",
            "Parent PR and remote branch heads have not converged",
        )
    if isinstance(published, str) and source != published:
        try:
            stage.git.fetch_remote_commit(stage.contract.branch, source)
            if not stage.git.is_ancestor(published, source):
                raise ParentHistoryRewritten("source", published, source)
        except ParentHistoryRewritten as error:
            _pause(stage, state, job, "integration_history_rewritten", str(error))
            return False
        except GitError as error:
            _pause(stage, state, job, "integration_failed", str(error))
            return False
    intent = job.get("ticket_write_intent")
    own_source = source == job.get("publication_sha") and (
        source == job.get("published_sha")
        or (
            isinstance(intent, dict)
            and intent.get("action") == "publish_ticket_ref"
            and intent.get("head_sha") == source
        )
    )
    if (source == record["source_sha"] or own_source) and target == record["target_sha"]:
        return True
    # Only append-only snapshots may end approval in favour of automatic
    # updating. A rewritten target must retain the ordinary human gate.
    try:
        stage.git.fetch_remote_commit(stage.contract.base_branch, target)
        if not stage.git.is_ancestor(str(record["target_sha"]), target):
            raise ParentHistoryRewritten("target", str(record["target_sha"]), target)
    except ParentHistoryRewritten as error:
        _pause(stage, state, job, "integration_history_rewritten", str(error))
        return False
    except GitError as error:
        _pause(stage, state, job, "integration_failed", str(error))
        return False
    grant = job.get("approval_grant")
    if isinstance(grant, dict):
        record_approval_update(
            job, previous_head=job["publication_sha"], previous_base=record["target_sha"],
            source=source, target=target,
        )
    job["phase"] = "accepted"
    for key in (
        "publication", "publication_sha", "publication_parent_sha",
        "publication_prior_published_sha", "pending_publication_result",
        "merge_intent", "approval_grant", "ticket_write_intent", "publication_commit_intent",
    ):
        job.pop(key, None)
    clear_required_checks_observation(job)
    state["status"] = "parent_delivery_pending"
    state["terminal_kind"] = None
    state["diagnostics"] = []
    stage.save(state)
    return False


def prepare_parent_integration(
    engine: ChangeDeliveryEngine, state: dict[str, Any], job: dict[str, Any], checkout: Path,
) -> bool:
    """Require a current complete Parent review of the prepared combination."""
    try:
        result = prepare_parent_snapshot(
            git=engine.git, github=engine.github, state_store=engine.state_store,
            state=state, job=job, checkout=checkout,
        )
    except ParentHistoryRewritten as error:
        job["parent_integration_history_rewrite"] = {
            "branch": error.branch_kind, "previous_sha": error.previous, "observed_sha": error.observed,
        }
        _pause(engine, state, job, "integration_history_rewritten", str(error))
        return False
    except ResyncBudgetExhausted:
        _pause(engine, state, job, "integration_resync_exhausted",
               "Parent integration resync allowance is exhausted; resume explicitly")
        return False
    except GitError as error:
        _pause(engine, state, job, "integration_failed", str(error))
        return False
    while result.status == "conflict":
        step = engine.work.develop_integration(state, job, checkout, scope="parent-only", allow_agent=True)
        if step.kind is not DeliveryStepKind.CONTINUE:
            return False
        try:
            result = finish_parent_snapshot(
                git=engine.git, github=engine.github, state_store=engine.state_store,
                state=state, job=job, checkout=checkout,
            )
        except GitError as error:
            _pause(engine, state, job, "integration_record_invalid", str(error))
            return False
    record = result.record
    if record is None:
        raise ValueError("Parent integration returned no record")
    changed = record["head_sha"] != job["candidate_sha"] or record["target_sha"] != job["base_sha"]
    acceptance = job.get("acceptance_record")
    if changed and isinstance(acceptance, dict):
        job["previous_parent_acceptance_record"] = acceptance
    job["base_sha"] = record["target_sha"]
    job["candidate_sha"] = record["head_sha"]
    current = isinstance(acceptance, dict) and engine.adapter.acceptance_is_current(state, job, acceptance)
    if changed or not current:
        awaiting_review = job.get("phase") == "candidate"
        job["parent_integration_stage"] = True
        job["phase"] = "candidate"
        for key in (
            "acceptance_record", "approval_grant", "publication", "publication_sha",
            "pending_publication_result", "publication_parent_sha", "merge_intent",
            "publication_commit_intent",
        ):
            job.pop(key, None)
        engine.save(state)
        return awaiting_review and not changed
    return reconcile_parent_integration(engine, state, job)


def _pause(stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)
