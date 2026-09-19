from __future__ import annotations

from agent_run.messages import display_text

from dataclasses import dataclass
from math import isfinite
from typing import Any, TypeGuard

from agent_run.final_approval_operation import final_approval_cleanup_pending
from agent_run.presentation_helpers import current_work_subject, human_pause_reason, unknown_execution_guidance


@dataclass(frozen=True)
class WaitingPresentation:
    work: str
    activity: str
    guidance: str
    show_command: bool
    details: tuple[tuple[str, str], ...]


def cleanup_instruction(state: dict[str, Any]) -> str | None:
    if state.get("status") == "completed" and final_approval_cleanup_pending(state):
        return display_text('presentation.status.merged_cleanup_pending_use_the_recovery_command_to_complete_works')
    cleanup = state.get("delivery_cleanup")
    if isinstance(cleanup, dict) and cleanup.get("status") == "cleanup_pending":
        return display_text('presentation.status.delivery_cleanup_is_incomplete_managed_checkouts_are_preserved_in')
    return None


def waiting_presentation(
    state: dict[str, Any], audit: dict[str, Any],
) -> WaitingPresentation | None:
    """Describe the current external wait without inferring Agent activity."""
    status = state.get("status")
    if status not in {
        "waiting_checks", "waiting_external", "waiting_merge", "supervision_timeout",
    }:
        return None
    raw = state.get("supervision_wait") or state.get("supervision_window")
    window = raw if isinstance(raw, dict) else {}
    snapshot = audit.get("supervision")
    wait = snapshot if isinstance(snapshot, dict) else {}
    checks = status == "waiting_checks" or window.get("kind") == "required_checks"
    selected = current_work_subject(state)
    subject = selected[1] if selected is not None else {}
    number = subject.get("pr_number")
    target = f"PR #{number}" if type(number) is int else "GitHub"
    work = (
        display_text('presentation.status.waiting_for_pre_merge_checks_on_v1', v1=target) if checks
        else display_text('presentation.status.waiting_for_merge_confirmation_on_v1', v1=target) if status == "waiting_merge"
        else display_text('presentation.status.waiting_for_the_operation_result_on_v1', v1=target)
    )
    credential_failure = wait.get("credential_failure_class")
    if isinstance(credential_failure, str):
        work = display_text('presentation.status.waiting_for_working_credentials_to_become_available')
    control = audit.get("executor_control")
    activity = control.get("activity") if isinstance(control, dict) else "unknown"
    timed_out = status == "supervision_timeout"
    if timed_out:
        description = display_text('presentation.status.automatic_waiting_timed_out_for_this_round')
        guidance = display_text('presentation.status.run_the_following_command_to_resume_waiting')
    elif activity == "running":
        description = display_text('presentation.status.checking_pre_merge_results_in_the_background') if checks else display_text('presentation.status.waiting_for_the_operation_result_in_the_background')
        cleanup = cleanup_instruction(state)
        guidance = display_text('presentation.status.background_waiting_will_continue_v1', v1=cleanup) if cleanup else display_text('presentation.status.no_action_required')
    elif activity == "not_running":
        description = display_text('presentation.status.automatic_waiting_stopped')
        guidance = display_text('presentation.status.run_the_following_command_when_ready_to_continue')
    else:
        description = display_text('presentation.status.cannot_confirm_whether_background_waiting_is_continuing')
        guidance = unknown_execution_guidance(state)
    details = _wait_times(window, wait, timed_out=timed_out)
    if isinstance(credential_failure, str):
        reason = (
            display_text('presentation.status.github_working_credentials_are_temporarily_unavailable') if credential_failure == "credential_unavailable"
            else display_text('presentation.status.github_working_credentials_are_unavailable_see_json_diagnostics_f')
        )
        details.append((display_text('presentation.status.reason'), reason))
        if type(wait.get("retry_count")) is int:
            details.append((display_text('presentation.status.retry_count'), str(wait["retry_count"])))
    details.extend(_check_results(subject, checks=checks))
    observation = wait.get("latest_observation")
    if isinstance(observation, dict) and isinstance(observation.get("message"), str):
        details.append((display_text('presentation.status.external_observation'), human_pause_reason(observation["message"])))
    return WaitingPresentation(
        work, description, guidance,
        show_command=timed_out or activity == "not_running",
        details=tuple(details),
    )


def _wait_times(
    window: dict[str, Any], wait: dict[str, Any], *, timed_out: bool,
) -> list[tuple[str, str]]:
    started, deadline = window.get("started_at"), window.get("deadline")
    remaining = wait.get("remaining_seconds")
    elapsed: object = window.get("elapsed_seconds") if timed_out else None
    if (
        not timed_out and _number(started) and _number(deadline)
        and _number(remaining) and deadline >= started
        and 0 <= remaining <= deadline - started
    ):
        elapsed = int(deadline - started) - int(remaining)
    elif not timed_out:
        remaining = None
    return [
        (display_text('presentation.status.waited'), _duration(elapsed)),
        (display_text('presentation.status.this_wait_round') if timed_out else display_text('presentation.status.maximum_wait_remaining'),
         display_text('presentation.status.timed_out') if timed_out else _duration(remaining)),
    ]


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _duration(value: object) -> str:
    if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return display_text('presentation.status.unknown')
    minutes = int(value) // 60
    if minutes < 1:
        return display_text('presentation.status.less_than_1_minute')
    hours, minutes = divmod(minutes, 60)
    return display_text('presentation.status.v0_hours_v2_minutes', v0=hours, v2=minutes) if hours else display_text('presentation.status.v0_minutes', v0=minutes)


def _check_results(subject: dict[str, Any], *, checks: bool) -> list[tuple[str, str]]:
    evidence = subject.get("required_checks_evidence")
    if not checks and not isinstance(evidence, dict):
        return []
    record = subject.get("record")
    expected_head = subject.get("publication_sha") or subject.get("head_sha")
    if isinstance(record, dict):
        expected_head = record.get("pr_head_sha") or record.get("run_head_sha") or expected_head
    if (
        not isinstance(evidence, dict) or not isinstance(expected_head, str)
        or evidence.get("head_sha") != expected_head
        or evidence.get("pr_number") != subject.get("pr_number")
    ):
        return [(display_text('presentation.status.pre_merge_checks'), display_text('presentation.status.no_check_results_for_the_current_candidate_yet'))]
    unavailable = subject.get("required_checks_observation_status") in {"unavailable", "unknown"}
    result = _check_result(evidence.get("result"))
    details = [(display_text('presentation.status.previous_pre_merge_checks') if unavailable else display_text('presentation.status.pre_merge_checks'), result)]
    if unavailable:
        details.append((display_text('presentation.status.current_observation'), display_text('presentation.status.latest_check_results_cannot_currently_be_confirmed')))
    values = evidence.get("checks")
    if isinstance(values, list):
        for check in values:
            if isinstance(check, dict) and isinstance(check.get("name"), str):
                details.append((check["name"], _check_result(check.get("bucket"))))
    return details


def _check_result(value: object) -> str:
    return {
        "pending": display_text('presentation.status.pending_178'), "pass": display_text('presentation.status.passed'), "fail": display_text('presentation.status.failed'),
        "cancel": display_text('presentation.status.canceled'), "none": display_text('presentation.status.no_pre_merge_checks_configured'), "skipping": display_text('presentation.status.skipped'),
    }.get(str(value), display_text('presentation.status.cannot_currently_confirm'))
