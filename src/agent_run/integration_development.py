"""Development work and deterministic closeout for a recorded Git scene."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_run.agents import HumanBlockerResult
from agent_run.artifacts import clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_threads import (
    record_development_thread, require_development_thread, select_development_thread,
)
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.git_errors import GitError
from agent_run.integration_development_budget import (
    IntegrationDevelopmentExhausted, acquire_integration_development,
    ensure_integration_development_available,
)
from agent_run.semantic_attempt import (
    allocate_semantic_attempt, close_semantic_attempt, pending_semantic_attempt,
)


def failure_scene(
    stage: ChangeDeliveryStage, job: dict[str, Any], checkout: Path, error: GitError,
    *, source: str, target: str, paths: list[str],
) -> dict[str, Any]:
    evidence = dict(error.evidence or {})
    head = evidence.get("head_sha") or stage.git.checkout_head(checkout)
    merge_head = evidence.get("merge_head_sha")
    return {
        "source_sha": source, "target_sha": target,
        "candidate_sha": str(job["candidate_sha"]), "stage": "git_check",
        "head_sha": head, "merge_head_sha": merge_head, "paths": list(paths),
        "attempts": 0, "failure": {**evidence, "error": str(error)},
        "checkout": str(checkout),
    }


def resolve_integration_work(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], checkout: Path,
    *, scene_key: str = "integration_conflict",
    finish: Callable[[], bool] | None = None,
) -> bool:
    """Complete one semantic Development unit, then let Git prove its result.

    A completed developer summary survives interruption before Git closeout.
    A failed closeout records new evidence and ends that unit; the next unit
    consumes the same cumulative integration allowance, with no Review.
    """
    while True:
        result = _resolve_integration_work_once(stage, state, job, checkout, scene_key=scene_key, finish=finish)
        if result is not None:
            return result


def _resolve_integration_work_once(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], checkout: Path,
    *, scene_key: str, finish: Callable[[], bool] | None,
) -> bool | None:
    scene = job[scene_key]
    if finish is None and isinstance(scene.get("resolution_sha"), str):
        if stage.git.checkout_head(checkout) != scene["resolution_sha"]:
            raise GitError("Recorded integration resolution no longer matches checkout")
        return True
    existing = pending_semantic_attempt(job, role="development")
    if existing is None:
        try:
            ensure_integration_development_available(job, policy_snapshot=job["policy_snapshot"])
        except IntegrationDevelopmentExhausted:
            stage._block(state, job, "integration_development_exhausted",
                         "Integration Development allowance is exhausted; resume explicitly to renew it")
            state["terminal_kind"] = "waiting_human"
            stage.save(state)
            return False
    if "development_summary" not in scene:
        stage.require_agent_work("integration_conflict")
    select_development_thread(state, job, new_attempt=existing is None)
    if existing is None:
        job["integration_conflict_agent_ordinal"] = max(
            int(job.get("modification_attempts", 0)),
            int(job.get("integration_conflict_agent_ordinal", 0)),
        ) + 1
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    attempt = allocate_semantic_attempt(
        job, role="development", work_subject=work_subject, generation=generation,
        currentness_boundary=stage._invocation_boundary(job),
        ordinal=int(job["integration_conflict_agent_ordinal"]),
        budget_window=int(job["integration_development_budget"]["window"]),
    )
    acquire_integration_development(job, attempt_id=attempt["attempt_id"], policy_snapshot=job["policy_snapshot"])
    scene["attempts"] = 1
    repair_input = {
        "repair_source": "integration",
        "integration_evidence": json.dumps(scene, ensure_ascii=False, sort_keys=True, indent=2),
        "work_source": "integration",
    }
    request = stage.adapter.development_request(state, {**job, **repair_input}, checkout)
    request.update({**repair_input, "_prompt_resources": state["prompt_resources"]})
    request["_invocation_event"] = stage._invocation_events(
        state, job, request, role="development", phase="integration_conflict", semantic_attempt=attempt,
    )
    request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
    if job.get("development_new_thread") is True:
        request["_invocation_mode"] = "new-thread"
    elif job.get("development_failure_resume") is True:
        request["_invocation_mode"] = "resume"
    stage.save(state)
    if "development_summary" not in scene:
        try:
            result = stage.agents.develop(request)
        finally:
            stage.release_agent_work()
        require_development_thread(job, result.thread_id, requested_thread=request.get("thread_id"))
        clear_initial_credential_wait(state, work_subject=stage.contract.label)
        if not stage.adapter.development_thread_is_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            stage._wait_for_human(state, job,
                phase="accepted",
                blockers=result.human_blockers, code="agent_requires_human")
            return False
        if not result.summary.strip():
            raise ValueError("Integration Development result is incomplete")
        scene["development_summary"] = result.summary
        clear_current_human_blocker(job)
        stage.save(state)
    stage._reject_stale(state, job, checkout,
                        "Integration work was discarded after requirements changed")
    summary = scene["development_summary"]
    completed_scene = deepcopy(scene)
    try:
        if finish is not None:
            completed = finish()
            resolved = None
        else:
            resolved = stage.git.commit_integration_work(
                checkout, expected_head_sha=str(scene["head_sha"]),
                expected_merge_sha=scene["merge_head_sha"],
                message=f"chore(ticket-{job['ticket_number']}): resolve integration conflict",
            )
    except GitError as error:
        close_semantic_attempt(job, attempt, outcome="integration_git_check_failed",
                               result={"development_summary": summary})
        job.setdefault("integration_work_history", []).append(completed_scene)
        observed_head = error.evidence.get("head_sha")
        observed_merge = error.evidence.get("merge_head_sha")
        checkpoint = (
            finish is not None and scene.get("stage") in scene.get("resolved_stages", [])
            and observed_head == scene.get("resolution_sha") and observed_merge is None
        )
        if observed_head is not None and not checkpoint and (
            observed_head != scene["head_sha"] or observed_merge != scene["merge_head_sha"]
        ):
            scene["failure"] = error.evidence | {"error": str(error)}
            stage._block(state, job, "integration_record_invalid", str(error))
            stage.save(state)
            return False
        if finish is None:
            job[scene_key] = failure_scene(
                stage, job, checkout, error, source=scene["source_sha"],
                target=scene["target_sha"], paths=scene["paths"],
            )
        else:
            scene["failure"] = error.evidence | {"error": str(error)}
            scene.pop("development_summary", None)
        stage.save(state)
        return None
    close_semantic_attempt(job, attempt, outcome="integration_conflict_resolved",
                           result={"development_summary": summary})
    if finish is None:
        scene["resolution_sha"] = resolved
    job.setdefault("integration_work_history", []).append(completed_scene if finish is not None else deepcopy(scene))
    stage.save(state)
    return (True if completed else None) if finish is not None else True
