"""Publication business transitions; callers retain their durable I/O checkpoints."""
from __future__ import annotations

from typing import Any, Literal

from agent_run.agents import HumanBlockerResult, PublicationResult
from agent_run.artifacts import (
    PublicationArtifact, clear_current_human_blocker, parse_publication_wire_result,
    record_human_blocker,
)
from agent_run.error_safety import bounded_error
from agent_run.external_supervision import wait_for_github_convergence
from agent_run.github import GitHubReadError
from agent_run.publication_operation_retry import (
    begin_publication_operation_attempt, record_publication_operation_failure,
)
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.required_checks_observation import bind_new_publication_head
from agent_run.semantic_attempt import (
    allocate_semantic_attempt, close_semantic_attempt, pending_semantic_attempt,
)


def begin_publication(
    owner: dict[str, Any], *, work_subject: str, generation: int,
    boundary: dict[str, Any],
) -> dict[str, Any]:
    """Allocate once, after preserving the previous Publication's retry facts."""
    pending = pending_semantic_attempt(owner, role="publication")
    if pending is not None:
        return pending
    begin_publication_operation_attempt(owner)
    ordinal = int(owner.get("publication_attempts", 0)) + 1
    owner["publication_attempts"] = ordinal
    return allocate_semantic_attempt(
        owner, role="publication", work_subject=work_subject, generation=generation,
        currentness_boundary=boundary, ordinal=ordinal,
    )


def retain_change_return(
    state: dict[str, Any], owner: dict[str, Any],
    raw: PublicationResult | HumanBlockerResult | dict[str, Any], *,
    primary_ticket: int | None = None, delivery_run: str | None = None,
) -> PublicationArtifact | None:
    """Retain the returned narrative before fallible currentness/Git operations."""
    owner.pop("publication_failure_resume", None)
    owner.pop("publication_new_thread", None)
    if isinstance(raw, HumanBlockerResult):
        owner["publication_thread_id"] = raw.thread_id
        record_human_blocker(
            state, owner, resume_phase="accepted", blocked_phase="blocked",
            blockers=raw.human_blockers, code="agent_requires_human",
        )
        return None
    if isinstance(raw, PublicationResult):
        if raw.replaced_thread_id is not None:
            raise ValueError("Publication Invocation cannot replace its Thread automatically")
        if raw.thread_id != owner.get("development_thread_id"):
            owner["publication_thread_id"] = raw.thread_id
        data, parse = raw.artifact, PublicationArtifact.parse
    else:
        # Only this legacy Change backend format is normalized storage. A
        # Final Run wire Thread field must not be accepted by this interface.
        data = raw
        parse = PublicationArtifact.parse if "result_kind" in raw else PublicationArtifact.from_stored
    artifact = parse(data, primary_ticket=primary_ticket, delivery_run=delivery_run)
    owner["pending_publication_result"] = _narrative(artifact)
    return artifact


def consume_run_return(
    state: dict[str, Any], owner: dict[str, Any],
    raw: dict[str, Any], *, delivery_run: str,
) -> PublicationArtifact | None:
    """Consume the current Final Run return without closing a Human Blocker."""
    wire = dict(raw)
    thread_id = wire.pop("_thread_id", None)
    if isinstance(thread_id, str):
        owner["thread_id"] = thread_id
    if wire.get("result_kind") == "human_blocker":
        normalized = parse_publication_wire_result(wire)
        record_human_blocker(
            state, owner, resume_phase="pending", blocked_phase="ready_for_human",
            blockers=tuple(normalized["human_blockers"]), code="agent_requires_human",
        )
        return None
    artifact = PublicationArtifact.parse(wire, delivery_run=delivery_run)
    clear_current_human_blocker(owner)
    return artifact


def _narrative(artifact: PublicationArtifact) -> dict[str, str]:
    return {
        "commit_message": artifact.commit_message, "pr_title": artifact.pr_title,
        "pr_body_markdown": artifact.pr_body_markdown,
    }


def complete_change_commit(
    owner: dict[str, Any], attempt: dict[str, Any], *, sha: str,
    actual_parent: str, prior_published: str, publication: PublicationArtifact,
) -> None:
    narrative = _narrative(publication)
    close_semantic_attempt(
        owner, attempt, outcome="publication_artifact", result={"publication": narrative},
    )
    bind_new_publication_head(owner, sha)
    owner.update(
        publication=narrative,
        publication_sha=sha,
        publication_parent_sha=actual_parent,
        publication_prior_published_sha=prior_published,
        phase="publishing",
    )
    for field in (
        "pending_publication_result", "publication_commit_intent", "last_publication_error",
    ):
        owner.pop(field, None)


def complete_run_publication(
    owner: dict[str, Any], attempt: dict[str, Any], artifact: PublicationArtifact,
) -> None:
    narrative = _narrative(artifact)
    owner["artifact"] = narrative
    close_semantic_attempt(
        owner, attempt, outcome="publication_artifact", result={"publication": narrative},
    )
    owner["phase"] = "pending"


def mark_publication_failure(
    state: dict[str, Any], owner: dict[str, Any], error: Exception, *,
    subject_key: Literal["change_job", "delivery_run"], subject: str,
) -> bool:
    exhausted = record_publication_operation_failure(owner, error)
    if exhausted:
        if subject_key == "change_job" and owner.get("phase") in {
            "publishing", "merging", "merged",
        }:
            owner["execution_resume_phase"] = owner["phase"]
        owner["phase"] = "publication_pending"
        state.update(
            status="publication_pending",
            terminal_kind="publication_pending",
            diagnostics=[
                publication_pending_diagnostic(subject_key=subject_key, subject=subject),
            ],
        )
    return exhausted


def wait_for_final_publication(
    state: dict[str, Any], owner: dict[str, Any], error: GitHubReadError,
) -> None:
    if mark_publication_failure(
        state, owner, error, subject_key="delivery_run", subject=str(state["run_id"]),
    ):
        return
    owner["phase"] = "waiting_external"
    wait_for_github_convergence(
        state, code=error.code, message=error.message,
        waiting_for="Final Run PR GitHub reconciliation",
    )


def hold_unknown_publication_write(
    state: dict[str, Any], owner: dict[str, Any], message: str,
) -> None:
    owner.update(
        phase="ready_for_human", blocked_reason="github_write_outcome_unknown",
        pause_resume_phase="waiting_external",
    )
    state.update(
        status="ready_for_human", terminal_kind="waiting_human",
        diagnostics=[{
            "code": "github_write_outcome_unknown", "message": bounded_error(message),
        }],
    )


def record_final_ref_failure(
    state: dict[str, Any], owner: dict[str, Any], error: Exception,
) -> None:
    if isinstance(error, OSError):
        if mark_publication_failure(
            state, owner, error, subject_key="delivery_run", subject=str(state["run_id"]),
        ):
            return
        owner["phase"] = "waiting_external"
        state.update(
            status="waiting_external", terminal_kind="waiting_external",
            diagnostics=[{
                "code": "final_run_ref_readback_pending", "message": "正在确认 Final Run ref 写入结果",
            }],
        )
    else:
        owner.update(
            phase="ready_for_human", blocked_reason="final_run_ref_authority_failed",
            pause_resume_phase="waiting_external",
        )
        state.update(
            status="ready_for_human", terminal_kind="waiting_human",
            diagnostics=[{
                "code": "final_run_ref_authority_failed", "message": bounded_error(str(error)),
            }],
        )


def record_final_publication_failure(
    state: dict[str, Any], owner: dict[str, Any], error: Exception,
) -> bool:
    if mark_publication_failure(
        state, owner, error, subject_key="delivery_run", subject=str(state["run_id"]),
    ):
        return True
    owner.pop("write_intent", None)
    owner.update(last_publication_error=str(error), phase="pending")
    return False
