"""Publication-agent narrative and commit stage for Change Delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from agent_run.agents import HumanBlockerResult, PublicationResult
from agent_run.agent_execution import execute_agent_work
from agent_run.artifacts import PublicationArtifact, clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.publication_lifecycle import (
    begin_publication,
    complete_change_commit,
    retain_change_return,
)
from agent_run.publication_evidence import publication_is_current
from agent_run.git_publication_commit import observe_publication_commit
from agent_run.git import GitError
from agent_run.semantic_attempt import (
    canonical_fingerprint,
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)


def _invalidate_stale_publication(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
    *, delivery_is_stale: bool = True,
) -> None:
    intent = job.get("publication_commit_intent")
    semantic_attempt = pending_semantic_attempt(job, role="publication")
    if isinstance(intent, dict):
        try:
            present = observe_publication_commit(stage.git, checkout, intent)
        except (GitError, OSError, TimeoutError) as error:
            stage._block(state, job, "publication_intent_mismatch", str(error))
            stage.save(state)
            return
        if semantic_attempt is None:
            raise ValueError("Publication commit intent is missing its Semantic Attempt")
        semantic_attempt["publication_commit_observation"] = {
            "expected_sha": intent["expected_sha"],
            "result": "present" if present else "absent",
        }
        job.pop("publication_commit_intent")
    original_result = job.pop("pending_publication_result", None)
    if semantic_attempt is not None:
        close_semantic_attempt(
            job,
            semantic_attempt,
            outcome="currentness_invalidated",
            result={"publication": original_result},
        )
        detach_active_invocation(state, semantic_attempt)
    if delivery_is_stale:
        stage._invalidate_stale(state, job, checkout)
    else:
        # Only the narrative's input/result binding changed. Keep the valid
        # candidate, Review, CI purpose/evidence and published PR authority.
        job.pop("publication", None)
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
            if not publication_is_current(
                job, pending_result, stage._invocation_boundary(job)
            ):
                _invalidate_stale_publication(
                    stage, state, job, checkout, delivery_is_stale=False,
                )
                return
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
        stage.require_agent_work("publication")
        semantic_attempt = pending_semantic_attempt(job, role="publication")
        if semantic_attempt is None:
            work_subject, generation = stage.adapter.invocation_identity(state, job)
            semantic_attempt = begin_publication(
                job,
                work_subject=work_subject,
                generation=generation,
                boundary=stage._invocation_boundary(job),
            )
        stage.save(state)
        request = stage.adapter.publication_request(state, job, checkout)
        try:
            raw = cast(
                PublicationResult | HumanBlockerResult | dict[str, Any],
                execute_agent_work(
                    state, job, request, semantic_attempt,
                    agents=stage.agents, save=stage.save, phase="publication",
                    boundary=stage._invocation_boundary(job),
                    is_current=lambda: stage._publication_is_current(state, job),
                ),
            )
        finally:
            stage.release_agent_work()
        clear_initial_credential_wait(state, work_subject=stage.contract.label)
        returned_publication = retain_change_return(
            state, job, raw,
            primary_ticket=(int(job["ticket_number"]) if isinstance(job.get("ticket_number"), int) else None),
            delivery_run=(None if isinstance(job.get("ticket_number"), int) else str(job["run_id"])),
        )
        if returned_publication is None:
            stage.save(state)
            return
        publication = returned_publication
        # The Worker has finished. Persist its normalized result before the
        # next remote fence so a read outage cannot rerun the completed Agent.
        stage.save(state)
        break
    clear_current_human_blocker(job)
    if not stage._publication_is_current(state, job):
        _invalidate_stale_publication(stage, state, job, checkout)
        return
    if not publication_is_current(
        job, job.get("pending_publication_result"), stage._invocation_boundary(job)
    ):
        _invalidate_stale_publication(
            stage, state, job, checkout, delivery_is_stale=False,
        )
        return
    semantic_attempt = pending_semantic_attempt(job, role="publication")
    if semantic_attempt is None:
        raise ValueError("Publication closeout is missing its Semantic Attempt")
    publication_parent = str(job.get("published_sha", job["base_sha"]))
    if "publication_commit_intent" in job and not isinstance(
        job["publication_commit_intent"], dict
    ):
        raise ValueError("Publication commit intent must be an object")
    def save_commit_intent() -> None:
        stage.save(state)

    sha = stage.publisher.create_publication_commit(
        checkout, job, publication.commit_message,
        save_intent=save_commit_intent,
    )
    actual_parent = stage.git.commit_parents(sha)[0]
    complete_change_commit(
        job, semantic_attempt,
        sha=sha, actual_parent=actual_parent,
        prior_published=publication_parent, publication=publication,
    )
    stage.save(state)
    stage._reject_stale(
        state, job, checkout, "Publication was discarded after requirements changed"
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
    if "ticket_integration" in job:
        boundary["ticket_integration_fingerprint"] = canonical_fingerprint(
            job["ticket_integration"]
        )
    if "parent_integration" in job:
        boundary["parent_integration_fingerprint"] = canonical_fingerprint(job["parent_integration"])
    return boundary


def invocation_identity(state: dict[str, Any], job: dict[str, Any]) -> tuple[str, int]:
    if isinstance(job.get("ticket_number"), int):
        return (
            f"ticket:{int(job['ticket_number'])}",
            int(job.get("ticket_branch_generation", 1)),
        )
    return f"parent-only:{state['run_id']}", int(job.get("parent_generation", 1))
