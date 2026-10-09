"""Bounded audit facts for explicit maintainer Resume authorization."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agent_run.external_supervision import is_github_refresh_wait
from agent_run.operator_gate import has_non_invocation_execution_failure, operator_gate_subjects
from agent_run.review_budget import budget_checkpoint_subjects
from agent_run.resume_audit_contract import (
    RESUME_AUDIT_KINDS,
    resume_event_digest,
    resume_history_digest,
    resume_identity,
)
from agent_run.semantic_attempt import (
    invocation_attempt_is_pending,
)
from agent_run.state_contract import human_blocker_subject_count


def append_explicit_resume_audit(
    state: dict[str, Any],
    *,
    new_thread: bool,
    human_response_supplied: bool,
    ticket_number: int | None = None,
) -> dict[str, Any]:
    """Record one public ``resume`` without creating a Resume budget."""
    if ticket_number is None and state.get("status") in {"blocked", "progress_exhausted"}:
        gates = operator_gate_subjects(state)
        if len(gates) == 1:
            location, subject = gates[0]
            if location.startswith("ticket:") and subject.get("blocked_reason") in {
                "ticket_execution_failed", "ticket_supervision_timeout", "ticket_paused",
                "ci_investigation_paused",
            }:
                ticket_number = int(subject["ticket_number"])

    audit = state.setdefault(
        "resume_audit",
        {
            "total": 0,
            "compacted": 0,
            "rolling_digest": None,
            "history": [],
        },
    )
    if not isinstance(audit, dict):
        raise ValueError("resume_audit must be an object")
    history = audit.get("history")
    if not isinstance(history, list):
        raise ValueError("resume_audit.history must be an array")
    total = audit.get("total")
    compacted = audit.get("compacted")
    if type(total) is not int or total < 0:
        raise ValueError("resume_audit.total must be non-negative")
    if compacted != 0:
        raise ValueError("resume_audit.compacted must remain zero")

    from agent_run.resume_intent import is_ticket_execution_recovery
    from agent_run.resume_work import resume_message_owner
    executor_recovery = ticket_number is None and is_ticket_execution_recovery(state, state.get("status"))
    executor_target = resume_message_owner(state) if executor_recovery else None
    invocation = state.get("active_agent_invocation")
    if executor_recovery:
        invocation = (
            state.get("ticket_agent_invocations", {}).get(executor_target[0].split(":", 1)[1])
            if executor_target is not None else None
        )
    if ticket_number is not None:
        invocation = state.get("ticket_agent_invocations", {}).get(str(ticket_number))
    active = (
        invocation
        if isinstance(invocation, dict)
        and invocation_attempt_is_pending(state, invocation)
        and (
            ticket_number is None
            or invocation.get("work_subject") == f"ticket:{ticket_number}"
        )
        else None
    )
    bound = active.get("semantic_attempt") if active is not None else None
    attempt = bound if isinstance(bound, dict) else None
    human_owner: tuple[str, dict[str, Any]] | None = None
    if human_response_supplied and not executor_recovery and (
        ticket_number is not None or human_blocker_subject_count(state) == 1
    ):
        human_owner = next(
            (
                (location, subject)
                for location, subject in operator_gate_subjects(state)
                if ticket_number is None or location == f"ticket:{ticket_number}"
                if subject.get("blocked_reason") in {
                    "agent_requires_human", "reviewer_requires_human"
                }
            ),
            None,
        )
    requested_at = datetime.now(UTC).isoformat()
    sequence = total + 1
    event: dict[str, Any] = {
        "sequence": sequence,
        "requested_at": requested_at,
        "kind": _resume_kind(state, active, ticket_number=ticket_number),
        "source_status": str(state.get("status", "unknown")),
        "failure_code": _failure_code(state),
        "work_subject": active.get("work_subject") if active is not None else None,
        "generation": active.get("generation") if active is not None else None,
        "semantic_attempt_id": (
            attempt.get("attempt_id") if attempt is not None else None
        ),
        "source_invocation_started_at": (
            active.get("started_at") if active is not None else None
        ),
        "source_invocation_status": (
            active.get("status") if active is not None else None
        ),
        "thread_id": (
            active.get("reported_thread_id")
            or active.get("requested_thread_id")
            if active is not None
            else None
        ),
        "new_thread": new_thread,
        "human_response_supplied": human_response_supplied,
        "successor_invocation_started_at": None,
    }
    if executor_recovery:
        if executor_target is not None:
            event["work_subject"], _owner, event["generation"] = executor_target
    elif human_owner is not None:
        location, subject = human_owner
        event["work_subject"] = location if location.startswith("ticket:") else (
            f"parent-only:{state['run_id']}" if location == "parent" else f"{location.replace('_', '-')}:{state['run_id']}"
        )
        event["generation"] = subject.get(
            "ticket_branch_generation",
            subject.get("repair_generation", subject.get("acceptance_generation", 1)),
        )
    elif ticket_number is not None:
        jobs = state.get("ticket_jobs")
        ticket_job = jobs.get(str(ticket_number)) if isinstance(jobs, dict) else None
        if isinstance(ticket_job, dict):
            event["work_subject"] = f"ticket:{ticket_number}"
            event["generation"] = ticket_job.get("ticket_branch_generation")
    elif event["kind"] in {"integration_resync", "execution_failure"}:
        acceptance = state.get("run_acceptance")
        repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if (
            isinstance(repair, dict)
            and repair.get("phase") == "blocked"
            and repair.get("blocked_reason") == (
                "integration_resync_exhausted"
                if event["kind"] == "integration_resync"
                else "integration_failed"
            )
        ):
            event["work_subject"] = f"run-repair:{state['run_id']}"
            event["generation"] = repair.get("repair_generation")
    if event["work_subject"] is None and not executor_recovery:
        target = resume_message_owner(state, ticket_number)
        if target is not None:
            event["work_subject"], _owner, event["generation"] = target
    event["resume_id"] = resume_identity(str(state["run_id"]), event)
    event["event_digest"] = resume_event_digest(event)
    response_audit: dict[str, Any] | None = None
    response: str | None = None
    if human_response_supplied:
        response = _latest_human_response(state, event)
        if response is None:
            raise ValueError("Human Response Resume is missing its response fact")
        stored_audit = state.setdefault("human_response_audit", {})
        if not isinstance(stored_audit, dict):
            raise ValueError("human_response_audit must be an object")
        response_audit = stored_audit
    history.append(event)
    if response_audit is not None and response is not None:
        response_audit[event["resume_id"]] = response
    audit["rolling_digest"] = resume_history_digest(history)
    audit["total"] = sequence
    return deepcopy(event)


def _latest_human_response(
    state: dict[str, Any], event: dict[str, Any]
) -> str | None:
    work_subject = event.get("work_subject")
    generation = event.get("generation")
    subject: object = None
    if isinstance(work_subject, str) and work_subject.startswith("ticket:"):
        jobs = state.get("ticket_jobs")
        subject = jobs.get(work_subject.split(":", 1)[1]) if isinstance(jobs, dict) else None
    elif isinstance(work_subject, str) and work_subject.startswith("parent-only:"):
        subject = state.get("parent_job")
    elif isinstance(work_subject, str) and work_subject.startswith(("run-acceptance:", "run-integration:")):
        subject = state.get("run_acceptance")
    elif isinstance(work_subject, str) and work_subject.startswith("run-repair:"):
        acceptance = state.get("run_acceptance")
        subject = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    elif isinstance(work_subject, str) and work_subject.startswith("run-publication:"):
        subject = state.get("run_publication")
    history = subject.get("human_response_history") if isinstance(subject, dict) else None
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("generation") != generation:
            continue
        response = entry.get("response")
        if isinstance(response, str):
            return response
    return None


def latest_resume_audit(state: dict[str, Any]) -> dict[str, Any] | None:
    audit = state.get("resume_audit")
    history = audit.get("history") if isinstance(audit, dict) else None
    if not isinstance(history, list) or not history:
        return None
    assert isinstance(audit, dict)
    latest = history[-1]
    return deepcopy(latest) if isinstance(latest, dict) else None


def bind_resume_to_successor(
    state: dict[str, Any],
    *,
    semantic_attempt: dict[str, Any],
    successor_started_at: str,
) -> tuple[str, int] | None:
    """Link the next Invocation to the latest compatible public Resume."""

    audit = state.get("resume_audit")
    history = audit.get("history") if isinstance(audit, dict) else None
    if not isinstance(history, list) or not history:
        return None
    assert isinstance(audit, dict)
    event = history[-1]
    if not isinstance(event, dict) or event.get("kind") not in {
        "agent_invocation",
        "budget_checkpoint",
        "execution_failure",
        "github_refresh_retry",
        "human_blocker",
        "operator_stopped",
        "integration_resync",
        "integration_development",
    }:
        return None
    if event.get("successor_invocation_started_at") is not None:
        return None
    attempt_id = semantic_attempt.get("attempt_id")
    bound_attempt_id = event.get("semantic_attempt_id")
    if bound_attempt_id is not None and bound_attempt_id != attempt_id:
        return None
    resume_id = event.get("resume_id")
    sequence = event.get("sequence")
    if not isinstance(resume_id, str) or type(sequence) is not int:
        return None
    event.update(
        {
            "work_subject": semantic_attempt.get("work_subject"),
            "generation": semantic_attempt.get("generation"),
            "semantic_attempt_id": attempt_id,
            "successor_invocation_started_at": successor_started_at,
        }
    )
    event["event_digest"] = resume_event_digest(event)
    audit["rolling_digest"] = resume_history_digest(history)
    return resume_id, sequence


def _resume_kind(
    state: dict[str, Any], invocation: dict[str, Any] | None,
    *, ticket_number: int | None = None,
) -> str:
    from agent_run.resume_intent import is_ticket_execution_recovery
    if ticket_number is None and is_ticket_execution_recovery(state, state.get("status")):
        return "operator_stopped" if state.get("status") == "operator_stopped" else "execution_failure"
    if ticket_number is not None:
        jobs = state.get("ticket_jobs")
        job = jobs.get(str(ticket_number)) if isinstance(jobs, dict) else None
        if not isinstance(job, dict):
            raise ValueError("resume Ticket Job is missing")
        reason = job.get("blocked_reason")
        if reason in {"agent_requires_human", "reviewer_requires_human"}:
            return "human_blocker"
        if reason in {"modification_budget_exhausted", "review_budget_exhausted", "ci_development_budget_exhausted"}:
            return "budget_checkpoint"
        if reason == "integration_development_exhausted":
            return "integration_development"
        if reason == "integration_resync_exhausted":
            return "integration_resync"
        if reason == "ticket_supervision_timeout":
            return "supervision_timeout"
        if reason == "ticket_execution_failed":
            return "execution_failure"
        if reason == "ci_investigation_paused":
            return "ci_investigation"
        if reason == "ticket_paused":
            return "operator_stopped"
        if job.get("phase") == "publication_pending":
            return "publication_operation_retry"
        if job.get("phase") in {"blocked", "ready_for_human"}:
            return "execution_failure"
        raise ValueError("resume Ticket has no current local recovery boundary")
    if state.get("status") == "operator_stopped":
        return "operator_stopped"
    if state.get("status") == "supervision_timeout":
        return "supervision_timeout"
    if sum(
        subject.get("phase") == "publication_pending"
        for location, subject in operator_gate_subjects(state)
        if location.startswith("ticket:") or location in {"parent", "run_repair", "run_publication"}
    ) == 1:
        return "publication_operation_retry"
    if is_github_refresh_wait(state):
        return "github_refresh_retry"
    if len(budget_checkpoint_subjects(state)) == 1:
        return "budget_checkpoint"
    for key in ("active_ticket_job", "parent_job", "run_acceptance"):
        subject = state.get(key)
        if isinstance(subject, dict) and subject.get("blocked_reason") == "integration_development_exhausted":
            return "integration_development"
        if isinstance(subject, dict) and subject.get("blocked_reason") in {
            "ci_development_budget_exhausted"
        }:
            return "budget_checkpoint"
    for job in state.get("ticket_jobs", {}).values():
        if isinstance(job, dict) and job.get("blocked_reason") == "integration_development_exhausted":
            return "integration_development"
    for key in ("active_ticket_job", "parent_job", "run_acceptance"):
        subject = state.get(key)
        if (
            isinstance(subject, dict)
            and subject.get("phase") in {"blocked", "ready_for_human"}
            and subject.get("blocked_reason") == "integration_resync_exhausted"
        ):
            return "integration_resync"
    acceptance = state.get("run_acceptance")
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    if isinstance(repair, dict) and repair.get("phase") == "blocked":
        if repair.get("blocked_reason") == "integration_resync_exhausted":
            return "integration_resync"
        if repair.get("blocked_reason") == "integration_failed":
            return "execution_failure"
    if human_blocker_subject_count(state) == 1:
        return "human_blocker"
    if state.get("status") == "deterministic_contradiction" or has_non_invocation_execution_failure(state):
        return "execution_failure"
    if (
        invocation is not None
        and invocation.get("status") in {"failed", "completed", "resuming"}
        and invocation_attempt_is_pending(state, invocation)
    ):
        return "agent_invocation"
    from agent_run.resume_work import paused_work
    if len(paused_work(state, ticket_number)) == 1:
        return "execution_failure"
    raise ValueError("explicit Resume has no canonical recovery boundary")


def _failure_code(state: dict[str, Any]) -> str | None:
    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, list):
        return None
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        if isinstance(code, str) and code:
            return code
    return None
