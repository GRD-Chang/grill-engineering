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
from agent_run.git import GitError
from agent_run.final_approval_operation import record_approval_update
from agent_run.github import GitHubReadError
from agent_run.integration_resync_budget import ResyncBudgetExhausted, observe_integration_snapshot
from agent_run.integration_development import failure_scene, resolve_integration_work
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    parent_integration_record_is_valid,
    prepare_parent_snapshot,
)
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.review_budget import can_start_development, mark_development
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    pending_semantic_attempt,
)


def reconcile_parent_integration(
    stage: Any, state: dict[str, Any], job: dict[str, Any],
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
            evidence = error.evidence
            if not all(isinstance(evidence.get(key), str) for key in ("source_sha", "target_sha")):
                _pause(stage, state, job, "integration_failed", str(error))
                return False
            scene = job.get("parent_integration_conflict")
            if isinstance(scene, dict):
                # A fetch failure must not replace an unfinished merge scene.
                scene["failure"] = dict(evidence) | {"error": str(error)}
            else:
                scene = failure_scene(
                    stage, job, checkout, error, source=evidence["source_sha"],
                    target=evidence["target_sha"], paths=[],
                )
                if evidence.get("operation") == "fetch_remote_commit":
                    # Fetch executes in the repository root; its HEAD is
                    # evidence for that operation, not the writable scene.
                    scene["head_sha"] = stage.git.checkout_head(checkout)
                    scene["merge_head_sha"] = stage.git._resolve_in_optional(checkout, "MERGE_HEAD")
                scene.update(resolved_stages=[], conflict_paths=[])
            job["parent_integration_conflict"] = scene
            stage.save(state)
            if not resolve_parent_conflict(stage, state, job, checkout, record_key="parent_integration"):
                return False
            continue
        if result.status == "conflict":
            if not resolve_parent_conflict(stage, state, job, checkout, record_key="parent_integration"):
                return False
            continue
        record = result.record
        if record is None:
            raise ValueError("Parent integration returned no record")
        changed = (
            record["head_sha"] != job["candidate_sha"]
            or record["target_sha"] != job["base_sha"]
        )
        acceptance = job.get("acceptance_record")
        if changed and isinstance(acceptance, dict):
            job["previous_parent_acceptance_record"] = acceptance
        job["base_sha"] = record["target_sha"]
        job["candidate_sha"] = record["head_sha"]
        current = isinstance(acceptance, dict) and stage.adapter.acceptance_is_current(state, job, acceptance)
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
            stage.save(state)
            # A candidate already awaiting integration Review can proceed now.
            return awaiting_review and not changed
        return reconcile_parent_integration(stage, state, job)


def _pause(stage: Any, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)


def resolve_parent_conflict(
    stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path,
    *, record_key: str,
) -> bool:
    if record_key == "parent_integration":
        def finish() -> bool:
            scene = job[f"{record_key}_conflict"]
            failure = scene.get("failure")
            if isinstance(failure, dict) and failure.get("operation") == "fetch_remote_commit":
                stage.git.fetch_remote_commit(stage.contract.branch, scene["source_sha"])
                stage.git.fetch_remote_commit(stage.contract.base_branch, scene["target_sha"])
            observe_integration_snapshot(
                job, source_sha=scene["source_sha"], target_sha=scene["target_sha"],
                policy_snapshot=job["policy_snapshot"],
            )
            result = finish_parent_snapshot_conflict(
                git=stage.git, subject=job, checkout=checkout,
                record_key=record_key,
                # Persist progress on this same scene. Switching to a new
                # scene (or finishing it) is atomic with Attempt closeout in
                # the shared executor, so recovery cannot charge new work to
                # the previous completed Development.
                save=lambda: stage.save(state) if isinstance(job.get(f"{record_key}_conflict"), dict)
                    and "development_summary" in job[f"{record_key}_conflict"] else None,
            )
            return result.status != "conflict"

        return resolve_integration_work(
            stage, state, job, checkout,
            scene_key=f"{record_key}_conflict", finish=finish,
        )
    conflict = job[f"{record_key}_conflict"]
    attempt = pending_semantic_attempt(job, role="development")
    new_run_development = (
        record_key == "run_repair_integration"
        and attempt is None and "development_summary" not in conflict
    )
    if new_run_development:
        if not can_start_development(job, stage.review_budget_policy()):
            stage._checkpoint_budget(state, job, "modification_budget_exhausted",
                                     "Parent integration Development allowance is exhausted")
            return False
    if (
        conflict.get("attempts", 0) >= 1 and attempt is None
        and "development_summary" not in conflict and not new_run_development
    ):
        _pause(stage, state, job, "integration_conflict_unresolved", "Parent conflict repair already attempted")
        return False
    if "development_summary" in conflict and attempt is None:
        return True
    if "development_summary" not in conflict:
        stage.require_agent_work("integration_conflict")
        if new_run_development:
            # A mechanical advance may reach another Git failure after its
            # one admitted Agent call. Denied work must not consume allowance.
            mark_development(job, stage.review_budget_policy(), attempt_kind="ordinary")
    conflict["attempts"] = 1
    select_development_thread(state, job, new_attempt=attempt is None)
    conflict_input = {
        "repair_source": "merge_conflict",
        "merge_conflict_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True, indent=2),
    }
    if record_key == "run_repair_integration":
        conflict_input = {
            "repair_source": "integration",
            "integration_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True, indent=2),
            "work_source": "integration",
        }
    # Select this invocation's evidence before the adapter reads historical repair input.
    request = stage.adapter.development_request(state, {**job, **conflict_input}, checkout)
    request.update({**conflict_input, "_prompt_resources": state["prompt_resources"]})
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
