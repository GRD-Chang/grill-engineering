from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_run.delivery_status import (
    execution_guidance,
    invocation_recovery_details,
)
from agent_run.final_approval_operation import approval_failure_text
from agent_run.messages import display_language, text
from agent_run.operator_action_presentation import human_action_type, human_preserved_results, operator_action_items
from agent_run.presentation_helpers import (
    human_delivery_object,
    human_pause_reason,
    human_status_term,
    human_next_action,
    terminal_safe,
)
from agent_run.waiting_presentation import cleanup_instruction, waiting_presentation, scheduler_notice
from agent_run.history_facts import (
    history_records as semantic_history_records,
    history_record_status_code,
    timeline_kind, scope_change_details, timeline_object,
    subject_label, utc_timestamp, human_responses_by_subject,
)
from agent_run.attempt_evidence import (
    owners_for_attempt, review_artifact_for_attempt,
    invocation_attempt_view,
)
from agent_run.acceptance_facts import acceptance_lanes, artifact_findings as _artifact_findings
from agent_run.delivery_facts import role_family as _role_family, ticket_phase_execution_activity, multi_ticket_progress, scheduler_progress
from agent_run.execution_timing import (
    invocation_activity, invocation_execution_seconds,
    run_elapsed_seconds,
    parse_optional_timestamp as _parse_optional_timestamp,
)


def _copy(key: str, **values: object) -> str:
    return text(key, language=display_language(), **values)


HISTORY_DETAIL_LIMIT = 240


def _approval_failures(audit: dict[str, Any]) -> list[dict[str, Any]]:
    control = audit.get("executor_control")
    failures = control.get("approval_failures") if isinstance(control, dict) else None
    return [item for item in failures if isinstance(item, dict)] if isinstance(failures, list) else []


def history_progress_view(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    events = _history_events(state, audit)
    execution_activity = ticket_phase_execution_activity(
        state, audit, invocation_activity(state.get("active_agent_invocation"), audit)
    )
    control = audit.get("executor_control")
    executor_activity = str(control.get("activity", "unknown")) if isinstance(control, dict) else "unknown"
    return {
        "events": events,
        "execution_activity": execution_activity,
        "scheduler": scheduler_progress(control),
        "multi_ticket": multi_ticket_progress(state, executor_activity=executor_activity),
        "summary": {
            "rounds": _round_summary(audit.get("semantic_agent_attempts")),
            "elapsed_seconds": _elapsed_seconds(state, events),
        },
        "time_zone": "UTC",
    }


def print_history_progress(
    state: dict[str, Any],
    audit: dict[str, Any],
    progress: dict[str, Any],
    *,
    display_term: Callable[[object], object],
    print_operator_action: Callable[[dict[str, Any]], None],
    details: bool = False,
) -> None:
    timezone, timezone_label = _local_timezone()
    parent = state.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    print(_copy('presentation.history.repository', v1=_safe_text(state.get('repository') or _copy('presentation.history.unknown'))))
    print(
        _copy('presentation.history.requirement', v1=_safe_text(parent_view.get('number', '?')), v3=_safe_text(parent_view.get('title') or _copy('presentation.history.untitled_requirement')))
    )
    print(_copy('presentation.history.timezone', v1=timezone_label))
    if notice := scheduler_notice(progress.get("scheduler")):
        print(notice)
    if progress["summary"]["elapsed_seconds"] is not None:
        print(_copy('presentation.history.elapsed', v1=_duration(progress['summary']['elapsed_seconds'])))
    executor_control = audit.get("executor_control")
    if (
        isinstance(executor_control, dict)
        and executor_control.get("activity") == "unknown"
    ):
        print(_copy('presentation.history.agent_activity_unknown_execution_status_cannot_be_confirmed'))
    if progress["execution_activity"] == "interrupted":
        print(_copy('presentation.history.execution_interrupted_waiting_to_resume'))
    records = history_records(state, audit, progress["events"])
    print(_copy('presentation.history.work_timeline'))
    if records:
        for record in records:
            for line in _record_lines(record, timezone=timezone, details=details):
                print(line)
    else:
        print(_copy('presentation.history.no_confirmed_agent_work_records_yet'))
    for failure in _approval_failures(audit):
        print(f"  {_safe_text(approval_failure_text(failure))}")
    cleanup_lines = _cleanup_history_lines(state, details=details)
    if cleanup_lines:
        print(_copy('presentation.history.delivery_cleanup'))
        for line in cleanup_lines:
            print(line)

    print(_copy('presentation.history.summary'))
    rounds = progress["summary"]["rounds"]
    if rounds:
        for key, count in rounds.items():
            print(_copy('presentation.history.rounds', v1=f'{_round_label(key):<22}', v2=count))
    else:
        print(_copy('presentation.history.no_agent_rounds_yet'))
    if progress["summary"]["elapsed_seconds"] is not None:
        print(f"  {_copy('presentation.history.elapsed_time'):<22}{_duration(progress['summary']['elapsed_seconds'])}")
    waiting_lines = _current_wait_lines(state, audit)
    if waiting_lines:
        print("\n" + "\n".join(waiting_lines))
        return
    if progress["execution_activity"] in {
        "interrupted",
        "unknown",
        "capacity_wait",
        "recovery_wait",
    } and not operator_action_items(audit):
        print(_copy('presentation.history.next_step', v1=execution_guidance(state, progress['execution_activity'])))
        return
    actions = operator_action_items(audit)
    if actions:
        for operator_action in actions:
            print()
            bounded_action = dict(operator_action)
            reasons = operator_action.get("reasons")
            if isinstance(reasons, list):
                bounded_action["reasons"] = [
                    _truncate_history_detail(str(reason)) for reason in reasons
                ]
            print_operator_action(bounded_action)
    else:
        print(
            _copy('presentation.history.next_step_17', v1=human_next_action(audit.get('next_action'), run_id=state.get('run_id')), v3=_operator_instruction(state, None))
        )


def print_rich_history_progress(
    state: dict[str, Any],
    audit: dict[str, Any],
    progress: dict[str, Any],
    *,
    details: bool = False,
) -> None:
    """Render a bounded, static Rich timeline for an interactive terminal."""

    from rich.console import Console
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    timezone, timezone_label = _local_timezone()
    records = history_records(state, audit, progress["events"])
    parent = state.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    # A Panel renders child Text lines with crop semantics. Put each logical
    # line in a foldable Table column so long URLs, commands, and CJK text wrap
    # inside the panel and retain their complete content at narrow widths.
    console = Console(highlight=False, soft_wrap=False)
    header = [
        _rich_line(
            _copy('presentation.history.task'),
            f"{state.get('repository') or 'unknown'} · #{parent_view.get('number', '?')} {parent_view.get('title') or _copy('presentation.history.untitled_requirement')}",
        ),
        _rich_line(_copy('presentation.history.timezone_19'), timezone_label),

    ]
    if notice := scheduler_notice(progress.get("scheduler")):
        header.append(Text(notice))
    if progress["summary"]["elapsed_seconds"] is not None:
        header.append(_rich_line(_copy('presentation.history.elapsed_time'), _duration(progress["summary"]["elapsed_seconds"])))
    if progress.get("execution_activity") in {
        "interrupted",
        "unknown",
        "capacity_wait",
        "recovery_wait",
    }:
        header.append(
            _rich_line(
                _copy('presentation.history.activity'),
                _execution_activity_text(str(progress["execution_activity"])),
            )
        )

    body: list[Text] = [*header, Text(""), Text(_copy('presentation.history.work_timeline_21'), style="bold")]
    for record in records:
        body.extend(
            Text(line) for line in _record_lines(record, timezone=timezone, details=details)
        )
        body.append(Text(""))
    if not records:
        body.append(Text(_copy('presentation.history.no_confirmed_agent_work_records_yet')))
    for failure in _approval_failures(audit):
        body.append(Text(f"  {_safe_text(approval_failure_text(failure))}"))
    cleanup_lines = _cleanup_history_lines(state, details=details)
    if cleanup_lines:
        body.extend([Text(""), Text(_copy('presentation.history.delivery_cleanup_22'), style="bold")])
        body.extend(Text(line) for line in cleanup_lines)
    for action in operator_action_items(audit):
        body.extend([Text(""), Text(_copy('presentation.history.action_required'), style="bold yellow")])
        body.append(Text(_copy('presentation.history.type', v1=_safe_text(human_action_type(action.get('type'))))))
        body.append(Text(_copy("presentation.status.object_v1", v1=human_delivery_object(action.get("object")))))
        body.append(Text(_copy("presentation.history.command", v1=action.get("next_action"))))
        reasons = action.get("reasons")
        if isinstance(reasons, list):
            for reason in reasons:
                value = _safe_text(
                    reason if action.get("type") == "Human Blocker" else human_pause_reason(reason)
                )
                if not details:
                    value = _truncate_history_detail(value)
                body.append(Text(_copy('presentation.history.reason', v1=value)))
        preserved = action.get("preserved")
        if preserved is not None:
            body.append(Text(_copy('presentation.history.preserved_results', v1=_safe_text(human_preserved_results(preserved)))))
        phase = action.get("phase")
        if phase is not None:
            body.append(Text(_copy('presentation.history.phase', v1=_safe_text(human_status_term(phase)))))
        if action.get("scope") == "global":
            body.append(Text(_copy('presentation.history.the_entire_task_is_paused_other_subtasks_will_not_continue')))
        else:
            body.append(Text(_copy('presentation.status.local_operator_gate')))
    body.extend(
        [
            Text(""),
            Text(_copy('presentation.history.summary_29'), style="bold"),
            Text(
                _copy('presentation.history.agent_rounds', v1=_round_summary_text(progress['summary']['rounds']))
            ),

        ]
    )
    waiting_lines = _current_wait_lines(state, audit)
    if waiting_lines:
        body.extend([Text(""), *(Text(line) for line in waiting_lines)])
    elif progress.get("execution_activity") in {
        "interrupted",
        "unknown",
        "capacity_wait",
        "recovery_wait",
    }:
        body.extend(
            [
                Text(""),
                _rich_line(
                    _copy('presentation.history.next_step_31'),
                    execution_guidance(
                        state,
                        str(progress["execution_activity"]),
                    ),
                ),
            ]
        )
    else:
        next_action = audit.get("next_action")
        body.extend(
            [
                Text(""),
                _rich_line(
                    _copy('presentation.history.next_step_31'),
                    human_next_action(next_action, run_id=state.get("run_id")),
                ),
                Text(_operator_instruction(state, None)),
            ]
        )
    border_style = {
        "interrupted": "yellow",
        "unknown": "yellow",
        "capacity_wait": "yellow",
        "recovery_wait": "red",
    }.get(str(progress.get("execution_activity")), "cyan")
    table = Table.grid(expand=True, padding=(0, 0))
    table.add_column(no_wrap=False, overflow="fold")
    for line in body:
        indent = len(line.plain) - len(line.plain.lstrip(" "))
        table.add_row(Padding(line[indent:], (0, 0, 0, indent)) if indent else line)
    console.print(
        Panel(
            table,
            title=Text(_copy('presentation.history.agent_run_work_timeline'), style="bold"),
            border_style=border_style,
            expand=True,
        ),
        overflow="fold",
        crop=False,
    )


def history_records(
    state: dict[str, Any], audit: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Decorate shared semantic records for the plain and Rich renderers."""
    records = semantic_history_records(state, audit, events)
    for record in records:
        for point in record.get("turning_points", []):
            for step in point.get("creation_steps", []):
                key = {"ensure_final_run_ref": "prepare_remote_branch", "create_final_pr": "create_pr"}.get(step.get("action"))
                if key:
                    step["action"] = _copy(f"presentation.history.{key}")
            changes = point.pop("scope_ticket_changes", {})
            if changes:
                source_details = point.get("details", [])
                # Only the generated suffix is localized. Blockers and the
                # external summary may contain the exact same label text.
                point["details"] = source_details[:-len(changes)] + [
                    f"{_copy('presentation.history.' + label)} {changes[key]}"
                    for key, label in (
                        ("added_tickets", "added_subtasks"),
                        ("removed_tickets", "removed_subtasks"),
                    ) if key in changes
                ]
        if record.get("event_record"):
            record["object"] = _safe_text(record["object"])
            record["role_label"] = _copy("presentation.history.lifecycle_event")
            record["role_zh"] = _copy("presentation.history.event")
            event: dict[str, Any] = next(iter(record.get("turning_points", [])), {})
            record["status_text"] = _turning_point_status(event)
        else:
            role = str(record.get("role") or "unknown")
            record["role_label"] = _role_label(role)
            record["role_zh"] = _role_zh_label(role)
            record["status_text"] = _history_record_status(record)
            record["recovery_details"] = [
                detail for invocation in record["invocations"]
                for detail in invocation_recovery_details(invocation)
            ]
    return records


def _history_record_status(record: dict[str, Any]) -> str:
    return {
        "execution_failed": _copy('presentation.history.execution_failed'), "review_failed": _copy('presentation.history.acceptance_failed'),
        "review_passed": _copy('presentation.history.acceptance_passed'), "review_blocked": _copy('presentation.history.acceptance_blocked'),
        "running": _copy('presentation.history.in_progress'), "interrupted": _copy('presentation.history.execution_interrupted_waiting_to_resume'),
        "unknown": _copy('presentation.history.execution_status_unknown'), "pending": _copy('presentation.history.pending'),
        "candidate": _copy('presentation.history.development_complete_awaiting_acceptance'),
        "publication_artifact": _copy('presentation.history.publication_narrative_complete'),
        "acceptance_artifact": _copy('presentation.history.acceptance_result_recorded'),
        "currentness_invalidated": _copy('presentation.history.invalidated_by_revision_change'),
        "no_code_changes": _copy('presentation.history.development_produced_no_code_changes'),
        "git_integrity_repair": _copy('presentation.history.git_repair_boundary_recorded'),
        "completed": _copy('presentation.history.completed'),
    }[history_record_status_code(record)]


def _record_lines(
    record: dict[str, Any], *, timezone: tzinfo, details: bool
) -> list[str]:
    if record.get("event_record") is True:
        return _event_record_lines(record, timezone=timezone, details=details)
    started = _format_local_timestamp(record.get("started_at"), timezone)
    ended_value = record.get("ended_at")
    ended = (
        _format_local_timestamp(ended_value, timezone)
        if ended_value is not None
        else (_copy('presentation.history.in_progress') if record.get("activity") == "running" else _copy('presentation.history.unknown_time'))
    )
    interval = f"{started} → {ended}" if ended_value is not None else started
    if ended_value is None and record.get("activity") == "running":
        interval += _copy('presentation.history.in_progress_50')
    ordinal = record.get("ordinal")
    round_text = _copy('presentation.history.round', v1=ordinal) if isinstance(ordinal, int) else _copy('presentation.history.round_not_recorded')
    finding_values = record.get("findings")
    findings = finding_values if isinstance(finding_values, list) else []
    title = (
        f"{_safe_text(human_delivery_object(record.get('object') or 'Delivery Run'))} · {_safe_text(record.get('role_label') or 'Agent')} {round_text} · {_safe_text(record.get('status_text') or _copy('presentation.history.unknown'))}"
    )
    if findings:
        title += _copy('presentation.history.findings', v1=len(findings))
    lines = [f"  {title}"]
    if _parse_optional_timestamp(record.get("started_at")) is not None:
        lines.append(_copy('presentation.history.time_range', v1=interval))
    if record.get("execution_seconds") is not None:
        lines.append(_copy('presentation.history.round_execution_time', v1=_duration(record['execution_seconds'])))
    if record.get("execution_timing_complete") is False:
        lines.append(_copy('presentation.history.timing_incomplete'))
        if type(record.get("recorded_execution_seconds")) is int:
            lines.append(_copy('presentation.history.recorded_execution_time', v1=_duration(record['recorded_execution_seconds'])))
    if record.get("span_seconds") is not None and record.get("span_seconds") != record.get("execution_seconds"):
        lines.append(_copy('presentation.history.round_elapsed_time', v1=_duration(record['span_seconds'])))
    if details and _role_family(str(record.get("role") or "")) != "publication":
        lines.append(f"    {_budget_line(record)}")
    configurations = record.get("configurations")
    if isinstance(configurations, list) and configurations:
        for index, configuration in enumerate(configurations, start=1):
            lines.append(
                _copy('presentation.history.model_reasoning_effort', v1=_copy('presentation.history.configuration') + str(index) if len(configurations) > 1 else _copy('presentation.history.configuration_59'), v3=_safe_text(configuration.get('model') or _copy('presentation.history.not_recorded')), v5=_safe_text(configuration.get('reasoning_effort') or _copy('presentation.history.not_recorded')))
            )
    else:
        lines.append(_copy('presentation.history.configuration_model_not_recorded_reasoning_effort_not_recorded'))
    if record.get("resumption_count", 0):
        lines.append(_copy('presentation.history.explicit_resumes_continuations_included_in_this_round', v1=record['resumption_count']))
    output_attempts = record.get("output_attempts")
    if isinstance(output_attempts, list) and any(
        isinstance(value, int) and value > 1 for value in output_attempts
    ):
        lines.append(
            _copy('presentation.history.output_continuations_included_in_this_round', v1=max((value for value in output_attempts if isinstance(value, int))) - 1)
        )
    recovery_details = record.get("recovery_details")
    if isinstance(recovery_details, list):
        lines.extend(
            f"    {_truncate_history_detail(_safe_text(detail))}"
            for detail in recovery_details
        )
    if findings:
        lines.append(_copy('presentation.history.findings_64', v1=len(findings)))
        lines.extend(
            f"      {_finding_question(finding)}"
            for finding in findings
        )
    elif record.get("role") in {"reviewer", "fresh_acceptance"} and record.get(
        "acceptance_artifact"
    ) is None:
        lines.append(_copy('presentation.history.acceptance_outcome_no_confirmed_acceptance_evidence_yet'))
    if details:
        lines.extend(_record_detail_lines(record, timezone=timezone))
    turning_points = record.get("turning_points")
    if isinstance(turning_points, list) and turning_points:
        lines.append(_copy('presentation.history.key_transitions'))
        for point in turning_points:
            if not isinstance(point, dict):
                continue
            point_details = point.get("details")
            lines.append(
                f"      {_format_local_timestamp(point.get('at'), timezone)} · "
                f"{_event_title(point)}"
            )
            if isinstance(point_details, list):
                lines.extend(
                    (
                        f"        - {_safe_text(value)}"
                        if details
                        else "        - "
                        f"{_truncate_history_detail(_safe_text(value))}"
                    )
                    for value in point_details
                )
            lines.extend(_turning_point_evidence_lines(point, timezone=timezone, details=details))
    return lines


def _event_record_lines(
    record: dict[str, Any], *, timezone: tzinfo, details: bool
) -> list[str]:
    point = next(
        (
            value
            for value in record.get("turning_points", [])
            if isinstance(value, dict)
        ),
        {},
    )
    lines = [
        f"  {_safe_text(human_delivery_object(record.get('object') or 'Delivery Run'))} · {_event_title(point)}",
        _copy('presentation.history.time', v1=_format_local_timestamp(point.get('at'), timezone)),
    ]
    if point.get("kind") in {"required_checks", "approval", "pr_creation"} and record.get("span_seconds"):
        lines[1] = (
            _copy('presentation.history.time_range_68', v1=_format_local_timestamp(record.get('started_at'), timezone), v3=_format_local_timestamp(record.get('ended_at'), timezone))
        )
        if point.get("kind") != "pr_creation":
            lines.append(_copy('presentation.history.recorded_wait_time', v1=_duration(record['span_seconds'])))
    point_details = point.get("details")
    if isinstance(point_details, list):
        lines.extend(
            f"      - {_safe_text(value) if details else _truncate_history_detail(_safe_text(value))}"
            for value in point_details
        )
    lines.extend(_turning_point_evidence_lines(point, timezone=timezone, details=details))
    if details:
        for step in point.get("creation_steps", []):
            lines.append(f"      {_format_local_timestamp(step['at'], timezone)} · {step['action']}")
    return lines


def _event_title(point: dict[str, Any]) -> str:
    kind = point.get("kind")
    if kind == "pr_creation":
        return _copy('presentation.history.final_pr_created', v1=point['pr_number'])
    if kind == "completion":
        return _copy('presentation.history.delivery_abandoned') if point.get("status") == "abandoned" else _copy('presentation.history.delivery_completed')
    if kind == "integration":
        if point.get("status") == "parent_closeout_pending":
            return _copy('presentation.history.code_merged_closing_the_requirement_issue')
        return _copy('presentation.history.code_merged') if point.get("commit_sha") else _copy('presentation.history.merge_code')
    if kind == "approval":
        return _copy('presentation.history.human_approval_granted') if point.get("approval_granted_at") else _copy('presentation.history.awaiting_human_approval')
    if kind == "required_checks":
        return _turning_point_status(point)
    if kind == "publication":
        return (
            _copy('presentation.history.create_update_pr', v1=point['pr_number'])
            if point.get("pr_number") is not None else _copy('presentation.history.prepare_code_commit_and_pr_creation')
        )
    action = point.get("github_write_action")
    if action in {"ensure_final_run_ref", "create_final_pr", "refresh_final_pr_narrative"}:
        label = {
            "ensure_final_run_ref": _copy('presentation.history.prepare_remote_branch'),
            "create_final_pr": _copy('presentation.history.create_final_pr'),
            "refresh_final_pr_narrative": _copy('presentation.history.update_final_pr_narrative'),
        }[action]
        return f"{label} · {_turning_point_status(point)}"
    return f"{_turning_point_kind(point)} · {_turning_point_status(point)}"


def _budget_line(record: dict[str, Any]) -> str:
    if _role_family(str(record.get("role") or "")) == "publication":
        return _copy('presentation.history.execution_allowance_not_applicable_publication_narrative')
    facts = record.get("budget_facts")
    if not isinstance(facts, dict):
        return _copy('presentation.history.used_at_round_start_development_count_not_recorded_acceptance_cou')
    window = facts.get("window")
    window_text = window if isinstance(window, int) else _copy('presentation.history.not_recorded')
    if "ci_developments_used" in facts:
        return _copy(
            "presentation.history.ticket_ci_usage",
            window=window_text, used=facts.get("ci_developments_used"),
            limit=facts.get("ci_development_limit"),
        )
    if "integration_development_used" in facts:
        return _copy(
            "presentation.history.integration_development_usage",
            window=window_text, used=facts.get("integration_development_used"),
            limit=facts.get("integration_development_limit"),
        )
    development = _budget_fraction(
        facts.get("development_attempts"), facts.get("development_limit")
    )
    review = _budget_fraction(
        facts.get("reviewer_invocations"), facts.get("reviewer_limit")
    )
    return (
        _copy('presentation.history.authorization_window_used_at_round_start_development_acceptance', v1=window_text, v3=development, v5=review)
    )


def _budget_fraction(used: object, limit: object) -> str:
    used_text = str(used) if type(used) is int else _copy('presentation.history.not_recorded')
    limit_text = str(limit) if type(limit) is int else _copy('presentation.history.not_recorded')
    return f"{used_text} / {limit_text}"


def _append_detail_scalar(
    lines: list[str], *, indent: str, label: str, value: object
) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return
    if isinstance(value, (str, int, float, bool)):
        lines.append(f"{indent}{label}：{_safe_text(value)}")


def _first_detail_value(
    value: dict[str, Any], keys: tuple[str, ...]
) -> object:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and (
            not isinstance(candidate, str) or candidate.strip()
        ):
            return candidate
    return None


def _required_checks_mode(value: dict[str, Any]) -> object:
    mode = _first_detail_value(value, ("required_checks_mode",))
    if mode is not None:
        return mode
    result = _first_detail_value(
        value, ("result", "required_checks", "required_checks_result")
    )
    if result == "none":
        return "not_configured"
    if isinstance(result, str) and result in {
        "pass",
        "pending",
        "unknown",
        "fail",
    }:
        return "configured"
    evidence = value.get("required_checks_evidence")
    if isinstance(evidence, dict):
        return _required_checks_mode(evidence)
    return None


def _append_check_observation_detail(
    lines: list[str],
    observation: object,
    *,
    indent: str,
    result_label: str | None = None,
) -> None:
    result_label = result_label or _copy('presentation.history.pre_merge_check_result')
    if not isinstance(observation, dict):
        return
    _append_detail_scalar(
        lines,
        indent=indent,
        label=_copy('presentation.history.pr_number'),
        value=observation.get("pr_number"),
    )
    _append_detail_scalar(
        lines,
        indent=indent,
        label=result_label,
        value=human_status_term(_first_detail_value(observation, ("result", "status", "conclusion"))),
    )
    _append_detail_scalar(
        lines,
        indent=indent,
        label=_copy('presentation.history.check_result_observed_at'),
        value=(
            _format_local_timestamp(observed_at, _local_timezone()[0])
            if (observed_at := _first_detail_value(observation, ("observed_at", "required_checks_observed_at")))
            else None
        ),
    )
    checks = observation.get("checks")
    if not isinstance(checks, list):
        return
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = _first_detail_value(check, ("name", "check_name"))
        result = _first_detail_value(
            check, ("result", "conclusion", "status", "bucket")
        )
        if name is None and result is None:
            continue
        details: list[str] = []
        if isinstance(name, (str, int, float, bool)):
            details.append(_copy('presentation.history.name', v1=_safe_text(name)))
        if isinstance(result, (str, int, float, bool)):
            details.append(_copy('presentation.history.result', v1=_safe_text(human_status_term(result))))
        for key, label in (
            ("workflow", _copy('presentation.history.workflow')),
            ("link", _copy('presentation.history.link')),
            ("description", _copy('presentation.history.description')),
        ):
            extra = check.get(key)
            if isinstance(extra, (str, int, float, bool)) and (
                not isinstance(extra, str) or extra.strip()
            ):
                details.append(f"{label}={_safe_text(extra)}")
        lines.append(_copy('presentation.history.check', v0=indent, v2='；'.join(details)))


def _git_integrity_detail_lines(
    evidence: dict[str, Any], *, indent: str
) -> list[str]:
    lines: list[str] = []
    for key, label in (
        ("status", _copy('presentation.history.result_95')),
        ("reason", _copy('presentation.history.failure_reason')),
        ("expected_head", _copy('presentation.history.expected_head')),
        ("observed_head", _copy('presentation.history.observed_head')),
        ("base_sha", _copy('presentation.history.base_head')),
        ("previous_candidate_sha", _copy('presentation.history.previous_code_revision')),
        ("workspace_clean", _copy('presentation.history.workspace_clean')),
        ("recovery_head", _copy('presentation.history.recovered_head')),
        ("recovery_action", _copy('presentation.history.recovery_action')),
        ("recovery_error", _copy('presentation.history.recovery_error')),
    ):
        value = evidence.get(key)
        if key == "status":
            value = human_status_term(value)
        elif key == "workspace_clean" and isinstance(value, bool):
            value = _copy('presentation.history.yes') if value else _copy('presentation.history.no')
        elif key == "recovery_action" and value == "controller_reset_and_clean":
            value = _copy('presentation.history.restore_saved_revision_and_clean_workspace')
        _append_detail_scalar(lines, indent=indent, label=label, value=value)
    return lines


def _supporting_record_detail_lines(
    kind: str, value: dict[str, Any]
) -> list[str]:
    """Render the operator-facing subset of a supporting record.

    Supporting records are durable audit objects and intentionally contain
    reviewer identities, policy snapshots, budgets, and nested authorizations.
    Human history needs the business result, not a recursive serialization of
    that internal record. Keep this projection explicit so new private fields
    cannot leak into ``history --details`` by accident.
    """

    lines: list[str] = []
    mode_text = {
        "configured": _copy('presentation.history.pre_merge_checks_configured'), "not_configured": _copy('presentation.history.no_pre_merge_checks_configured'),
    }.get(str(_required_checks_mode(value)))
    if kind == "required_checks_evidence":
        if mode_text:
            lines.append(f"      {mode_text}")
        _append_check_observation_detail(lines, value, indent="      ")
        return lines

    if kind == "deterministic_integration_record":
        source = value.get("source")
        if source == "accepted":
            lines.append(_copy('presentation.history.merged_after_acceptance_passed'))
        elif source == "fallback":
            lines.append(_copy('presentation.history.merged_after_fallback_checks_without_independent_acceptance_passi'))
        if mode_text:
            lines.append(f"      {mode_text}")
        _append_detail_scalar(
            lines,
            indent="      ",
            label=_copy('presentation.history.pre_merge_check_result'),
            value=human_status_term(_first_detail_value(value, ("required_checks", "required_checks_result"))),
        )
        _append_detail_scalar(
            lines, indent="      ", label=_copy('presentation.history.pr_number'), value=value.get("pr_number")
        )
        pr = value.get("pr")
        if isinstance(pr, dict):
            _append_detail_scalar(
                lines, indent="      ", label=_copy('presentation.history.pr_status'), value=human_status_term(pr.get("state"))
            )
        _append_detail_scalar(
            lines,
            indent="      ",
            label=_copy('presentation.history.integration_summary'),
            value=value.get("integrated_message"),
        )
        evidence = value.get("required_checks_evidence")
        if isinstance(evidence, dict):
            lines.append(_copy('presentation.history.pre_merge_check_evidence'))
            _append_check_observation_detail(lines, evidence, indent="        ")
        return lines

    if kind == "fallback_publication_receipt":
        lines.append(_copy('presentation.history.acceptance_allowance_exhausted_published_using_fallback_checks_no'))
        _append_detail_scalar(
            lines, indent="      ", label=_copy('presentation.history.pr_number'), value=value.get("pr_number")
        )
        if mode_text:
            lines.append(f"      {mode_text}")
        evidence = value.get("required_checks_evidence")
        if isinstance(evidence, dict):
            lines.append(_copy('presentation.history.pre_merge_check_evidence'))
            _append_check_observation_detail(lines, evidence, indent="        ")
        source = value.get("failure_evidence_source")
        failure_evidence = (
            value.get("failure_evidence")
            if source in ("git_integrity", "acceptance")
            else _first_detail_value(
                value, ("required_check_failure_evidence", "failure_evidence")
            )
        )
        if isinstance(failure_evidence, dict):
            lines.extend(_failure_evidence_lines(source, failure_evidence))
        return lines

    return lines


def _failure_evidence_lines(source: object, evidence: dict[str, Any]) -> list[str]:
    """Render the selected failure category, preserving its own evidence boundary."""
    lines: list[str] = []
    if source == "git_integrity":
        label = _copy('presentation.history.git_integrity_failure_evidence')
        lines = _git_integrity_detail_lines(evidence, indent="        ")
    elif source == "acceptance":
        label = _copy('presentation.history.acceptance_failure_evidence')
        for lane, check in acceptance_lanes(evidence).items():
            _append_detail_scalar(
                lines, indent="        ", label=_acceptance_lane_label(lane),
                value=human_status_term(check["status"]),
            )
            _append_detail_scalar(
                lines, indent="          ", label=_copy('presentation.history.evidence'),
                value=check["evidence"],
            )
            for finding in check["findings"]:
                if finding.strip():
                    lines.extend(_full_finding_lines(finding, indent="          "))
    else:
        label = _copy('presentation.history.check_failure_evidence')
        _append_check_observation_detail(
            lines, evidence, indent="        ",
            result_label=_copy('presentation.history.result_95'),
        )
    return [f"      {label}", *lines] if lines else []


def _acceptance_lane_label(lane: str) -> str:
    return _copy("presentation.history." + {
        "e2e": "functional_verification",
        "standards": "engineering_review",
        "spec": "requirements_verification",
    }[lane])


def _record_detail_lines(record: dict[str, Any], *, timezone: tzinfo) -> list[str]:
    lines: list[str] = []
    summary = record.get("development_summary")
    if isinstance(summary, str) and summary.strip():
        lines.extend([_copy('presentation.history.development_summary'), f"      {_safe_text(summary)}"])
    elif record.get("role") == "development":
        lines.append(_copy('presentation.history.development_summary_not_recorded'))
    artifact = record.get("acceptance_artifact")
    if isinstance(artifact, dict):
        checks = artifact.get("checks")
        if not _artifact_is_structured(artifact):
            lines.append(
                _copy('presentation.history.original_acceptance_evidence')
                + _safe_text(json.dumps(artifact, ensure_ascii=False, default=str))
            )
        if isinstance(checks, dict):
            lines.append(_copy('presentation.history.acceptance_evidence'))
            lanes = acceptance_lanes(artifact)
            for lane in ("e2e", "standards", "spec"):
                label = _acceptance_lane_label(lane)
                check = lanes.get(lane)
                if not isinstance(check, dict):
                    lines.append(_copy('presentation.history.not_recorded_127', v1=label))
                    continue
                lines.append(
                    f"      {label}：{_safe_text(human_status_term(check.get('status') or _copy('presentation.history.unknown')))}"
                )
                evidence = check.get("evidence")
                if isinstance(evidence, str) and evidence.strip():
                    lines.append(_copy('presentation.history.evidence_128', v1=_safe_text(evidence)))
                for finding in check["findings"]:
                    lines.extend(_full_finding_lines(finding, indent="        "))
    publication = record.get("publication")
    if isinstance(publication, dict):
        lines.append(_copy('presentation.history.publication_narrative'))
        for key, label in (
            ("commit_message", _copy('presentation.history.commit_message')),
            ("pr_title", _copy('presentation.history.pr_title')),
            ("pr_body_markdown", _copy('presentation.history.pr_body')),
        ):
            value = publication.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"      {label}：{_safe_text(value)}")
        for key, label in (
            ("pr_number", _copy('presentation.history.pr_number')),
        ):
            value = publication.get(key)
            if value is not None:
                lines.append(f"      {label}：{_safe_text(value)}")
    invocations = record.get("invocations")
    if isinstance(invocations, list) and invocations:
        lines.append(_copy('presentation.history.round_execution_records'))
        for index, invocation in enumerate(invocations, start=1):
            if not isinstance(invocation, dict):
                continue
            started = _format_local_timestamp(invocation.get("started_at"), timezone)
            ended_value = invocation.get("ended_at")
            is_current_running_invocation = (
                record.get("activity") == "running" and index == len(invocations)
            )
            ended = (
                _format_local_timestamp(ended_value, timezone)
                if ended_value is not None
                else (_copy('presentation.history.in_progress') if is_current_running_invocation else _copy('presentation.history.unknown_time'))
            )
            duration = _invocation_duration(
                invocation,
                {"executor_control": {"activity": "running"}}
                if is_current_running_invocation
                else {},
            )
            lines.append(
                _copy('presentation.history.status_duration', v1=index, v3=started, v5=ended, v7=_safe_text(human_status_term(invocation.get('status') or _copy('presentation.history.not_recorded'))), v9=_duration(duration))
            )
            lines.append(
                _copy('presentation.history.model_reasoning_effort_135', v1=_safe_text(invocation.get('model') or _copy('presentation.history.not_recorded')), v3=_safe_text(invocation.get('reasoning_effort') or _copy('presentation.history.not_recorded')))
            )
            if isinstance(invocation.get("attempt_count"), int) and invocation["attempt_count"] > 1:
                lines.append(_copy('presentation.history.output_continuations', v1=_output_continuation_text(invocation['attempt_count'])))
            elif invocation.get("attempt_count") is None:
                lines.append(_copy('presentation.history.output_continuation_count_not_recorded'))
            for key, label in (
                ("error", _copy('presentation.history.error')),
                ("validation_error", _copy('presentation.history.validation_error')),
                ("interruption_observed_at", _copy('presentation.history.interruption_observed_at')),
                ("last_failure_at", _copy('presentation.history.last_failure_at')),
                ("signal", _copy('presentation.history.signal')),
                ("return_code", _copy('presentation.history.return_code')),
            ):
                value = invocation.get(key)
                if value is not None and (
                    not isinstance(value, str) or value.strip()
                ):
                    if key in {"interruption_observed_at", "last_failure_at"}:
                        value = _format_local_timestamp(value, timezone)
                    lines.append(f"        {label}：{_safe_text(value)}")
    supporting = record.get("supporting_records")
    if isinstance(supporting, list):
        for item in supporting:
            if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
                continue
            kind = str(item.get("kind"))
            lines.append(f"    {_supporting_label(kind)}")
            lines.extend(_supporting_record_detail_lines(kind, item["value"]))
    return lines


def _cleanup_history_lines(state: dict[str, Any], *, details: bool) -> list[str]:
    cleanup = state.get("delivery_cleanup")
    if not isinstance(cleanup, dict):
        return []
    items = cleanup.get("items")
    pending_items = [
        item
        for item in items.values()
        if isinstance(item, dict) and item.get("status") != "completed"
    ] if isinstance(items, dict) else []
    status = cleanup.get("status")
    last_error = cleanup.get("last_error")
    if status == "completed" and not last_error and not pending_items:
        return []
    lines = [_copy('presentation.history.status', v1=_cleanup_status_text(status))]
    if last_error is not None:
        value = _safe_text(last_error)
        lines.append(
            _copy('presentation.history.last_error', v1=value if details else _truncate_history_detail(value))
        )
    for item in pending_items:
        branch = item.get("branch") or _copy('presentation.history.branch_not_recorded')
        checkout = item.get("checkout") or _copy('presentation.history.workspace_not_recorded')
        lines.append(
            _copy('presentation.history.pending_cleanup_branch_workspace_status', v1=_safe_text(branch), v3=_safe_text(checkout), v5=_cleanup_status_text(item.get('status')))
        )
        if details:
            recovery_kind = item.get("recovery_kind")
            if recovery_kind == "stale_dirty_checkout":
                lines.append(_copy('presentation.history.retained_because_the_workspace_has_uncommitted_changes'))
            item_error = item.get("last_error")
            if item_error is not None:
                lines.append(_copy('presentation.history.item_error', v1=_safe_text(item_error)))
    return lines


def _cleanup_status_text(value: object) -> str:
    return {
        "pending": _copy('presentation.history.pending_151'),
        "cleanup_pending": _copy('presentation.history.awaiting_cleanup'),
        "completed": _copy('presentation.history.completed'),
    }.get(str(value or ""), _safe_text(value or _copy('presentation.history.not_recorded')))


def _turning_point_evidence_lines(
    point: dict[str, Any], *, timezone: tzinfo, details: bool
) -> list[str]:
    lines: list[str] = []
    if point.get("pr_number") is not None and point.get("kind") != "pr_creation":
        lines.append(_copy('presentation.history.pr_number_153', v1=_safe_text(point['pr_number'])))
    if (
        point.get("kind") not in {"required_checks", "pr_creation"}
        and point.get("required_checks_signature") is None
        and point.get("required_checks_result") is not None
    ):
        # Older records may only retain this result on the completion event.
        # Without an observation identity, do not assume a separate check
        # record already carries it.
        lines.append(_copy('presentation.history.pre_merge_check_result_154', v1=_safe_text(human_status_term(point['required_checks_result']))))
    observed_at = point.get("required_checks_observed_at")
    if details and point.get("kind") == "required_checks" and observed_at is not None:
        lines.append(
            _copy('presentation.history.check_result_observed_at_155', v1=_format_local_timestamp(observed_at, timezone))
        )
    next_action = point.get("next_action")
    if next_action is not None:
        value = _safe_text(next_action)
        if not details:
            value = _truncate_history_detail(value)
        lines.append(_copy('presentation.history.next_step_156', v1=value))
    evidence = point.get("required_checks_evidence")
    if point.get("kind") == "required_checks" and isinstance(evidence, dict) and (
        evidence.get("checks") or evidence.get("omitted_checks")
    ):
        checks = evidence.get("checks")
        if isinstance(checks, list):
            names = dict.fromkeys(
                str(check["name"]) for check in checks
                if isinstance(check, dict) and check.get("name")
            )
            if names:
                lines.append(_copy('presentation.history.check_names', v1=_safe_text('、'.join(names))))
        if details:
            lines.append(_copy('presentation.history.pre_merge_check_evidence_158'))
            _append_check_observation_detail(lines, evidence, indent="          ")
        if details and evidence.get("omitted_checks"):
            lines.append(_copy('presentation.history.details_omitted_for_more_checks', v1=evidence['omitted_checks']))
    reason = point.get("external_wait_reason")
    if (
        isinstance(reason, dict) and reason.get("message")
        and not (
            reason.get("code") == "github_write_pending"
            and point.get("github_write_action") in {
                "ensure_final_run_ref", "create_final_pr", "refresh_final_pr_narrative",
            }
        )
    ):
        lines.append(_copy('presentation.history.external_situation', v1=_safe_text(reason['message'])))
    return lines


def _json_detail_lines(value: object, *, indent: str) -> list[str]:
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    except (TypeError, ValueError):
        serialized = _safe_text(value)
    return [f"{indent}{_safe_text(line)}" for line in serialized.splitlines()]


def _artifact_is_structured(artifact: dict[str, Any]) -> bool:
    checks = artifact.get("checks")
    if not isinstance(checks, dict) or set(checks) != {"e2e", "standards", "spec"}:
        return False
    return all(
        isinstance(check, dict)
        and isinstance(check.get("findings"), list)
        and all(isinstance(finding, str) for finding in check["findings"])
        for check in checks.values()
    )


def _full_finding_lines(finding: str, *, indent: str) -> list[str]:
    match = _FINDING.fullmatch(finding)
    if match is None:
        return [_copy('presentation.history.original_finding', v0=indent, v2=_safe_text(finding))]
    lines = [
        f"{indent}{label}：{_safe_text(value)}"
        for label, value in zip((_copy('presentation.history.finding'), _copy('presentation.history.evidence'), _copy('presentation.history.required_fix'), _copy('presentation.history.reverification')), match.groups())
    ]
    return lines


def _finding_question(finding: object) -> str:
    if not isinstance(finding, str):
        return _copy('presentation.history.original_finding_165', v1=_safe_text(finding))
    match = _FINDING.fullmatch(finding)
    return (
        _copy('presentation.history.finding_166', v1=_safe_text(match.group(1)))
        if match is not None
        else _copy('presentation.history.original_finding_165', v1=_safe_text(finding))
    )


def _record_action(record: dict[str, Any]) -> str:
    role = _role_family(str(record.get("role") or ""))
    return {
        "development": _copy('presentation.history.implement_task'),
        "review": _copy('presentation.history.review_code'),
        "publication": _copy('presentation.history.write_commit_and_pr_narrative'),
    }.get(role or "", _copy('presentation.history.delivery_status_changed'))


def _turning_point_kind(event: dict[str, Any]) -> str:
    return {
        "human_blocker": _copy('presentation.history.human_blocker'),
        "supervision": _copy('presentation.history.confirm_external_operation_outcome'),
        "required_checks": _copy('presentation.history.pre_merge_checks'),
        "integration": _copy('presentation.history.integration'),
        "publication": _copy('presentation.history.publication'),
        "approval": _copy('presentation.history.human_approval'),
        "abandonment": _copy('presentation.history.abandonment'),
        "completion": _copy('presentation.history.delivery_completion'),
        "resume": _copy('presentation.history.resume'),
        "unsupported_scope_change": _copy('presentation.history.scope_change'),
    }.get(str(event.get("kind") or ""), _copy('presentation.history.status_change'))


def _turning_point_status(event: dict[str, Any]) -> str:
    if event.get("kind") == "approval":
        return _copy('presentation.history.approved') if event.get("approval_granted_at") else _copy('presentation.history.awaiting_human_approval')
    if event.get("kind") == "required_checks":
        if event.get("required_checks_observation_status") in {"unavailable", "unknown"}:
            return _copy('presentation.history.pre_merge_check_results_unavailable')
        return {
            "pending": _copy('presentation.history.awaiting_pre_merge_checks'), "pass": _copy('presentation.history.pre_merge_checks_passed'),
            "none": _copy('presentation.history.no_pre_merge_checks_configured'), "fail": _copy('presentation.history.pre_merge_checks_failed'),
            "unknown": _copy('presentation.history.pre_merge_check_results_unknown'),
        }.get(str(event.get("required_checks_result")), _copy('presentation.history.pre_merge_check_results_unknown'))
    if event.get("kind") == "resume":
        return _copy('presentation.history.human_resume_continue')
    if event.get("activity") == "interrupted":
        return _copy('presentation.history.execution_interrupted_waiting_to_resume')
    if event.get("activity") == "unknown":
        return _copy('presentation.history.execution_status_unknown')
    if event.get("activity") == "capacity_wait":
        return _copy('presentation.history.model_capacity_unavailable_awaiting_automatic_continuation')
    if event.get("activity") == "recovery_wait":
        return _copy('presentation.history.execution_error_awaiting_automatic_continuation')
    return str(human_status_term(event.get("status") or _copy('presentation.history.status_change')))


def _format_local_timestamp(value: object, timezone: tzinfo) -> str:
    timestamp = _parse_optional_timestamp(value)
    if timestamp is None:
        return _copy('presentation.history.unknown_time')
    return timestamp.astimezone(timezone).strftime("%Y-%m-%d %H:%M %z")


def _execution_activity_text(activity: str) -> str:
    return {
        "running": _copy('presentation.history.executing'),
        "interrupted": _copy('presentation.history.execution_interrupted_waiting_to_resume'),
        "unknown": _copy('presentation.history.execution_status_unknown'),
        "capacity_wait": _copy('presentation.history.model_capacity_unavailable_awaiting_automatic_continuation'),
        "recovery_wait": _copy('presentation.history.execution_error_awaiting_automatic_continuation'),
        "not_running": _copy('presentation.history.not_executing'),
    }.get(activity, _copy('presentation.history.unknown'))


def _round_summary_text(rounds: object) -> str:
    if not isinstance(rounds, dict) or not rounds:
        return _copy('presentation.history.no_agent_rounds_yet_193')
    return "；".join(_copy('presentation.history.rounds_194', v0=_round_label(str(key)), v2=value) for key, value in rounds.items())


def _rich_line(label: object, value: object) -> Any:
    from rich.text import Text

    line = Text()
    line.append(f"{_safe_text(label)}：", style="bold")
    line.append(_safe_text(_copy('presentation.history.unknown') if value is None else value))
    return line


def _safe_text(value: object) -> str:
    return terminal_safe(value)


_FINDING = re.compile(
    r"问题：(.*?)；证据：(.*?)；必须修复：(.*?)；复验：(.*)", re.DOTALL
)

def _history_events(
    state: dict[str, Any], audit: dict[str, Any]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timeline_sources = (audit.get("timeline"), audit.get("timeline_continuation"))
    source_order = 0
    for timeline in timeline_sources:
        if not isinstance(timeline, list):
            continue
        for raw in timeline:
            if not isinstance(raw, dict) or not isinstance(raw.get("at"), str):
                continue
            timestamp = utc_timestamp(raw["at"])
            if timestamp is None:
                continue
            blockers = raw.get("human_blockers")
            details = (
                [item for item in blockers if isinstance(item, str)]
                if isinstance(blockers, list)
                else []
            )
            kind = timeline_kind(raw, details)
            details.extend(scope_change_details(raw))
            worker = raw.get("worker")
            events.append(
                {
                    "at": timestamp,
                    "kind": kind,
                    "object": timeline_object(state, raw),
                    "phase": raw.get("phase"),
                    "status": raw.get("status"),
                    "role": (
                        _machine_role_label(worker)
                        if isinstance(worker, str)
                        else worker
                    ),
                    "round": raw.get("attempt"),
                    "details": details,
                    "_order": source_order,
                }
            )
            source_order += 1

    invocations = audit.get("agent_invocations")
    if isinstance(invocations, list):
        for order, invocation in enumerate(invocations, start=len(events)):
            if not isinstance(invocation, dict):
                continue
            raw_at = invocation.get("ended_at") or invocation.get("started_at")
            timestamp = utc_timestamp(raw_at) if isinstance(raw_at, str) else None
            if timestamp is None:
                continue
            attempt = invocation.get("semantic_attempt")
            attempt_view = invocation_attempt_view(invocation)
            work_subject = str(invocation.get("work_subject") or "")
            role = str(
                attempt_view.get("role")
                or invocation.get("invocation_role")
                or invocation.get("role")
                or "unknown"
            )
            round_number = attempt_view.get("ordinal")
            artifact = review_artifact_for_attempt(
                owners_for_attempt(state, attempt_view),
                attempt_view,
                [invocation],
                state=state,
            )
            review_details = (
                _artifact_findings(artifact)
                if isinstance(artifact, dict)
                else []
            )
            status = invocation.get("status")
            if review_details:
                status = "fail"
            events.append(
                {
                    "at": timestamp,
                    "kind": _invocation_kind(role),
                    "object": subject_label(state, work_subject),
                    "phase": invocation.get("phase"),
                    "status": status,
                    # Keep the historical JSON contract stable. Localized
                    # labels belong only to the human renderer below.
                    "role": _machine_role_label(role),
                    "round": round_number,
                    "invocation_role": invocation.get("invocation_role"),
                    "binding_role": invocation.get("binding_role"),
                    "model": invocation.get("model"),
                    "reasoning_effort": invocation.get("reasoning_effort"),
                    "profile_revision": invocation.get("profile_revision"),
                    "duration_seconds": _invocation_duration(invocation, audit),
                    "activity": invocation_activity(invocation, audit),
                    "details": review_details + invocation_recovery_details(invocation),
                    "_order": order,
                }
            )

    responses = human_responses_by_subject(state)
    response_audit = state.get("human_response_audit")
    audited_responses = response_audit if isinstance(response_audit, dict) else {}
    resumes = audit.get("agent_resumes")
    if isinstance(resumes, list):
        for order, resume in enumerate(resumes, start=len(events)):
            if not isinstance(resume, dict):
                continue
            raw_at = resume.get("requested_at")
            timestamp = utc_timestamp(raw_at) if isinstance(raw_at, str) else None
            if timestamp is None:
                continue
            response_details: list[str] = []
            if resume.get("human_response_supplied") is True:
                audited_response = audited_responses.get(resume.get("resume_id"))
                if isinstance(audited_response, str):
                    response_details.append(audited_response)
                else:
                    subject_responses = responses.get(
                        (
                            str(resume.get("work_subject") or ""),
                            resume.get("generation"),
                        )
                    )
                    if subject_responses:
                        response_details.append(subject_responses.pop(0))
            events.append(
                {
                    "at": timestamp,
                    "kind": "resume",
                    "object": subject_label(state, str(resume.get("work_subject") or "")),
                    "phase": resume.get("source_status"),
                    "status": "resumed",
                    "role": None,
                    "round": None,
                    "details": response_details,
                    "_order": order,
                }
            )

    events.sort(key=lambda event: (event["at"], event["_order"]))
    for event in events:
        event.pop("_order", None)
    return events


def _round_summary(attempts: object) -> dict[str, int]:
    if not isinstance(attempts, list):
        return {}
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id in seen:
            continue
        if isinstance(attempt_id, str):
            seen.add(attempt_id)
        key = _round_key(
            str(attempt.get("work_subject") or ""),
            str(attempt.get("role") or ""),
        )
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _round_key(subject: str, role: str) -> str | None:
    if subject.startswith("ticket:"):
        scope = "ticket"
    elif subject.startswith("parent-only:"):
        scope = "parent"
    elif subject.startswith("run-"):
        scope = "run"
    else:
        scope = None
    role_key = _role_family(role)
    return f"{scope}_{role_key}" if scope and role_key else None


def _invocation_kind(role: str) -> str:
    return _role_family(role) or "agent_invocation"


def _role_label(role: str) -> str:
    family = _role_family(role)
    if family is None:
        return role
    return {
        "development": _copy('presentation.history.development_agent'),
        "review": _copy('presentation.history.acceptance_agent'),
        "publication": _copy('presentation.history.publication_agent'),
    }[family]


def _machine_role_label(role: str) -> str:
    """Return the stable JSON role value for a semantic role alias."""

    family = _role_family(role)
    if family is None:
        return role
    return {
        "development": "Development Agent",
        "review": "Review Agent",
        "publication": "Publication Agent",
    }[family]


def _role_zh_label(role: str) -> str:
    return {
        "development": _copy('presentation.history.development'),
        "review": _copy('presentation.history.acceptance'),
        "publication": _copy('presentation.history.publication'),
    }.get(_role_family(role) or "", "Agent")


def _supporting_label(kind: str) -> str:
    return {
        "required_checks_evidence": _copy('presentation.history.pre_merge_check_evidence_202'),
        "deterministic_integration_record": _copy('presentation.history.pr_merge_record'),
        "fallback_publication_receipt": _copy('presentation.history.fallback_publication_record'),
    }.get(kind, kind)


def _round_label(key: str) -> str:
    scope, _, role = key.partition("_")
    subject = {"ticket": _copy('presentation.history.subtask'), "parent": _copy('presentation.history.overall_task'), "run": _copy('presentation.history.overall')}.get(scope, _copy('presentation.history.task'))
    action = {"development": _copy('presentation.history.development'), "review": _copy('presentation.history.acceptance'), "publication": _copy('presentation.history.publication_narrative_208')}.get(role, _copy('presentation.history.execution'))
    return _copy("presentation.history.round_summary_label", subject=subject, action=action)


def _invocation_duration(
    invocation: dict[str, Any], audit: dict[str, Any]
) -> int | None:
    return invocation_execution_seconds(
        invocation,
        allow_open=invocation_activity(invocation, audit)
        in {"running", "capacity_wait", "recovery_wait"},
    )


def _elapsed_seconds(state: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    return run_elapsed_seconds(state, events=events)


def _local_timezone() -> tuple[tzinfo, str]:
    configured = os.environ.get("TZ")
    if configured:
        try:
            timezone: tzinfo = ZoneInfo(configured)
        except (ZoneInfoNotFoundError, ValueError):
            return UTC, "UTC (UTC+00:00)"
        return timezone, f"{configured} ({_utc_offset(timezone)})"
    local = datetime.now().astimezone().tzinfo
    if local is None:
        return UTC, "UTC (UTC+00:00)"
    name = getattr(local, "key", None) or datetime.now(local).tzname() or "UTC"
    return local, f"{name} ({_utc_offset(local)})"


def _utc_offset(timezone: tzinfo) -> str:
    offset = datetime.now(timezone).utcoffset()
    if offset is None:
        return "UTC+00:00"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{remainder:02d}"


def _duration(seconds: object) -> str:
    from agent_run.presentation_helpers import execution_duration

    return execution_duration(seconds)


def _output_continuation_text(value: object) -> str:
    if type(value) is int and value >= 1:
        return str(value - 1)
    return _copy('presentation.history.not_recorded')


def _truncate_history_detail(value: str) -> str:
    if len(value) <= HISTORY_DETAIL_LIMIT:
        return value
    marker = _copy('presentation.history.truncated_see_json_for_full_content')
    keep = HISTORY_DETAIL_LIMIT - len(marker)
    return value[:keep] + marker


def _operator_instruction(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    cleanup = cleanup_instruction(state)
    if cleanup:
        return cleanup
    if invocation is not None and invocation.get("status") in {"running", "resuming"}:
        return _copy('presentation.history.no_action_is_needed_for_now')
    if state.get("status") in {"completed", "abandoned"}:
        return _copy('presentation.history.no_action_needed')
    return _copy('presentation.history.continue_with_the_command_above_do_not_start_the_same_task_again')


def _current_wait_lines(state: dict[str, Any], audit: dict[str, Any]) -> list[str]:
    waiting = waiting_presentation(state, audit)
    if waiting is None:
        return []
    lines = [_copy('presentation.history.current_work', v1=_safe_text(waiting.work)), _copy('presentation.history.execution_214', v1=_safe_text(waiting.activity))]
    lines.extend(f"{label}：{_safe_text(value)}" for label, value in waiting.details)
    lines.append(_copy('presentation.history.next_step_215', v1=_safe_text(waiting.guidance)))
    if waiting.show_command:
        lines.append(_copy('presentation.history.command', v1=human_next_action(audit.get('next_action'), run_id=state.get('run_id'))))
    return lines
