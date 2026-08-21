from __future__ import annotations

"""Publication-agent narrative and commit stage for Change Delivery."""

from pathlib import Path
from typing import Any, Callable

from agent_run.agents import HumanBlockerResult, PublicationResult
from agent_run.agent_invocation import canonical_fingerprint, invocation_event_recorder
from agent_run.artifacts import PublicationArtifact, clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.external_supervision import is_github_convergence_error
from agent_run.github import GitHubReadError
from agent_run.publication_pending import publication_pending_diagnostic

def publication(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> None:
    while True:
        if not stage._publication_is_current(state, job):
            stage._invalidate_stale(state, job, checkout)
            stage.save(state)
            return
        try:
            request = stage.adapter.publication_request(state, job, checkout)
        except GitHubReadError as error:
            if not is_github_convergence_error(error.code):
                raise
            attempts = int(job.get("publication_attempts", 0)) + 1
            job["publication_attempts"] = attempts
            job["last_publication_error"] = str(error)
            if stage.publication_budget_exhausted(attempts):
                job["phase"] = "publication_pending"
                state["status"] = "publication_pending"
                state["terminal_kind"] = "publication_pending"
                state["diagnostics"] = [
                    publication_pending_diagnostic(
                        subject_key="change_job", subject=stage.contract.label
                    )
                ]
                stage.save(state)
                return
            stage.save(state)
            continue
        job["publication_attempts"] = int(job.get("publication_attempts", 0)) + 1
        stage.save(state)
        request["_invocation_event"] = stage._invocation_events(
            state, job, request, role="publication", phase="publication"
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
        break
    clear_current_human_blocker(job)
    if not stage._publication_is_current(state, job):
        stage._invalidate_stale(state, job, checkout)
        stage.save(state)
        return
    sha = stage.publisher.create_publication_commit(
        checkout, job, publication.commit_message
    )
    job.update(
        {
            "publication": {
                "commit_message": publication.commit_message,
                "pr_title": publication.pr_title,
                "pr_body_markdown": publication.pr_body_markdown,
            },
            "publication_sha": sha,
            "phase": "publishing",
        }
    )
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
) -> Callable[..., None]:
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    boundary: dict[str, Any] = {
        "base_sha": str(job["base_sha"]),
    }
    if "candidate_sha" in job:
        boundary["candidate_sha"] = str(job["candidate_sha"])
    acceptance = job.get("acceptance_record")
    if isinstance(acceptance, dict) and "reviewed_candidate_tree" in acceptance:
        boundary["candidate_tree"] = str(acceptance["reviewed_candidate_tree"])
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
    return invocation_event_recorder(
        state,
        role=role,
        phase=phase,
        work_subject=work_subject,
        generation=generation,
        invocation_input=request,
        currentness_boundary=boundary,
        save=stage.save,
    )


def invocation_identity(state: dict[str, Any], job: dict[str, Any]) -> tuple[str, int]:
    if isinstance(job.get("ticket_number"), int):
        return (
            f"ticket:{int(job['ticket_number'])}",
            int(job.get("ticket_branch_generation", 1)),
        )
    return f"parent-only:{state['run_id']}", int(job.get("parent_generation", 1))
