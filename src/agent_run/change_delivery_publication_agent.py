"""Publication-agent narrative and commit stage for Change Delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent_run.agents import HumanBlockerResult, PublicationResult
from agent_run.agent_invocation import invocation_event_recorder
from agent_run.artifacts import PublicationArtifact, clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.delivery_policy import invocation_deadline_for_state
from agent_run.publication_operation_retry import begin_publication_operation_attempt
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    canonical_fingerprint,
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)
from agent_run.required_checks_observation import bind_new_publication_head


def _invalidate_stale_publication(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    job.pop("pending_publication_result", None)
    semantic_attempt = pending_semantic_attempt(job, role="publication")
    if semantic_attempt is not None:
        close_semantic_attempt(
            job,
            semantic_attempt,
            outcome="currentness_invalidated",
        )
        detach_active_invocation(state, semantic_attempt)
    stage._invalidate_stale(state, job, checkout)
    stage.save(state)


def publication(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    while True:
        if not stage._publication_is_current(state, job):
            _invalidate_stale_publication(stage, state, job, checkout)
            return
        pending_result = job.get("pending_publication_result")
        if isinstance(pending_result, dict):
            publication = PublicationArtifact.from_stored(
                pending_result,
                primary_ticket=(
                    int(job["ticket_number"])
                    if isinstance(job.get("ticket_number"), int)
                    else None
                ),
                delivery_run=(
                    None
                    if isinstance(job.get("ticket_number"), int)
                    else str(job["run_id"])
                ),
            )
            break
        semantic_attempt = pending_semantic_attempt(job, role="publication")
        if semantic_attempt is None:
            begin_publication_operation_attempt(job)
            ordinal = int(job.get("publication_attempts", 0)) + 1
            job["publication_attempts"] = ordinal
            work_subject, generation = stage.adapter.invocation_identity(state, job)
            semantic_attempt = allocate_semantic_attempt(
                job,
                role="publication",
                work_subject=work_subject,
                generation=generation,
                currentness_boundary=stage._invocation_boundary(job),
                ordinal=ordinal,
            )
        stage.save(state)
        request = stage.adapter.publication_request(state, job, checkout)
        request["_prompt_resources"] = state["prompt_resources"]
        request["_invocation_event"] = stage._invocation_events(
            state,
            job,
            request,
            role="publication",
            phase="publication",
            semantic_attempt=semantic_attempt,
        )
        request["_currentness_check"] = lambda: stage._publication_is_current(
            state, job
        )
        if job.get("publication_new_thread") is True:
            request["_invocation_mode"] = "new-thread"
        elif job.get("publication_failure_resume") is True:
            request["_invocation_mode"] = "resume"
        raw = stage.agents.publication(request)
        clear_initial_credential_wait(state, work_subject=stage.contract.label)
        job.pop("publication_failure_resume", None)
        if isinstance(raw, HumanBlockerResult):
            job["publication_thread_id"] = raw.thread_id
            job.pop("publication_new_thread", None)
            stage._wait_for_human(
                state,
                job,
                phase="accepted",
                blockers=raw.human_blockers,
            )
            return
        if isinstance(raw, PublicationResult):
            job.pop("publication_new_thread", None)
            if raw.replaced_thread_id is not None:
                raise ValueError(
                    "Publication Invocation cannot replace its Thread automatically"
                )
            elif raw.thread_id != job.get("development_thread_id"):
                job["publication_thread_id"] = raw.thread_id
            artifact_data = raw.artifact
            parse_publication = PublicationArtifact.parse
        else:
            artifact_data = raw
            parse_publication = (
                PublicationArtifact.parse
                if isinstance(raw, dict) and "result_kind" in raw
                else PublicationArtifact.from_stored
            )
        job.pop("publication_new_thread", None)
        if isinstance(job.get("ticket_number"), int):
            publication = parse_publication(
                artifact_data, primary_ticket=int(job["ticket_number"])
            )
        else:
            publication = parse_publication(
                artifact_data, delivery_run=str(job["run_id"])
            )
        # The Worker has finished. Persist its normalized result before the
        # next remote fence so a read outage cannot rerun the completed Agent.
        job["pending_publication_result"] = {
            "commit_message": publication.commit_message,
            "pr_title": publication.pr_title,
            "pr_body_markdown": publication.pr_body_markdown,
        }
        stage.save(state)
        break
    clear_current_human_blocker(job)
    if not stage._publication_is_current(state, job):
        _invalidate_stale_publication(stage, state, job, checkout)
        return
    semantic_attempt = pending_semantic_attempt(job, role="publication")
    if semantic_attempt is None:
        raise ValueError("Publication closeout is missing its Semantic Attempt")
    publication_result = {
        "commit_message": publication.commit_message,
        "pr_title": publication.pr_title,
        "pr_body_markdown": publication.pr_body_markdown,
    }
    sha = stage.publisher.create_publication_commit(
        checkout, job, publication.commit_message
    )
    close_semantic_attempt(
        job,
        semantic_attempt,
        outcome="publication_artifact",
        result={"publication": publication_result},
    )
    bind_new_publication_head(job, sha)
    job.update(
        {
            "publication": publication_result,
            "publication_sha": sha,
            "phase": "publishing",
        }
    )
    job.pop("pending_publication_result", None)
    job.pop("last_publication_error", None)
    stage.save(state)
    stage._reject_stale(
        state, job, checkout, "Publication was discarded after requirements changed"
    )


def invocation_events(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    request: dict[str, Any],
    *,
    role: str = "publication",
    phase: str,
    semantic_attempt: dict[str, Any],
) -> Callable[..., None]:
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    boundary = stage._invocation_boundary(job)
    invocation_deadline_seconds = invocation_deadline_for_state(state, role)
    return invocation_event_recorder(
        state,
        role=role,
        phase=phase,
        work_subject=work_subject,
        generation=generation,
        invocation_input=request,
        currentness_boundary=boundary,
        semantic_attempt=semantic_attempt,
        save=stage.save,
        invocation_deadline_seconds=invocation_deadline_seconds,
    )


def invocation_boundary(
    job: dict[str, Any], *, candidate_tree: str | None = None
) -> dict[str, Any]:
    boundary: dict[str, Any] = {
        "base_sha": str(job["base_sha"]),
    }
    if "candidate_sha" in job:
        boundary["candidate_sha"] = str(job["candidate_sha"])
    if candidate_tree is not None:
        boundary["candidate_tree"] = candidate_tree
    for key in ("effective_revision", "parent_revision", "ticket_graph_revision"):
        if key in job:
            boundary[key] = job[key]
    for key in ("default_base_sha", "repair_base_run_head_sha"):
        if key in job:
            boundary[key] = job[key]
    if "ticket_completion_records" in job:
        boundary["ticket_completion_records_fingerprint"] = canonical_fingerprint(
            job["ticket_completion_records"]
        )
    return boundary


def invocation_identity(state: dict[str, Any], job: dict[str, Any]) -> tuple[str, int]:
    if isinstance(job.get("ticket_number"), int):
        return (
            f"ticket:{int(job['ticket_number'])}",
            int(job.get("ticket_branch_generation", 1)),
        )
    return f"parent-only:{state['run_id']}", int(job.get("parent_generation", 1))
