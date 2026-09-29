"""Freeze the authority of one manual Resume before an Executor applies it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_run.operator_gate import operator_gate_subjects
from agent_run.review_budget import budget_checkpoint_subjects, review_budget_owner
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.task_control import TaskControlError


class ResumeIntentError(TaskControlError):
    """The admitted Resume no longer grants authority over this pause."""


def resume_pause_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Recover unconsumed pause facts behind a temporary GitHub refresh wait."""
    current = dict(state)
    if (
        current.get("status") == "waiting_external"
        and current.get("github_refresh_pending") is True
    ):
        if isinstance(current.get("operator_stop"), Mapping):
            current["status"] = "operator_stopped"
        elif isinstance(current.get("supervision_wait"), Mapping):
            current["status"] = "supervision_timeout"
    return current


def bind_resume_intent(
    state: Mapping[str, Any], *, ticket_number: int | None = None
) -> dict[str, Any]:
    """Bind pause facts, excluding observation timestamps and refresh metadata."""
    state = current = resume_pause_state(state)
    checkpoints = budget_checkpoint_subjects(current)
    gates = operator_gate_subjects(current)
    if ticket_number is not None:
        if type(ticket_number) is not int or ticket_number < 1:
            raise ResumeIntentError("resume Ticket 编号必须为正整数")
        gates = [
            (location, subject) for location, subject in gates
            if location == f"ticket:{ticket_number}"
        ]
        checkpoints = [
            (location, subject) for location, subject in checkpoints
            if location == "ticket" and subject.get("ticket_number") == ticket_number
        ]
        if not gates and not checkpoints:
            raise ResumeIntentError("指定 Ticket 没有可恢复的局部门禁")
    resync = [
        (location, subject)
        for location, subject in gates
        if subject.get("blocked_reason") == "integration_resync_exhausted"
    ]
    ci_recovery = [
        (location, subject)
        for location, subject in gates
        if subject.get("blocked_reason") in {
            "ci_inner_budget_exhausted", "ci_outer_budget_exhausted"
        }
    ]
    human = [
        (location, subject)
        for location, subject in gates
        if subject.get("blocked_reason")
        in {"agent_requires_human", "reviewer_requires_human"}
    ]
    publication_retry = [
        (location, subject) for location, subject in gates
        if location.startswith("ticket:")
        and subject.get("phase") == "publication_pending"
    ]
    ticket_failure = [
        (location, subject) for location, subject in gates
        if location.startswith("ticket:")
        and subject.get("blocked_reason") in {"ticket_execution_failed", "ticket_supervision_timeout"}
    ]
    ticket_pause = [
        (location, subject) for location, subject in gates
        if location.startswith("ticket:")
        and subject.get("blocked_reason") == "ticket_paused"
    ]
    if ticket_number is not None and not any(
        (checkpoints, human, resync, ci_recovery, publication_retry,
         ticket_failure, ticket_pause)
    ):
        raise ResumeIntentError("指定 Ticket 没有受支持的局部恢复边界")
    if (
        len(checkpoints) > 1 or len(human) > 1 or len(resync) > 1
        or len(ci_recovery) > 1 or len(publication_retry) > 1
        or len(ticket_failure) > 1
        or len(ticket_pause) > 1
        or sum(bool(group) for group in (
            checkpoints, human, resync, ci_recovery, publication_retry,
            ticket_failure, ticket_pause,
        )) > 1
    ):
        raise ResumeIntentError("resume 暂停对象不唯一；拒绝推断恢复授权")
    if checkpoints:
        reason, authorization = "budget_checkpoint", "new_budget_window"
        subjects = checkpoints
    elif human:
        reason, authorization = "human_blocker", "human_response"
        subjects = human
    elif resync:
        reason, authorization = "integration_resync", "renew_integration_resync"
        subjects = resync
    elif ci_recovery:
        reason, authorization = "ci_recovery", "renew_ci_recovery"
        subjects = ci_recovery
    elif publication_retry:
        reason, authorization = "publication_operation_retry", "renew_publication_operation_retry"
        subjects = publication_retry
    elif ticket_failure:
        reason, authorization = str(ticket_failure[0][1]["blocked_reason"]), "continue_ticket"
        subjects = ticket_failure
    elif ticket_pause:
        reason, authorization = "ticket_paused", "continue_ticket"
        subjects = ticket_pause
    else:
        reason = str(state.get("status"))
        parent_job = state.get("parent_job")
        if reason == "parent_delivery_pending" and not (
            isinstance(parent_job, Mapping) and parent_job.get("phase") == "merging"
        ):
            reason = "execution_interrupted"
        if reason not in {
            "operator_stopped", "supervision_timeout", "parent_closeout_pending",
            "parent_delivery_pending",
        }:
            reason = "execution_interrupted"
        authorization = "continue_existing_work"
        # Refresh may clear the synthetic execution-failure gate while retaining
        # the exact interrupted Invocation. Its Agent identity is the target.
        subjects = []
    invocation = state.get("active_agent_invocation")
    invocation = invocation if isinstance(invocation, Mapping) else {}
    if (
        ticket_number is not None
        and invocation.get("work_subject") != f"ticket:{ticket_number}"
    ):
        invocation = {}
    target = {
        "run_id": state.get("run_id"),
        "invocation": {
            key: invocation.get(key)
            for key in (
                "binding_id", "role", "requested_thread_id", "reported_thread_id",
                "started_at", "semantic_attempt", "currentness_boundary",
                "thread_execution_binding", "output_attempt", "validation_error",
            )
        },
        "subjects": [
            {
                "location": location,
                "review_budget": review_budget_owner(subject).get("review_budget"),
                **{
                    key: subject.get(key)
                    for key in (
                        "ticket_number", "generation", "acceptance_generation",
                        "candidate_sha", "phase", "blocked_reason", "human_blockers",
                        "pending_semantic_attempt", "integration_resync",
                        "ci_recovery", "publication_operation_retry",
                    )
                },
            }
            for location, subject in subjects
        ],
        "operator_stop": state.get("operator_stop"),
        "parent_revision": state.get("accepted_parent_spec_revision"),
        "ticket_graph_revision": state.get("accepted_ticket_graph_revision"),
        "supervision_wait": state.get("supervision_wait")
        if reason == "supervision_timeout" else None,
    }
    intent: dict[str, Any] = {
        "pause_reason": reason,
        "authorization": authorization,
        "target_digest": canonical_fingerprint(target),
    }
    if ticket_number is not None:
        intent["ticket_number"] = ticket_number
    return intent


def validate_resume_intent(
    state: Mapping[str, Any], intent: object,
) -> None:
    """Never turn an accepted continuation into a different business grant."""
    ticket_number = intent.get("ticket_number") if isinstance(intent, Mapping) else None
    if not isinstance(intent, Mapping) or dict(intent) != bind_resume_intent(
        state, ticket_number=ticket_number
    ):
        raise ResumeIntentError("resume 暂停对象或授权已变化；原 Action 不能重新解释，请检查状态后重新提交")


def action_resume_intent(action: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = action.get("payload")
    intent = payload.get("resume_intent") if isinstance(payload, Mapping) else None
    return dict(intent) if isinstance(intent, Mapping) else None
