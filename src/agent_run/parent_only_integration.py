from __future__ import annotations

"""Parent-only adapter for shared exact-snapshot integration."""

import json
from pathlib import Path
from typing import Any

from agent_run.agents import HumanBlockerResult
from agent_run.artifacts import clear_current_human_blocker
from agent_run.change_delivery_threads import (
    record_development_thread,
    require_development_thread,
    select_development_thread,
)
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.delivery_policy import parent_only_budget_policy_for_job
from agent_run.git import GitError
from agent_run.integration_resync_budget import ResyncBudgetExhausted
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    parent_integration_record_is_valid,
    prepare_parent_snapshot,
)
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.review_budget import reset_budget
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    pending_semantic_attempt,
)


def reconcile_parent_integration(stage: Any, state: dict[str, Any], job: dict[str, Any]) -> bool:
    """Invalidate Parent publication if either remote ref gained outside work."""

    if not parent_integration_record_is_valid(stage.git, job):
        stage._block(state, job, "integration_record_invalid", "Parent integration record is invalid")
        stage.save(state)
        return False
    record = job["parent_integration"]
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    published = job.get("published_sha")
    if source is None:
        _pause(stage, state, job, "remote_source_missing", "Parent source ref is missing")
        return False
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
    job["phase"] = "accepted"
    for key in (
        "publication", "publication_sha", "publication_parent_sha",
        "publication_prior_published_sha", "pending_publication_result",
        "merge_intent", "approval_grant", "ticket_write_intent",
    ):
        job.pop(key, None)
    clear_required_checks_observation(job)
    state["status"] = "parent_delivery_pending"
    state["diagnostics"] = []
    stage.save(state)
    return False


def prepare_parent_integration(
    stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path
) -> bool:
    """Parent-only delivery adapter for the common snapshot operation."""

    while True:
        try:
            intent = job.get("ticket_write_intent")
            own_source_sha = (
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
                record_key="parent_integration",
                save=lambda: stage.save(state),
                own_source_write=own_source_sha if isinstance(own_source_sha, str) else False,
            )
        except ParentHistoryRewritten as error:
            job["parent_integration_history_rewrite"] = {
                "branch": error.branch_kind,
                "previous_sha": error.previous,
                "observed_sha": error.observed,
            }
            _pause(stage, state, job, "integration_history_rewritten", str(error))
            return False
        except ResyncBudgetExhausted:
            _pause(
                stage, state, job, "integration_resync_exhausted",
                "Parent integration resync allowance is exhausted; resume explicitly",
            )
            return False
        except GitError as error:
            _pause(stage, state, job, "integration_failed", str(error))
            return False
        if result.status == "conflict":
            if not resolve_parent_conflict(stage, state, job, checkout, record_key="parent_integration"):
                return False
            try:
                result = finish_parent_snapshot_conflict(
                    git=stage.git,
                    subject=job,
                    checkout=checkout,
                    record_key="parent_integration",
                    save=lambda: stage.save(state),
                )
            except GitError as error:
                _pause(stage, state, job, "integration_conflict_unresolved", str(error))
                return False
            if result.status == "conflict":
                continue
        record = result.record
        if record is None:
            raise ValueError("Parent integration returned no record")
        changed = (
            record["head_sha"] != job["candidate_sha"]
            or record["target_sha"] != job["base_sha"]
        )
        if changed:
            # The demand acceptance remains in the old window. A new exact
            # combination gets its own review and repair allowance.
            job["previous_parent_acceptance_record"] = job.get("acceptance_record")
            resync = job.get("integration_resync")
            current_window = int(resync["window"]) if isinstance(resync, dict) else 1
            prior_window = int(job.get("reviewed_integration_window", 1))
            if job.get("parent_integration_stage") is not True or current_window > prior_window:
                old_policy = parent_only_budget_policy_for_job(
                    job, state_snapshot=state.get("policy_snapshot")
                )
                reset_budget(job, old_policy)
                job["reviewed_integration_window"] = current_window
            job["parent_integration_stage"] = True
            job["base_sha"] = record["target_sha"]
            job["candidate_sha"] = record["head_sha"]
            job["phase"] = "candidate"
            job.pop("acceptance_record", None)
            job.pop("approval_grant", None)
            stage.save(state)
            return False
        return reconcile_parent_integration(stage, state, job)


def _pause(stage: Any, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)


def resolve_parent_conflict(
    stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path,
    *, record_key: str,
) -> bool:
    conflict = job[f"{record_key}_conflict"]
    attempt = pending_semantic_attempt(job, role="development")
    if conflict.get("attempts", 0) >= 1 and attempt is None and "development_summary" not in conflict:
        _pause(stage, state, job, "integration_conflict_unresolved", "Parent conflict repair already attempted")
        return False
    if "development_summary" in conflict and attempt is None:
        return True
    conflict["attempts"] = 1
    select_development_thread(state, job, new_attempt=attempt is None)
    request = stage.adapter.development_request(state, job, checkout)
    request.update({
        "repair_source": "merge_conflict",
        "merge_conflict_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True, indent=2),
        "_prompt_resources": state["prompt_resources"],
    })
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    if attempt is None:
        ordinal_key = f"{record_key}_conflict_ordinal"
        job[ordinal_key] = max(
            int(job.get("modification_attempts", 0)),
            int(job.get(ordinal_key, 0)),
        ) + 1
    attempt = allocate_semantic_attempt(
        job,
        role="development",
        work_subject=work_subject,
        generation=generation,
        currentness_boundary=stage._invocation_boundary(job),
        ordinal=int(job[f"{record_key}_conflict_ordinal"]),
        budget_window=int(job["review_budget"]["window"]),
    )
    request["_invocation_event"] = stage._invocation_events(
        state, job, request, role="development", phase="integration_conflict",
        semantic_attempt=attempt,
    )
    request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
    if job.get("development_new_thread") is True:
        request["_invocation_mode"] = "new-thread"
    elif job.get("development_failure_resume") is True:
        request["_invocation_mode"] = "resume"
    stage.save(state)
    if "development_summary" not in conflict:
        result = stage.agents.develop(request)
        require_development_thread(job, result.thread_id, requested_thread=request.get("thread_id"))
        clear_initial_credential_wait(state, work_subject=stage.contract.label)
        record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            stage._wait_for_human(
                state, job, phase="accepted", blockers=result.human_blockers,
                code="agent_requires_human",
            )
            return False
        if not isinstance(result.summary, str) or not result.summary.strip():
            _pause(stage, state, job, "integration_conflict_unresolved", "Conflict Agent returned no result")
            return False
        conflict["development_summary"] = result.summary
        clear_current_human_blocker(job)
        stage.save(state)
        stage._reject_stale(
            state, job, checkout,
            "Parent conflict resolution was discarded after requirements changed",
        )
    close_semantic_attempt(
        job, attempt, outcome="integration_conflict_resolved",
        result={"development_summary": conflict["development_summary"]},
    )
    stage.save(state)
    return True
