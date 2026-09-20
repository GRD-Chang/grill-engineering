from __future__ import annotations

"""Development and Candidate commit stage for Change Delivery."""

from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_run.agents import HumanBlockerResult
from agent_run.artifacts import clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_fallback import (
    preserve_required_checks_publication_authorization,
)
from agent_run.change_delivery_threads import (
    record_development_thread as _record_development_thread,
    select_development_thread,
    require_development_thread,
)
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential as wait_for_initial_credential_state,
)
from agent_run.git_errors import GitError, GitIntegrityError
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.review_budget import repair_budget_context
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    pending_semantic_attempt,
    require_controller_reprepare_intent,
)


def develop(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    resume_after_initial_credential(stage, state, job)
    stage._reject_stale(
        state,
        job,
        checkout,
        "Development did not start after requirements changed",
    )
    pending = job.get("pending_attempt")
    attempt_kind = str(job.get("next_attempt_kind", "ordinary"))
    attempt = (
        pending
        if isinstance(pending, int)
        and pending > int(job.get("modification_attempts", 0))
        else int(job.get("modification_attempts", 0)) + 1
    )
    pending_resume = (
        isinstance(pending, int)
        and pending > int(job.get("modification_attempts", 0))
    )
    if not pending_resume:
        stage.mark_development_attempt(job, attempt_kind=attempt_kind)
    # Persist the actual Worker attempt before invoking Codex.  This
    # makes a foreground status query truthful even while Codex is still
    # running, and leaves an observable recovery boundary on interruption.
    job["pending_attempt"] = attempt
    job["pending_attempt_kind"] = attempt_kind
    if pending_resume:
        if not isinstance(job.get("managed_checkout_head"), str):
            raise ValueError(
                "incompatible_run_state: pending Development is missing its managed checkout head"
            )
    else:
        job["managed_checkout_head"] = stage.git.checkout_head(checkout)
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    budget = job.get("review_budget")
    if not isinstance(budget, dict) or type(budget.get("window")) is not int:
        raise ValueError("Development is missing its Review Budget Window")
    new_semantic_attempt = pending_semantic_attempt(job, role="development") is None
    semantic_attempt = allocate_semantic_attempt(
        job,
        role="development",
        work_subject=work_subject,
        generation=generation,
        currentness_boundary=stage._invocation_boundary(job),
        ordinal=attempt,
        budget_window=int(budget["window"]),
    )
    select_development_thread(state, job, new_attempt=new_semantic_attempt)
    intent = job.get("candidate_commit_intent")
    if not (
        isinstance(intent, dict)
        and intent.get("expected_head") == job["managed_checkout_head"]
        and intent.get("attempt") == attempt
        and isinstance(intent.get("token"), str)
        and intent.get("token")
    ):
        job["candidate_commit_intent"] = {
            "kind": "candidate_commit",
            "expected_head": str(job["managed_checkout_head"]),
            "attempt": attempt,
            "token": uuid4().hex,
        }
    stage.save(state)
    request = stage.adapter.development_request(state, job, checkout)
    request["_prompt_resources"] = state["prompt_resources"]
    if request.get("repair_source") is not None:
        request["review_budget_context"] = repair_budget_context(
            job, stage.review_budget_policy()
        )
    request["_invocation_event"] = stage._invocation_events(
        state,
        job,
        request,
        role="development",
        phase=str(job["phase"]),
        semantic_attempt=semantic_attempt,
    )
    request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
    if job.get("development_new_thread") is True:
        request["_invocation_mode"] = "new-thread"
    elif job.get("development_failure_resume") is True:
        request["_invocation_mode"] = "resume"
    result = stage.agents.develop(request)
    require_development_thread(
        job, result.thread_id, requested_thread=request.get("thread_id")
    )
    clear_initial_credential_wait(state, work_subject=stage.contract.label)
    if isinstance(result, HumanBlockerResult):
        if not stage.adapter.development_thread_is_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        _record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        stage._wait_for_human(
            state, job, phase=str(job["phase"]), blockers=result.human_blockers
        )
        return
    if not result.thread_id.strip() or not result.summary.strip():
        raise ValueError("Development result is incomplete")
    if not stage.adapter.development_thread_is_allowed(state, result.thread_id):
        raise ValueError("Change Job Development Thread is not independent")
    _record_development_thread(job, result.thread_id, result.replaced_thread_id)
    job.pop("development_new_thread", None)
    job.pop("development_failure_resume", None)
    job["development_summary"] = result.summary
    clear_current_human_blocker(job)
    job["phase"] = "committing_candidate"
    stage.save(state)
    stage._reject_stale(
        state,
        job,
        checkout,
        "Development result was discarded after requirements changed",
    )


def wait_for_initial_credential(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    *,
    http_status: int | None,
) -> None:
    wait_for_initial_credential_state(
        state,
        work_subject=stage.contract.label,
        phase=str(job["phase"]),
        resume_status=str(state.get("status", "active")),
        http_status=http_status,
    )
    stage.save(state)


def resume_after_initial_credential(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any]
) -> None:
    if resume_initial_credential_wait(state, work_subject=stage.contract.label):
        stage.save(state)


def commit_candidate(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> bool:
    attempt = int(job["pending_attempt"])
    controller_reprepared = "controller_candidate_reprepare" in job
    if controller_reprepared:
        require_controller_reprepare_intent(job)
    expected_head = str(
        job.get("managed_checkout_head")
        or job.get("candidate_sha")
        or job.get("base_sha")
    )
    intent = job.get("candidate_commit_intent")
    if not (
        isinstance(intent, dict)
        and intent.get("expected_head") == expected_head
        and intent.get("attempt") == attempt
        and isinstance(intent.get("token"), str)
        and intent.get("token")
    ):
        intent = {
            "kind": "candidate_commit",
            "expected_head": expected_head,
            "attempt": attempt,
            "token": uuid4().hex,
        }
        job["candidate_commit_intent"] = intent
        stage.save(state)
    try:
        candidate = stage.publisher.commit_candidate(checkout, job, attempt)
    except GitIntegrityError as error:
        evidence = dict(error.evidence)
        evidence.update(
            {
                "expected_head": str(
                    expected_head
                ),
                "base_sha": str(job.get("base_sha", "")),
                "previous_candidate_sha": str(job.get("candidate_sha", "")),
            }
        )
        return _route_git_integrity_repair(stage, state, job, checkout, evidence)
    if candidate is None:
        observed_head = stage.git.checkout_head(checkout)
        if observed_head != expected_head:
            evidence = {
                "kind": "git_integrity",
                "status": "fail",
                "reason": "clean checkout head differs from the managed Candidate boundary",
                "expected_head": expected_head,
                "observed_head": observed_head,
                "base_sha": str(job.get("base_sha", "")),
                "previous_candidate_sha": str(job.get("candidate_sha", "")),
                "workspace_clean": "true",
            }
            return _route_git_integrity_repair(stage, state, job, checkout, evidence)
        semantic_attempt = pending_semantic_attempt(job, role="development")
        if semantic_attempt is None and not controller_reprepared:
            raise ValueError("Development closeout is missing its Semantic Attempt")
        if semantic_attempt is not None:
            close_semantic_attempt(
                job,
                semantic_attempt,
                outcome="no_code_changes",
                result=_development_attempt_result(job),
            )
        job.pop("controller_candidate_reprepare", None)
        job.pop("pending_attempt", None)
        job.pop("candidate_commit_intent", None)
        return stage._block(
            state,
            job,
            "no_code_changes",
            "Development Attempt produced no code changes",
        )
    preserve_required_checks_publication_authorization(job)
    clear_required_checks_observation(job)
    job.update(
        {
            "candidate_sha": candidate,
            "modification_attempts": attempt,
            "code_modification_attempts": attempt,
            "attempt_kind": str(job.pop("pending_attempt_kind", "ordinary")),
            "phase": "candidate",
        }
    )
    job.pop("next_attempt_kind", None)
    job.pop("candidate_commit_intent", None)
    stage._sync_attempts(state, job)
    semantic_attempt = pending_semantic_attempt(job, role="development")
    if semantic_attempt is None and not controller_reprepared:
        raise ValueError("Candidate closeout is missing its Semantic Attempt")
    if semantic_attempt is not None:
        close_semantic_attempt(
            job,
            semantic_attempt,
            outcome="candidate",
            result=_development_attempt_result(job),
        )
    job.pop("controller_candidate_reprepare", None)
    job.pop("pending_attempt", None)
    stage.save(state)
    return True


def _route_git_integrity_repair(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
    evidence: dict[str, str],
) -> bool:
    """Return an integrity finding to the same Development Thread."""

    expected_head = evidence.get("expected_head") or job.get("managed_checkout_head")
    if not isinstance(expected_head, str) or not expected_head:
        raise GitError("Git Integrity Repair is missing its legal checkout boundary")
    try:
        stage.git.restore_managed_checkout(checkout, expected_head=expected_head)
    except GitError as error:
        evidence["recovery_error"] = str(error)
        job["git_integrity_evidence"] = evidence
        job.update(
            {
                "phase": "escalating",
                "escalation_code": "git_integrity_boundary_restore_failed",
            }
        )
        stage.save(state)
        return True

    evidence["recovery_head"] = expected_head
    evidence["recovery_action"] = "controller_reset_and_clean"
    job["git_integrity_evidence"] = evidence
    job["repair_source"] = "git_integrity"
    job.pop("ci_evidence", None)
    clear_required_checks_observation(job)
    job.pop("final_ci_fix_failure_head", None)
    job["phase"] = "repairing"
    job["managed_checkout_head"] = expected_head
    job["modification_attempts"] = int(job["pending_attempt"])
    job["code_modification_attempts"] = int(job["pending_attempt"])
    job["attempt_kind"] = "ordinary"
    job.pop("candidate_sha", None)
    job.pop("acceptance_record", None)
    job.pop("acceptance_artifact", None)
    job.pop("pending_attempt", None)
    job.pop("pending_attempt_kind", None)
    job.pop("next_attempt_kind", None)
    job.pop("candidate_commit_intent", None)
    stage._sync_attempts(state, job)
    semantic_attempt = pending_semantic_attempt(job, role="development")
    if semantic_attempt is None:
        raise ValueError("Git Integrity closeout is missing its Semantic Attempt")
    close_semantic_attempt(
        job,
        semantic_attempt,
        outcome="git_integrity_repair",
        result=_development_attempt_result(job),
    )
    stage.save(state)
    return True


def _development_attempt_result(job: dict[str, Any]) -> dict[str, Any]:
    summary = job.get("development_summary")
    return {"development_summary": summary} if isinstance(summary, str) else {}
