"""Human status actions derived from existing controller and delivery facts."""
from __future__ import annotations

from agent_run.messages import display_text

from typing import Any

from agent_run.final_approval_operation import final_approval_cleanup_pending
from agent_run.presentation_helpers import current_work_subject


def controller_work(state: dict[str, Any], audit: dict[str, Any]) -> str | None:
    control = audit.get("executor_control")
    if not isinstance(control, dict) or control.get("activity") != "running":
        return None
    invocation = audit.get("agent_invocation")
    if isinstance(invocation, dict) and invocation.get("status") in {"running", "resuming"}:
        return None
    current = current_work_subject(state)
    phase = current[1].get("phase") if current else None
    return {
        "committing_candidate": display_text('presentation.status.saving_code_automatically_no_action_required'),
        "candidate": display_text('presentation.status.preparing_acceptance_no_action_required'),
        "publishing": display_text('presentation.status.preparing_to_create_a_pr_no_action_required'),
        "publication_pending": display_text('presentation.status.creating_a_pr_no_action_required'),
        "creating_pr": display_text('presentation.status.creating_a_pr_no_action_required'),
    }.get(str(phase))


def status_heading(state: dict[str, Any], view: dict[str, Any], fallback: object) -> object:
    if state.get("status") == "completed":
        return display_text('presentation.status.merged_cleanup_pending') if final_approval_cleanup_pending(state) else display_text('presentation.status.task_completed')
    current = current_work_subject(state)
    if current and current[0] == "run_acceptance" and view.get("execution_activity") == "running":
        return display_text('presentation.status.accepting_the_overall_requirement')
    if state.get("status") in {"run_approval_pending", "parent_approval_pending"}:
        subject = current[1] if current else {}
        number = subject.get("pr_number")
        return display_text('presentation.status.please_approve_merge_of_pr_v1', v1=number) if type(number) is int else display_text('presentation.status.awaiting_merge_approval')
    return fallback


def delivery_result_lines(state: dict[str, Any]) -> list[str]:
    if state.get("status") != "completed":
        return []
    subject = state.get("parent_job") if state.get("delivery_type") == "parent_only" else state.get("run_publication")
    if not isinstance(subject, dict):
        return []
    lines = []
    if subject.get("phase") in {"merged", "completed"}:
        number = subject.get("pr_number")
        if type(number) is int:
            lines.append(display_text('presentation.status.pr_v1_merged', v1=number))
    if subject.get("parent_closed") is True or (state.get("delivery_type") == "parent_only" and subject.get("phase") == "completed"):
        lines.append(display_text('presentation.status.parent_issue_closed'))
    elif state.get("delivery_type") != "parent_only":
        lines.append(display_text('presentation.status.parent_issue_closure_not_yet_confirmed'))
    return lines


def status_timing(state: dict[str, Any], invocation: dict[str, Any] | None = None) -> dict[str, Any]:
    from agent_run.cli_presentation import _semantic_attempt_history
    from agent_run.delivery_history import history_records
    from agent_run.execution_timing import execution_totals

    audit = {"semantic_agent_attempts": _semantic_attempt_history(state),
             "agent_invocations": state.get("agent_invocation_history", [])}
    records = history_records(state, audit)
    totals = execution_totals(state, records, None)
    identity = invocation.get("semantic_attempt") if invocation else None
    if isinstance(identity, dict):
        record = next((record for record in records if record.get("attempt_id") == identity.get("attempt_id")), None)
        if record is not None:
            totals["round_seconds"] = record.get("execution_seconds")
    return totals


def approval_lines(state: dict[str, Any]) -> list[str]:
    from agent_run.waiting_presentation import _check_results

    if state.get("status") not in {"run_approval_pending", "parent_approval_pending"}:
        return []
    current = current_work_subject(state)
    subject = current[1] if current else {}
    number = subject.get("pr_number")
    lines = []
    if type(number) is int and isinstance(state.get("repository"), str):
        lines.append(f"https://github.com/{state['repository']}/pull/{number}")
    lines.extend(f"{label}：{value}" for label, value in _check_results(subject, checks=True))
    return lines
