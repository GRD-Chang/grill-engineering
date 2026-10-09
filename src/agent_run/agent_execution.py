"""Shared Agent session, Invocation binding and durable return boundary.

Stages own semantic work allocation, currentness fences and business successors.
The backend retains bounded invocation recovery and output-format repair.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from agent_run.agent_invocation import invocation_event_recorder
from agent_run.agent_return import AgentReturn, restore_agent_return, store_agent_return
from agent_run.agents import (
    AgentBackend, DevelopmentResult, HumanBlockerResult, ReviewResult, PublicationResult,
)
from agent_run.change_delivery_threads import (
    latest_reviewer_thread, record_development_thread, record_reviewer, require_development_thread,
)
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.delivery_policy import invocation_deadline_for_state, policy_snapshot_for_state
from agent_run.change_delivery_state import require_string_list
from agent_run.semantic_attempt import canonical_fingerprint


def select_development_thread(
    state: dict[str, Any], job: dict[str, Any], *, new_attempt: bool
) -> None:
    """Rotate only at allocation of semantic work, before the durable save."""

    policy = policy_snapshot_for_state(state).get("development_thread_policy", "reuse")
    if not new_attempt or policy != "new-per-attempt":
        return
    current = job.get("development_thread_id")
    if isinstance(current, str) and current:
        history = require_string_list(job, "development_thread_history")
        if current not in history:
            history.append(current)
        job["development_thread_history"] = history
    job["development_thread_id"] = None
    # The new Attempt has no failed invocation to resume. Manual replacement
    # remains a separate same-Attempt operation handled by the Controller.
    job.pop("development_failure_resume", None)


def execute_agent_work(
    state: dict[str, Any], subject: dict[str, Any], request: dict[str, Any],
    semantic_attempt: dict[str, Any], *, agents: AgentBackend,
    save: Callable[[dict[str, Any]], object], phase: str,
    boundary: dict[str, Any], is_current: Callable[[], bool],
) -> AgentReturn:
    pending = subject.get("pending_semantic_attempt")
    if pending is not semantic_attempt or semantic_attempt.get("status") != "pending":
        raise ValueError("Agent execution does not own the pending Semantic Attempt")
    if canonical_fingerprint(boundary) != semantic_attempt["currentness_boundary_fingerprint"]:
        raise ValueError("Agent execution currentness boundary is stale")
    if "returned_result" in semantic_attempt:
        return restore_agent_return(semantic_attempt["returned_result"], semantic_attempt)
    role = str(semantic_attempt["role"])
    work_subject = str(semantic_attempt["work_subject"])
    run_scope = work_subject.startswith(("run-acceptance:", "run-publication:"))
    if role == "reviewer":
        invocation_role = "reviewer" if run_scope else "fresh_acceptance"
        prefix = "reviewer" if run_scope else "review"
    elif role == "publication":
        invocation_role = "final_publication" if run_scope else "publication"
        prefix = "publication"
    elif role == "development":
        invocation_role = prefix = "development"
    else:
        raise ValueError(f"Unknown Semantic Attempt role: {role}")
    request["_prompt_resources"] = state["prompt_resources"]
    if subject.get(f"{prefix}_new_thread") is True:
        request["thread_id"] = None
        request["_invocation_mode"] = "new-thread"
    elif subject.get(f"{prefix}_failure_resume") is True:
        request["_invocation_mode"] = "resume"
    request["_currentness_check"] = is_current
    request["_invocation_event"] = invocation_event_recorder(
        state, role=invocation_role, phase=phase, work_subject=work_subject,
        generation=int(semantic_attempt["generation"]), invocation_input=request,
        currentness_boundary=boundary, semantic_attempt=semantic_attempt, save=save,
        invocation_deadline_seconds=invocation_deadline_for_state(state, invocation_role),
    )
    save(state)
    if role == "development":
        result: AgentReturn = agents.develop(request)
    elif role == "reviewer":
        result = agents.review(request)
    elif role == "publication":
        result = agents.run_publication(request) if run_scope else agents.publication(request)
    else:
        raise ValueError(f"Unknown Semantic Attempt role: {role}")
    _validate_thread(state, subject, request, result, work_subject)
    try:
        semantic_attempt["returned_result"] = store_agent_return(result, semantic_attempt)
    except ValueError:
        if isinstance(result, ReviewResult) and not run_scope:
            # Preserve the existing Change contract: a malformed Artifact
            # cannot make its Reviewer identity available as a fresh Thread.
            record_reviewer(subject, result.thread_id, new_thread=subject.get("review_new_thread") is True)
            for key in ("review_new_thread", "review_failure_resume", "review_resume_thread_id", "review_human_blocker_resume"):
                subject.pop(key, None)
            save(state)
        raise
    # This boundary precedes remote observations, Git writes and role closeout.
    save(state)
    return restore_agent_return(semantic_attempt["returned_result"], semantic_attempt)


def _validate_thread(
    state: dict[str, Any], subject: dict[str, Any], request: dict[str, Any],
    result: AgentReturn, work_subject: str,
) -> None:
    # Validate without advancing role bookkeeping: only the stage consumes the
    # result. An invalid role identity must never become a reusable return.
    development = isinstance(result, DevelopmentResult) or (
        isinstance(result, HumanBlockerResult)
        and subject["pending_semantic_attempt"]["role"] == "development"
    )
    if development:
        assert isinstance(result, (DevelopmentResult, HumanBlockerResult))
        require_development_thread(subject, result.thread_id, requested_thread=request.get("thread_id"))
        if work_subject.startswith("run-repair:") and result.thread_id in prior_thread_identities(state, state["run_acceptance"]):
            raise ValueError("Change Job Development Thread is not independent")
        record_development_thread(_session_facts(subject), result.thread_id, result.replaced_thread_id)
    elif isinstance(result, ReviewResult):
        if work_subject.startswith("run-acceptance:"):
            resumed_thread = subject.get("reviewer_resume_thread_id")
            resumed = not subject.get("reviewer_new_thread") and (
                bool(subject.get("prior_human_blockers")) or isinstance(resumed_thread, str)
            )
            expected = resumed_thread if isinstance(resumed_thread, str) else latest_reviewer_thread(subject)
            if resumed and result.thread_id != expected:
                raise ValueError("Human Blocker resume requires the latest Reviewer Thread")
            if not result.thread_id.strip() or (result.thread_id in prior_thread_identities(state, subject) and not resumed):
                raise ValueError("Run Acceptance requires a new Reviewer Thread")
        else:
            record_reviewer(_session_facts(subject), result.thread_id, new_thread=subject.get("review_new_thread") is True)
    elif isinstance(result, PublicationResult) and result.replaced_thread_id is not None:
        raise ValueError("Publication Invocation cannot replace its Thread automatically")


def _session_facts(subject: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(subject[key])
        for key in (
            "development_thread_id", "development_thread_history", "reviewer_thread_ids",
            "review_human_blocker_resume", "review_failure_resume", "review_resume_thread_id",
        )
        if key in subject
    }
