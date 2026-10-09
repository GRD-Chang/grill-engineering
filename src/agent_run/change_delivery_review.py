from __future__ import annotations

"""Fresh Candidate acceptance and Human Blocker stage for Change Delivery."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_run.delivery_budget import delivery_budget_owner

from agent_run.artifacts import (
    AcceptanceArtifact,
    record_human_blocker,
)
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.development_review import after_review, ReviewDecision
from agent_run.review_attempt import (
    begin_review, consume_review_result, finish_review_result, remember_review,
    review_ordinal,
)
from agent_run.ci_recovery_budget import (
    ci_limit_for_job,
    ci_reviewer_budget_context,
)
from agent_run.change_delivery_state import require_mapping as _mapping
from agent_run.change_delivery_threads import record_reviewer as _record_reviewer
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.review_budget import reviewer_budget_context
from agent_run.review_context import (
    prepare_review_context,
    review_context_for_result,
    review_record_identity,
)
from agent_run.semantic_attempt import pending_semantic_attempt


def review(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    # A new Reviewer always starts from the current Candidate boundary.
    # Any result retained for a prior base/candidate must not survive into
    # an interrupted fresh attempt.
    job.pop("pending_review_result", None)
    attempt = review_ordinal(job)
    job["phase"] = "reviewing"
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    owner = delivery_budget_owner(state, job)
    budget = owner.get("review_budget")
    if not isinstance(budget, dict) or type(budget.get("window")) is not int:
        raise ValueError("Reviewer is missing its Review Budget Window")
    semantic_attempt = begin_review(
        job,
        work_subject=work_subject,
        generation=generation,
        boundary=stage._invocation_boundary(job),
        budget_owner=owner,
        budget_window=int(
            delivery_budget_owner(state, job)["ci_recovery"]["window"]
            if job.get("attempt_kind") == "ci_repair"
            else budget["window"]
        ),
    )
    stage._sync_attempts(state, job)
    stage.save(state)
    validation = checkout.parent / f"validation-{stage.contract.label}-{attempt}"
    try:
        stage.publisher.prepare_validation(checkout, job, validation)
        request = stage.adapter.review_request(state, job, validation)
        if job.get("attempt_kind") == "ci_repair":
            recovery = delivery_budget_owner(state, job).get("ci_recovery")
            records = recovery.get("review_artifacts") if isinstance(recovery, dict) else None
            if isinstance(records, list) and records:
                previous = records[-1]
                if isinstance(previous, dict):
                    request["previous_acceptance_artifact"] = previous.get("artifact")
                    request["previous_review_identity"] = previous.get("review_identity")
        if job.get("attempt_kind") == "ci_repair":
            request["ci_evidence"] = deepcopy(job.get("ci_evidence"))
            request["ci_failure_context"] = deepcopy(job.get("required_checks_origin"))
            request["attempt_kind"] = "ci_repair"
            request["work_source"] = "ci"
        request["_prompt_resources"] = state["prompt_resources"]
        if job.get("parent_integration_stage") is True and job.get("attempt_kind") != "ci_repair":
            request.pop("review_budget_context", None)
        elif job.get("attempt_kind") == "ci_repair":
            limit = ci_limit_for_job(job)
            request["review_budget_context"] = ci_reviewer_budget_context(
                owner,
                limit=limit,
                is_ticket=stage.contract.label.startswith("ticket-"),
            )
        else:
            request["review_budget_context"] = reviewer_budget_context(
                owner,
                stage.review_budget_policy(),
                current_attempt_consumed=semantic_attempt.get("budget_consumed") is True,
            )
        prepare_review_context(request, semantic_attempt, job, stage.git, state)
        stage.save(state)
        request["_invocation_event"] = stage._invocation_events(
            state,
            job,
            request,
            role="fresh_acceptance",
            phase="reviewing",
            semantic_attempt=semantic_attempt,
        )
        request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
        if job.get("review_new_thread") is True:
            request["_invocation_mode"] = "new-thread"
        elif job.get("review_failure_resume") is True:
            request["_invocation_mode"] = "resume"
        try:
            review = stage.agents.review(request)
        finally:
            stage.release_agent_work()
    finally:
        stage.git.remove_worktree(validation)
    clear_initial_credential_wait(state, work_subject=stage.contract.label)
    _record_reviewer(
        job, review.thread_id, new_thread=job.get("review_new_thread") is True
    )
    human_blocker_resume = job.get("review_human_blocker_resume") is True
    job.pop("review_new_thread", None)
    job.pop("review_failure_resume", None)
    job.pop("review_resume_thread_id", None)
    job.pop("review_human_blocker_resume", None)
    try:
        artifact = consume_review_result(job, stage.review_budget_policy(), review.artifact,
            budget_owner=owner)
    except ValueError:
        # A malformed Artifact has no resumable result, but its Reviewer
        # identity must remain durable before a fresh output attempt.
        stage.save(state)
        raise
    job["pending_review_result"] = {
        "reviewer_thread_id": review.thread_id,
        "artifact": artifact.raw,
        "human_blocker_resume": human_blocker_resume,
    }
    # Persist the returned result and Reviewer identity atomically.  This
    # keeps the successful lifecycle's existing save cadence while making
    # a later currentness observation retryable without another Reviewer.
    stage.save(state)
    complete_review(stage, state, job, checkout)


def complete_review(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    pending = _mapping(job, "pending_review_result")
    reviewer_thread_id = pending.get("reviewer_thread_id")
    if not isinstance(reviewer_thread_id, str) or not reviewer_thread_id.strip():
        raise ValueError("pending review result is missing its Reviewer Thread")
    artifact = AcceptanceArtifact.parse(pending.get("artifact"))
    if pending_semantic_attempt(job, role="reviewer") is None:
        raise ValueError("Reviewer closeout is missing its Semantic Attempt")
    stage._reject_stale(
        state,
        job,
        checkout,
        "Fresh Acceptance was discarded after requirements changed",
    )
    job["acceptance_artifact"] = artifact.raw
    previous_repair_source = job.get("repair_source")
    # A new valid Acceptance Artifact supersedes any earlier Required-Checks
    # repair input when it is itself an Acceptance Repair.  A passing review
    # after CI repair still needs the exact CI failure as Publication context;
    # Ticket CI findings retain that original failure for the next CI Worker.
    ticket_ci = job.get("attempt_kind") == "ci_repair"
    if not artifact.is_accepted and not ticket_ci and stage.contract.label.startswith("ticket-"):
        job.pop("ci_evidence", None)
        job.pop("final_ci_fix_failure_head", None)
    elif not ticket_ci and previous_repair_source != "required_checks" and stage.contract.label.startswith("ticket-"):
        job.pop("ci_evidence", None)
        job.pop("final_ci_fix_failure_head", None)
    clear_required_checks_observation(job)
    budget = (
        delivery_budget_owner(state, job).get("ci_recovery")
        if job.get("attempt_kind") == "ci_repair"
        else delivery_budget_owner(state, job).get("review_budget")
    )
    review_facts: dict[str, Any] = {}
    if isinstance(budget, dict):
        context = review_context_for_result(job)
        candidate_sha = str(context["binding"]["candidate_sha"])
        reviewed_base_sha = str(context["binding"]["base_sha"])
        review_identity = review_record_identity(context)
        review_record = {
            "reviewer_thread_id": reviewer_thread_id,
            "candidate_sha": candidate_sha,
            "reviewed_base_sha": reviewed_base_sha,
            "review_identity": review_identity,
            "artifact": artifact.raw,
        }
        review_facts = {
            key: deepcopy(review_record[key])
            for key in ("reviewer_thread_id", "candidate_sha", "review_identity")
        }
        if job.get("parent_integration_stage") is not True or job.get("attempt_kind") == "ci_repair":
            remember_review(
                budget, review_record, limit=stage.review_budget_policy().review_limit,
                human_blocker_resume=pending.get("human_blocker_resume") is True,
            )
    context = review_context_for_result(job)
    job["last_review_candidate_sha"] = str(context["binding"]["candidate_sha"])
    prior_acceptance = job.get("acceptance_record")
    if isinstance(prior_acceptance, dict) and AcceptanceArtifact.parse(prior_acceptance.get("artifact")).is_accepted:
        job["previous_parent_acceptance_record"] = deepcopy(prior_acceptance)
    job["acceptance_record"] = stage.adapter.acceptance_record(
        state, job, reviewer_thread_id, artifact.raw, review_context=context,
    )
    if artifact.is_accepted and job.get("attempt_kind") == "ci_repair":
        job["publication_authority"] = "accepted"
        job.pop("fallback_publication_receipt", None)
        job.pop("ci_repair_publication_receipt", None)
    if not artifact.is_accepted:
        job["repair_source"] = "acceptance"
        if job.get("parent_integration_stage") is True and job.get("attempt_kind") != "ci_repair":
            job["next_attempt_kind"] = "integration"
    # A first rejection has no PR to expose.  For a repair after a PR
    # already exists, update that PR's one status comment immediately so
    # it cannot keep advertising an obsolete passing Candidate.
    existing_pr = job.get("pr_number")
    if not artifact.requires_human and isinstance(existing_pr, int):
        stage._record_agent_run_status(
            existing_pr,
            job,
            "not_checked",
            next_action=(
                "repair Fresh Validation findings"
                if artifact.has_failures
                else "generate publication narrative"
            ),
        )
    finish_review_result(
        state, job, artifact, resume_phase="candidate", blocked_phase="blocked",
        history_facts=review_facts,
    )
    if artifact.requires_human:
        stage.save(state)
        return
    decision = after_review(job, stage.review_budget_policy(), artifact,
        budget_owner=delivery_budget_owner(state, job))
    if decision == ReviewDecision.ACCEPT:
        job["phase"] = "accepted"
    elif decision == ReviewDecision.REVIEW_EXHAUSTED:
        stage._checkpoint_budget(
            state, job, decision.value,
            "Review budget is exhausted and the current Candidate still needs modification",
        )
    elif decision in {
        ReviewDecision.CI_EXHAUSTED,
        ReviewDecision.DEVELOPMENT_EXHAUSTED,
    }:
        job.update({"phase": "escalating", "escalation_code": decision.value})
    else:
        job["phase"] = "repairing"
        if job.get("attempt_kind") == "ci_repair":
            job["next_attempt_kind"] = "ci_repair"
    stage.save(state)


def wait_for_human(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    *,
    phase: str,
    blockers: tuple[str, ...],
    code: str = "agent_requires_human",
) -> None:
    record_human_blocker(
        state, job, resume_phase=phase, blocked_phase="blocked", blockers=blockers,
        code=code,
    )
    stage.save(state)
