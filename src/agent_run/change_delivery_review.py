from __future__ import annotations

"""Fresh Candidate acceptance and Human Blocker stage for Change Delivery."""

from pathlib import Path
from typing import Any

from agent_run.artifacts import (
    AcceptanceArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
)
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_state import require_mapping as _mapping
from agent_run.change_delivery_threads import record_reviewer as _record_reviewer
from agent_run.credential_availability import clear_initial_credential_wait


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
    attempt = int(job.get("validation_attempts", 0)) + 1
    job["validation_attempts"] = attempt
    stage._sync_attempts(state, job)
    job["phase"] = "reviewing"
    stage.save(state)
    validation = checkout.parent / f"validation-{stage.contract.label}-{attempt}"
    try:
        stage.publisher.prepare_validation(checkout, job, validation)
        request = stage.adapter.review_request(state, job, validation)
        request["_invocation_event"] = stage._invocation_events(
            state, job, request, role="fresh_acceptance", phase="reviewing"
        )
        request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
        if job.get("review_new_thread") is True:
            request["_invocation_mode"] = "new-thread"
        elif job.get("review_failure_resume") is True:
            request["_invocation_mode"] = "resume"
        review = stage.agents.review(request)
    finally:
        stage.git.remove_worktree(validation)
    clear_initial_credential_wait(state, work_subject=stage.contract.label)
    _record_reviewer(
        job, review.thread_id, new_thread=job.get("review_new_thread") is True
    )
    job.pop("review_new_thread", None)
    job.pop("review_failure_resume", None)
    job.pop("review_resume_thread_id", None)
    job.pop("review_human_blocker_resume", None)
    try:
        artifact = AcceptanceArtifact.parse(review.artifact)
    except ValueError:
        # A malformed Artifact has no resumable result, but its Reviewer
        # identity must remain durable before a fresh output attempt.
        stage.save(state)
        raise
    stage.mark_review_invocation(job)
    job["pending_review_result"] = {
        "reviewer_thread_id": review.thread_id,
        "artifact": artifact.raw,
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
    # a later failed Acceptance Repair must clear it before its next Worker.
    if not artifact.is_accepted:
        job.pop("ci_evidence", None)
        job.pop("final_ci_fix_failure_head", None)
        job.pop("required_checks_evidence", None)
    elif previous_repair_source != "required_checks":
        job.pop("ci_evidence", None)
        job.pop("final_ci_fix_failure_head", None)
        job.pop("required_checks_evidence", None)
    budget = job.get("review_budget")
    if isinstance(budget, dict):
        candidate_sha = str(job["candidate_sha"])
        if isinstance(job.get("default_base_sha"), str):
            review_identity = {
                "run_base_sha": str(job["base_sha"]),
                "repair_candidate_sha": candidate_sha,
                "expected_merge_tree": stage.git.expected_merge_tree(
                    default_head_sha=str(job["default_base_sha"]),
                    run_head_sha=candidate_sha,
                ),
            }
        else:
            review_identity = {
                "reviewed_base_sha": str(job["base_sha"]),
                "reviewed_candidate_sha": candidate_sha,
                "reviewed_candidate_tree": stage.git.resolve(
                    f"{candidate_sha}^{{tree}}"
                ),
            }
        budget["review_artifacts"].append(
            {
                "reviewer_thread_id": reviewer_thread_id,
                "candidate_sha": candidate_sha,
                "reviewed_base_sha": str(job["base_sha"]),
                "review_identity": review_identity,
                "artifact": artifact.raw,
            }
        )
        del budget["review_artifacts"][:-5]
    job["last_review_candidate_sha"] = str(job["candidate_sha"])
    job["acceptance_record"] = stage.adapter.acceptance_record(
        state, job, reviewer_thread_id, artifact.raw
    )
    if not artifact.is_accepted:
        job["repair_source"] = "acceptance"
    job.pop("pending_review_result", None)
    # A first rejection has no PR to expose.  For a repair after a PR
    # already exists, update that PR's one status comment immediately so
    # it cannot keep advertising an obsolete passing Candidate.
    existing_pr = job.get("pr_number")
    if artifact.requires_human:
        stage._wait_for_human(
            state,
            job,
            phase="candidate",
            blockers=artifact.blocker_evidence,
            code="reviewer_requires_human",
        )
        return
    clear_current_human_blocker(job)
    if isinstance(existing_pr, int):
        stage._record_agent_run_status(
            existing_pr,
            job,
            "not_checked",
            next_action=(
                "repair Fresh Validation findings"
                if artifact.has_failures
                else (
                    "await human decision"
                    if artifact.requires_human
                    else "generate publication narrative"
                )
            ),
        )
    if artifact.is_accepted:
        job["phase"] = "accepted"
    elif (
        not stage.review_budget_policy().fallback
        and stage.review_budget_exhausted_for_review(job)
    ):
        stage._checkpoint_budget(
            state,
            job,
            "review_budget_exhausted",
            "Review budget is exhausted and the current Candidate still needs modification",
        )
    elif stage.modification_budget_exhausted(job):
        job["phase"] = "escalating"
        job["escalation_code"] = (
            "reviewer_requires_human"
            if artifact.requires_human
            else "modification_budget_exhausted"
        )
    else:
        job["phase"] = "repairing"
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
    append_human_blocker_history(job, phase=phase, blockers=blockers)
    job.update(
        {
            "human_blockers": list(blockers),
            "human_blocker_phase": phase,
            "phase": "blocked",
            "blocked_reason": code,
        }
    )
    state.update(
        {
            "status": "ready_for_human",
            "terminal_kind": "waiting_human",
            "diagnostics": [
                {
                    "code": code,
                    "message": blocker,
                }
                for blocker in blockers
            ],
        }
    )
    stage.save(state)
