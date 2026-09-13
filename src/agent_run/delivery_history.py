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
    invocation_activity,
    invocation_execution_seconds,
    invocation_recovery_details,
)
from agent_run.history_check_facts import CHECKS_HISTORY_FIELDS
from agent_run.history_publication import group_final_pr_creation
from agent_run.history_waits import collapse_check_waits
from agent_run.operator_action_presentation import human_action_type, human_preserved_results
from agent_run.presentation_helpers import (
    delivery_object_label,
    human_delivery_object,
    human_pause_reason,
    human_status_term,
    human_next_action,
    terminal_safe,
)
from agent_run.waiting_presentation import cleanup_instruction, waiting_presentation


HISTORY_DETAIL_LIMIT = 240
_TRUNCATION_MARKER = "…（已截断；完整内容见 --json）"


def _history_turning_points(
    state: dict[str, Any], audit: dict[str, Any]
) -> list[dict[str, Any]]:
    """Project persisted lifecycle facts used as timeline turning points.

    Ordinary timeline snapshots are deliberately omitted.  They describe the
    same Attempt repeatedly and must not become extra human-facing rounds.
    The private attempt id is used only for this renderer; it is not added to
    the public JSON projection.
    """

    points: list[dict[str, Any]] = []
    timeline_events: list[dict[str, Any]] = []
    order = 0
    for timeline in (audit.get("timeline"), audit.get("timeline_continuation")):
        if not isinstance(timeline, list):
            continue
        for raw in timeline:
            if not isinstance(raw, dict) or not isinstance(raw.get("at"), str):
                continue
            timestamp = _utc_timestamp(raw["at"])
            if timestamp is None:
                continue
            blockers = raw.get("human_blockers")
            details = (
                [item for item in blockers if isinstance(item, str)]
                if isinstance(blockers, list)
                else []
            )
            kind = _human_timeline_kind(raw, details)
            if (
                kind == "publication" and raw.get("semantic_attempt_id")
                and raw.get("status") == "run_publication_pending"
                and raw.get("pr_number") is None
            ):
                continue
            details.extend(_scope_change_details(raw, human=True))
            event = {
                "at": timestamp,
                "kind": kind,
                "object": (
                    _subject_label(state, raw["history_work_subject"])
                    if isinstance(raw.get("history_work_subject"), str)
                    else _timeline_object(state, raw)
                ),
                "phase": raw.get("phase"),
                "status": raw.get("status"),
                "role": raw.get("worker"),
                "round": raw.get("attempt"),
                "details": details,
                "semantic_attempt_id": raw.get("semantic_attempt_id"),
                "_order": order,
            }
            for key in (
                "pr_number",
                "commit_sha",
                "thread_id",
                "approval_granted_at",
                "required_checks_result",
                "required_checks_observed_at",
                "required_checks_evidence",
                "next_action",
                "result",
                "explicit_resume_sequence",
                "explicit_resume_kind",
                "explicit_resume_thread_id",
                "explicit_resume_attempt_id",
                *CHECKS_HISTORY_FIELDS,
            ):
                if raw.get(key) is not None:
                    event[key] = raw[key]
            timeline_events.append(event)
            order += 1

    timeline_events.sort(key=lambda event: (event["at"], event["_order"]))
    points.extend(_collapse_human_blocker_snapshots(timeline_events))

    responses = _human_responses_by_subject(state)
    response_audit = state.get("human_response_audit")
    audited_responses = response_audit if isinstance(response_audit, dict) else {}
    resumes = audit.get("agent_resumes")
    if isinstance(resumes, list):
        for resume in resumes:
            if not isinstance(resume, dict):
                continue
            raw_at = resume.get("requested_at")
            timestamp = _utc_timestamp(raw_at) if isinstance(raw_at, str) else None
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
            points.append(
                {
                    "at": timestamp,
                    "kind": "resume",
                    "object": _subject_label(
                        state, str(resume.get("work_subject") or "")
                    ),
                    "phase": resume.get("source_status"),
                    "status": "resumed",
                    "role": None,
                    "round": None,
                    "details": response_details,
                    "semantic_attempt_id": resume.get("semantic_attempt_id"),
                    "resume_id": resume.get("resume_id"),
                    "_order": order,
                }
            )
            order += 1
    points.sort(key=lambda event: (event["at"], event["_order"]))
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point in points:
        identity = _history_turning_point_identity(point)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(point)
    points = deduplicated
    for point in points:
        point.pop("_order", None)
        point.pop("_human_blocker_occurrence", None)
    return group_final_pr_creation(collapse_check_waits(points))


def _collapse_human_blocker_snapshots(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one human pause per blocked interval in the human timeline.

    Resume authorization is persisted before the old blocker is cleared. The
    resulting ready-for-human snapshot has a newer resume sequence but still
    belongs to the same blocked interval. A later pause is distinct only
    after a persisted non-ready snapshot proves that the interval ended.
    """

    active_scopes: set[tuple[str, str, str, str]] = set()
    occurrences: dict[tuple[str, str, str, str], int] = {}
    turning_points: list[dict[str, Any]] = []
    for event in events:
        scope = _human_blocker_scope(event)
        details = event.get("details")
        is_blocker = (
            isinstance(details, list)
            and bool(details)
            and _is_human_blocker_event(event)
        )
        if is_blocker:
            if scope in active_scopes:
                continue
            active_scopes.add(scope)
            event["_human_blocker_occurrence"] = occurrences.get(scope, 0)
            occurrences[scope] = occurrences.get(scope, 0) + 1
        elif str(event.get("status") or "") != "ready_for_human":
            active_scopes.discard(scope)
        if _is_history_turning_point(event):
            turning_points.append(event)
    return turning_points


def _human_blocker_scope(event: dict[str, Any]) -> tuple[str, str, str, str]:
    semantic_attempt_id = str(event.get("semantic_attempt_id") or "")
    return (
        str(event.get("object") or ""),
        semantic_attempt_id,
        "" if semantic_attempt_id else str(event.get("role") or ""),
        "" if semantic_attempt_id else str(event.get("round") or ""),
    )


def _history_turning_point_identity(point: dict[str, Any]) -> str:
    """Deduplicate repeated internal snapshots without hiding operations.

    Timeline snapshots can differ only because an invocation counter or its
    timestamp changed.  Those are not user-visible business transitions.  A
    concrete publication/check state remains distinct by its business fields;
    integration records with the same PR and commit are one integration even
    when persistence observes both the merged and completed phases. Human
    blockers are keyed by their proven blocked interval so repeated operator
    actions and later real pauses stay visible.
    """

    resume_id = point.get("resume_id")
    if point.get("kind") == "resume" and isinstance(resume_id, str) and resume_id:
        return json.dumps(
            {"kind": "resume", "resume_id": resume_id},
            ensure_ascii=False,
            sort_keys=True,
        )
    kind = point.get("kind")
    can_deduplicate_snapshot = (
        kind == "publication"
        and any(
            point.get(key) is not None
            for key in (
                "semantic_attempt_id",
                "round",
                "thread_id",
                "pr_number",
                "commit_sha",
            )
        )
    )
    if kind == "human_blocker":
        # A ready_for_human marker is persisted more than once while the
        # controller waits. The projection assigns an occurrence after a
        # proven non-ready boundary, so a later real pause remains visible
        # even when it has the same resume sequence and blocker text.
        return json.dumps(
            {
                "kind": kind,
                "object": point.get("object"),
                "semantic_attempt_id": point.get("semantic_attempt_id"),
                "round": point.get("round"),
                "role": point.get("role"),
                "phase": point.get("phase"),
                "status": point.get("status"),
                "details": point.get("details"),
                "human_blocker_occurrence": point.get(
                    "_human_blocker_occurrence"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    if can_deduplicate_snapshot:
        return json.dumps(
            {
                "kind": kind,
                "object": point.get("object"),
                "semantic_attempt_id": point.get("semantic_attempt_id"),
                "round": point.get("round"),
                "role": point.get("role"),
                "thread_id": point.get("thread_id"),
                "phase": point.get("phase"),
                "status": point.get("status"),
                "details": point.get("details"),
                "pr_number": point.get("pr_number"),
                "commit_sha": point.get("commit_sha"),
                "approval_granted_at": point.get("approval_granted_at"),
                "required_checks_result": point.get("required_checks_result"),
                "required_checks_observed_at": point.get(
                    "required_checks_observed_at"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    if kind == "integration":
        pr_number = point.get("pr_number")
        commit_sha = point.get("commit_sha")
        if pr_number is not None or commit_sha is not None:
            return json.dumps(
                {
                    "kind": "integration",
                    "object": point.get("object"),
                    "pr_number": pr_number,
                    "commit_sha": commit_sha,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
    return json.dumps(
        {key: value for key, value in point.items() if key != "_order"},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def history_progress_view(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    events = _history_events(state, audit)
    return {
        "events": events,
        "execution_activity": invocation_activity(state.get("active_agent_invocation"), audit),
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
    print(f"仓库:       {_safe_text(state.get('repository') or '未知')}")
    print(
        "整体需求:   "
        f"#{_safe_text(parent_view.get('number', '?'))} "
        f"{_safe_text(parent_view.get('title') or '未命名整体需求')}"
    )
    print(f"时区:       {timezone_label}")
    print(f"任务历时:   {_duration(progress['summary']['elapsed_seconds'])}")
    executor_control = audit.get("executor_control")
    if (
        isinstance(executor_control, dict)
        and executor_control.get("activity") == "unknown"
    ):
        print("Agent 活跃状态: 无法确认（运行状态无法确认）")
    if progress["execution_activity"] == "interrupted":
        print("执行已中断，等待恢复")
    records = history_records(state, audit, progress["events"])
    print("\n工作时间线")
    if records:
        for record in records:
            for line in _record_lines(record, timezone=timezone, details=details):
                print(line)
    else:
        print("  尚无可确认的 Agent 工作记录")
    cleanup_lines = _cleanup_history_lines(state, details=details)
    if cleanup_lines:
        print("\n交付清理")
        for line in cleanup_lines:
            print(line)

    print("\n汇总")
    rounds = progress["summary"]["rounds"]
    if rounds:
        for key, count in rounds.items():
            print(f"  {_round_label(key):<22}{count} 轮")
    else:
        print("  尚无 Agent 轮次")
    print(
        f"  {'总运行时长':<22}"
        f"{_duration(progress['summary']['elapsed_seconds'])}"
    )
    waiting_lines = _current_wait_lines(state, audit)
    if waiting_lines:
        print("\n" + "\n".join(waiting_lines))
        return
    if progress["execution_activity"] in {
        "interrupted",
        "unknown",
        "capacity_wait",
        "recovery_wait",
    }:
        print(f"\n下一步: {execution_guidance(state, progress['execution_activity'])}")
        return
    operator_action = audit.get("operator_action")
    if isinstance(operator_action, dict):
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
            f"\n下一步: {human_next_action(audit.get('next_action'), run_id=state.get('run_id'))}；"
            f"{_operator_instruction(state, None)}"
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
            "任务",
            f"{state.get('repository') or 'unknown'} · "
            f"#{parent_view.get('number', '?')} "
            f"{parent_view.get('title') or '未命名整体需求'}",
        ),
        _rich_line("时区", timezone_label),
        _rich_line("任务历时", _duration(progress["summary"]["elapsed_seconds"])),
    ]
    if progress.get("execution_activity") in {
        "interrupted",
        "unknown",
        "capacity_wait",
        "recovery_wait",
    }:
        header.append(
            _rich_line(
                "活跃状态",
                _execution_activity_text(str(progress["execution_activity"])),
            )
        )

    body: list[Text] = [*header, Text(""), Text("工作时间线", style="bold")]
    for record in records:
        body.extend(
            Text(line) for line in _record_lines(record, timezone=timezone, details=details)
        )
        body.append(Text(""))
    if not records:
        body.append(Text("  尚无可确认的 Agent 工作记录"))
    cleanup_lines = _cleanup_history_lines(state, details=details)
    if cleanup_lines:
        body.extend([Text(""), Text("交付清理", style="bold")])
        body.extend(Text(line) for line in cleanup_lines)
    action = audit.get("operator_action")
    if isinstance(action, dict):
        body.extend([Text(""), Text("需要你处理", style="bold yellow")])
        body.append(Text(f"  类型：{_safe_text(human_action_type(action.get('type')))}"))
        reasons = action.get("reasons")
        if isinstance(reasons, list):
            for reason in reasons:
                value = _safe_text(
                    reason if action.get("type") == "Human Blocker" else human_pause_reason(reason)
                )
                if not details:
                    value = _truncate_history_detail(value)
                body.append(Text(f"  原因：{value}"))
        preserved = action.get("preserved")
        if preserved is not None:
            body.append(Text(f"  已保留成果：{_safe_text(human_preserved_results(preserved))}"))
        phase = action.get("phase")
        if phase is not None:
            body.append(Text(f"  所在阶段：{_safe_text(human_status_term(phase))}"))
        body.append(Text("整项任务已暂停，其他子任务也不会继续。"))
    body.extend(
        [
            Text(""),
            Text("汇总", style="bold"),
            Text(
                f"  Agent 轮次：{_round_summary_text(progress['summary']['rounds'])}"
            ),
            Text(
                f"  总运行时长：{_duration(progress['summary']['elapsed_seconds'])}"
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
                    "下一步",
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
                    "下一步",
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
            title=Text("agent-run · 工作时间线", style="bold"),
            border_style=border_style,
            expand=True,
        ),
        overflow="fold",
        crop=False,
    )


def history_records(
    state: dict[str, Any],
    audit: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate raw snapshots and invocations into one record per Attempt.

    This is a read-only projection.  Raw timeline events remain in the JSON
    audit output; only the human renderer consumes this compact view.
    """

    attempts = audit.get("semantic_agent_attempts")
    invocations = audit.get("agent_invocations")
    attempt_values = attempts if isinstance(attempts, list) else []
    invocation_values = invocations if isinstance(invocations, list) else []
    records_by_key: dict[str, dict[str, Any]] = {}
    for attempt in attempt_values:
        if not isinstance(attempt, dict):
            continue
        _ensure_history_record(records_by_key, attempt=attempt)
    for invocation in invocation_values:
        if not isinstance(invocation, dict):
            continue
        attempt = invocation.get("semantic_attempt")
        attempt_view = attempt if isinstance(attempt, dict) else {}
        _ensure_history_record(
            records_by_key,
            attempt=attempt_view,
            invocation=invocation,
        )["invocations"].append(invocation)

    raw_events = _history_turning_points(state, audit)
    if not raw_events and isinstance(events, list):
        # Keep malformed/legacy state visible when no persisted turning point
        # can be reconstructed from the raw audit fields. Invocation events
        # already belong to their Attempt and must not become duplicate
        # standalone records merely because their review details are long.
        raw_events = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("details")
            and _role_family(str(event.get("kind") or "")) is None
        ]
    event_by_attempt: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        attempt_id = raw.get("semantic_attempt_id")
        if isinstance(attempt_id, str) and raw.get("kind") not in {
            "required_checks", "integration", "completion", "approval", "supervision", "abandonment", "pr_creation",
        }:
            event_by_attempt.setdefault(attempt_id, []).append(raw)
        elif _is_history_turning_point(raw):
            standalone.append(raw)
    for key, record in records_by_key.items():
        attempt_id = record.get("attempt_id")
        if isinstance(attempt_id, str):
            record["turning_points"] = event_by_attempt.get(attempt_id, [])
        else:
            record["turning_points"] = []
        _finalize_history_record(state, record)

    for index, raw in enumerate(standalone):
        record = _ensure_history_record(
            records_by_key,
            attempt={
                "attempt_id": (
                    f"event:{raw.get('at')}:{raw.get('kind')}:{index}"
                ),
                "work_subject": raw.get("object"),
                "role": "event",
                "ordinal": None,
                "event_record": True,
                "event_at": raw.get("at"),
            },
        )
        record.setdefault("turning_points", []).append(raw)
        _finalize_history_record(state, record)

    result = list(records_by_key.values())
    result.sort(key=lambda item: (_record_start(item), _record_order(item)))
    return result


def _ensure_history_record(
    records: dict[str, dict[str, Any]],
    *,
    attempt: dict[str, Any],
    invocation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _attempt_key(attempt, invocation)
    record = records.get(key)
    if record is None:
        record = {
            "attempt_id": attempt.get("attempt_id"),
            "role": attempt.get("role")
            or (invocation or {}).get("role")
            or (invocation or {}).get("invocation_role"),
            "work_subject": attempt.get("work_subject")
            or (invocation or {}).get("work_subject"),
            "generation": attempt.get("generation")
            or (invocation or {}).get("generation"),
            "ordinal": attempt.get("ordinal"),
            "budget_window": attempt.get("budget_window"),
            "attempt": attempt,
            "invocations": [],
            "turning_points": [],
            "event_record": attempt.get("event_record") is True,
            "event_at": attempt.get("event_at"),
        }
        records[key] = record
    elif record.get("attempt") is not attempt and attempt:
        # The persisted Attempt is authoritative. An Invocation contains a
        # startup mirror which may still say "pending" after the Attempt was
        # completed. Only fill genuinely absent identity fields from it.
        authoritative = record.get("attempt")
        if not isinstance(authoritative, dict) or not authoritative:
            record["attempt"] = attempt
        else:
            for key in (
                "attempt_id",
                "role",
                "work_subject",
                "generation",
                "currentness_boundary_fingerprint",
                "ordinal",
                "budget_window",
            ):
                if authoritative.get(key) is None and attempt.get(key) is not None:
                    authoritative[key] = attempt[key]
                    if record.get(key) is None:
                        record[key] = attempt[key]
    return record


def _attempt_key(
    attempt: dict[str, Any], invocation: dict[str, Any] | None = None
) -> str:
    attempt_id = attempt.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return f"id:{attempt_id}"
    values = {
        key: attempt.get(key, (invocation or {}).get(key))
        for key in (
            "role",
            "work_subject",
            "generation",
            "currentness_boundary_fingerprint",
            "ordinal",
            "budget_window",
        )
    }
    return "identity:" + json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)


def _finalize_history_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    if record.get("event_record") is True:
        _finalize_event_record(state, record)
        return
    invocations = [
        invocation
        for invocation in record.get("invocations", [])
        if isinstance(invocation, dict)
    ]
    record["object"] = _subject_label(
        state, str(record.get("work_subject") or "run")
    )
    record["role_label"] = _role_label(str(record.get("role") or "unknown"))
    record["role_zh"] = _role_zh_label(str(record.get("role") or "unknown"))
    starts = [
        timestamp
        for invocation in invocations
        if (timestamp := _parse_optional_timestamp(invocation.get("started_at"))) is not None
    ]
    ends = [
        timestamp
        for invocation in invocations
        if (timestamp := _parse_optional_timestamp(invocation.get("ended_at"))) is not None
    ]
    record["started_at"] = min(starts).isoformat() if starts else None
    has_unclosed_invocation = any(
        invocation.get("ended_at") is None for invocation in invocations
    )
    if has_unclosed_invocation:
        record["ended_at"] = None
    elif ends:
        record["ended_at"] = max(ends).isoformat()
    else:
        record["ended_at"] = None
    record["activity"] = _record_activity(state, record, invocations)
    record["span_seconds"] = _record_span_seconds(record)
    record["execution_seconds"] = _record_execution_seconds(record)
    record["resumption_count"] = sum(
        1 for invocation in invocations if invocation.get("resume_id") is not None
    )
    record["output_attempts"] = [
        invocation.get("attempt_count")
        for invocation in invocations
        if type(invocation.get("attempt_count")) is int
    ]
    record["recovery_details"] = [
        detail
        for invocation in invocations
        for detail in invocation_recovery_details(invocation)
    ]
    record["configurations"] = _record_configurations(invocations)
    details = _history_record_details(state, record)
    record.update(details)
    record["status_text"] = _history_record_status(record)


def _finalize_event_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    """Finalize a lifecycle event without inventing an Agent Attempt."""

    event = next(
        (
            point
            for point in record.get("turning_points", [])
            if isinstance(point, dict)
        ),
        {},
    )
    event_at = event.get("at") or record.get("event_at")
    record["object"] = _safe_text(record.get("work_subject") or "Delivery Run")
    record["role_label"] = "生命周期节点"
    record["role_zh"] = "节点"
    record["started_at"] = event.get("wait_started_at", event_at)
    record["ended_at"] = event.get("wait_observed_until", event_at)
    record["activity"] = "not_running"
    start = _parse_optional_timestamp(record["started_at"])
    end = _parse_optional_timestamp(record["ended_at"])
    record["span_seconds"] = (
        max(0, int((end - start).total_seconds()))
        if event.get("kind") in {"required_checks", "approval", "pr_creation"} and start and end else None
    )
    record["execution_seconds"] = None
    record["resumption_count"] = 0
    record["output_attempts"] = []
    record["recovery_details"] = []
    record["configurations"] = []
    record["findings"] = []
    record["acceptance_artifact"] = None
    record["development_summary"] = None
    record["publication"] = None
    record["supporting_records"] = []
    record["status_text"] = _turning_point_status(event)


def _record_start(record: dict[str, Any]) -> datetime:
    timestamp = _parse_optional_timestamp(record.get("started_at"))
    if timestamp is not None:
        return timestamp
    timestamp = _parse_optional_timestamp(record.get("ended_at"))
    return timestamp or datetime.max.replace(tzinfo=UTC)


def _record_order(record: dict[str, Any]) -> tuple[str, str, int]:
    ordinal = record.get("ordinal")
    return (
        str(record.get("work_subject") or ""),
        str(record.get("role") or ""),
        ordinal if type(ordinal) is int else 0,
    )


def _record_span_seconds(record: dict[str, Any]) -> int | None:
    invocations = record.get("invocations")
    if isinstance(invocations, list) and any(
        isinstance(invocation, dict)
        and _parse_optional_timestamp(invocation.get("started_at")) is not None
        and _parse_optional_timestamp(invocation.get("ended_at")) is None
        for invocation in invocations
    ):
        # A current, trusted running observation is the one case where query
        # time is meaningful. Interrupted/unknown observations remain open.
        if record.get("activity") != "running":
            return None
        started = _parse_optional_timestamp(record.get("started_at"))
        return (
            max(0, int((datetime.now(UTC) - started).total_seconds()))
            if started is not None
            else None
        )
    started = _parse_optional_timestamp(record.get("started_at"))
    ended = _parse_optional_timestamp(record.get("ended_at"))
    if started is None:
        return None
    if ended is None:
        if record.get("activity") != "running":
            return None
        ended = datetime.now(UTC)
    return max(0, int((ended - started).total_seconds()))


def _record_execution_seconds(record: dict[str, Any]) -> int | None:
    invocations = record.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        return None
    last_invocation_index = len(invocations) - 1
    open_invocation_indices = [
        index
        for index, invocation in enumerate(invocations)
        if isinstance(invocation, dict)
        and _parse_optional_timestamp(invocation.get("started_at")) is not None
        and _parse_optional_timestamp(invocation.get("ended_at")) is None
    ]
    if any(index != last_invocation_index for index in open_invocation_indices):
        # An older invocation without a persisted end is not made complete by
        # observing a later invocation running.  Its execution interval is
        # unknowable, so the aggregate must remain unknown.
        return None
    total = 0
    for index, invocation in enumerate(invocations):
        if not isinstance(invocation, dict):
            return None
        allow_open = (
            index == last_invocation_index
            and record.get("activity")
            in {"running", "capacity_wait", "recovery_wait"}
        )
        duration = invocation_execution_seconds(invocation, allow_open=allow_open)
        if duration is None:
            return None
        total += duration
    return total


def _record_configurations(invocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configurations: list[dict[str, Any]] = []
    for invocation in invocations:
        configuration = {
            "started_at": invocation.get("started_at"),
            "model": invocation.get("model"),
            "reasoning_effort": invocation.get("reasoning_effort"),
        }
        if configurations and (
            configurations[-1].get("model") == configuration.get("model")
            and configurations[-1].get("reasoning_effort")
            == configuration.get("reasoning_effort")
        ):
            continue
        configurations.append(configuration)
    return configurations


def _history_record_status(record: dict[str, Any]) -> str:
    invocations = record.get("invocations")
    if isinstance(invocations, list) and invocations:
        latest = invocations[-1]
        if isinstance(latest, dict) and latest.get("status") in {
            "failed",
            "execution_failed",
        }:
            return "执行失败"
    attempt = record.get("attempt")
    if isinstance(record.get("findings"), list) and record["findings"]:
        return "验收未通过"
    artifact = record.get("acceptance_artifact")
    if isinstance(artifact, dict):
        outcome = _artifact_outcome(artifact)
        if outcome == "pass":
            return "验收通过"
        if outcome == "blocked":
            return "验收受阻"
    if isinstance(attempt, dict) and attempt.get("status") == "pending":
        if record.get("activity") == "running":
            return "进行中"
        if record.get("activity") in {"interrupted", "unknown"}:
            return _execution_activity_text(str(record["activity"]))
        return "待执行"
    attempt_outcome: object = (
        attempt.get("outcome") if isinstance(attempt, dict) else None
    )
    return {
        "candidate": "开发完成，待验收",
        "publication_artifact": "发布说明已完成",
        "acceptance_artifact": "验收结果已记录",
        "currentness_invalidated": "因版本变化失效",
        "no_code_changes": "开发未产生代码变更",
        "git_integrity_repair": "已记录 Git 修复边界",
    }.get(str(attempt_outcome), "已完成")


def _history_record_details(
    state: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    attempt = record.get("attempt")
    attempt_view = dict(attempt) if isinstance(attempt, dict) else {}
    for key in (
        "work_subject",
        "role",
        "generation",
        "ordinal",
        "budget_window",
    ):
        if attempt_view.get(key) is None and record.get(key) is not None:
            attempt_view[key] = record[key]
    invocations = record.get("invocations")
    invocation_values = [
        item
        for item in (invocations if isinstance(invocations, list) else [])
        if isinstance(item, dict)
    ]
    record["activity"] = _record_activity(state, record, invocation_values)
    owners = _owners_for_attempt(state, attempt_view)
    artifact = _review_artifact_for_attempt(owners, attempt_view, invocation_values)
    if artifact is None and isinstance(attempt_view.get("acceptance_artifact"), dict):
        artifact = attempt_view["acceptance_artifact"]
    findings = _artifact_findings(artifact) if isinstance(artifact, dict) else []
    return {
        "findings": findings,
        "acceptance_artifact": artifact,
        "budget_facts": _budget_facts_for_attempt(state, attempt_view),
        "development_summary": _development_summary_for_attempt(
            attempt_view, owners
        ),
        "publication": _publication_for_attempt(attempt_view, owners),
        "supporting_records": _supporting_records(
            owners, attempt_view, invocation_values
        ),
    }


def _budget_facts_for_attempt(
    state: dict[str, Any], attempt: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the immutable budget projection for this Attempt's window."""

    role = _role_family(str(attempt.get("role") or ""))
    window = attempt.get("budget_window")
    if role == "publication":
        return {"window": window}
    facts: dict[str, Any] = {
        "window": window,
        "development_attempts": None,
        "reviewer_invocations": None,
    }
    if not isinstance(window, int):
        return facts
    subject = str(attempt.get("work_subject") or "")
    policy_owner: tuple[str, dict[str, Any]] | None = None
    historical_policy_owner: tuple[str, dict[str, Any]] | None = None
    for owner_subject, owner in _subject_variants(state):
        if owner_subject != subject:
            continue
        if policy_owner is None:
            policy_owner = (owner_subject, owner)
        history = owner.get("review_budget_history")
        if not isinstance(history, list):
            continue
        for snapshot in history:
            if not isinstance(snapshot, dict):
                continue
            budget = snapshot.get("review_budget")
            if isinstance(budget, dict) and budget.get("window") == window:
                historical_policy_owner = (owner_subject, snapshot)
                break
        if historical_policy_owner is not None:
            break

    if historical_policy_owner is not None:
        policy_owner = historical_policy_owner

    stored_snapshot = attempt.get("budget_snapshot")
    if isinstance(stored_snapshot, dict) and stored_snapshot.get("window") == window:
        facts.update(
            {
                "development_attempts": stored_snapshot.get("development_attempts"),
                "reviewer_invocations": stored_snapshot.get("reviewer_invocations"),
            }
        )
    if policy_owner is None:
        return facts
    owner_subject, owner = policy_owner
    try:
        from agent_run.delivery_policy import (
            parent_only_budget_policy_for_job,
            run_repair_budget_policy_for_job,
            ticket_budget_policy_for_job,
        )

        if owner_subject.startswith("ticket:") or subject.startswith("ticket:"):
            policy = ticket_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        elif owner_subject.startswith("run-") or subject.startswith("run-"):
            policy = run_repair_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        else:
            policy = parent_only_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        facts.update(
            {
                "development_limit": policy.development_limit,
                "reviewer_limit": policy.review_limit,
            }
        )
    except (TypeError, ValueError, KeyError):
        # A malformed or absent policy cannot justify an inferred limit.
        pass
    return facts


def _record_activity(
    state: dict[str, Any],
    record: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> str:
    if not invocations:
        return "not_running"
    latest = invocations[-1]
    active = state.get("active_agent_invocation")
    if isinstance(active, dict) and (
        latest is active
        or (
            latest.get("started_at") == active.get("started_at")
            and latest.get("work_subject") == active.get("work_subject")
        )
    ):
        audit = {"executor_control": state.get("_executor_control")}
        return invocation_activity(active, audit)
    return "not_running"


def _owners_for_attempt(
    state: dict[str, Any], attempt: dict[str, Any]
) -> list[dict[str, Any]]:
    attempt_id = attempt.get("attempt_id")
    subject = str(attempt.get("work_subject") or "")
    generation = attempt.get("generation")
    owners: list[dict[str, Any]] = []
    for work_subject, owner in _subject_variants(state):
        if work_subject != subject:
            continue
        owner_attempts = _owner_attempts(owner)
        if any(
            isinstance(candidate, dict)
            and (
                candidate.get("attempt_id") == attempt_id
                if isinstance(attempt_id, str)
                else candidate.get("generation") == generation
                and candidate.get("role") == attempt.get("role")
                and candidate.get("ordinal") == attempt.get("ordinal")
            )
            for candidate in owner_attempts
        ):
            owners.append(owner)
    if owners:
        return owners
    same_subject = [
        owner
        for work_subject, owner in _subject_variants(state)
        if work_subject == subject
    ]
    if isinstance(attempt_id, str) and any(
        _owner_attempts(owner) for owner in same_subject
    ):
        # An identified Attempt which is absent from a retained owner history
        # cannot safely borrow the current Candidate's Artifact.
        return []
    return same_subject


def _subject_variants(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    run_id = state.get("run_id")
    values: list[tuple[str, dict[str, Any]]] = []
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and isinstance(active.get("ticket_number"), int):
        values.append((f"ticket:{active['ticket_number']}", active))
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for number, job in jobs.items():
            if isinstance(job, dict):
                values.append((f"ticket:{number}", job))
    parent = state.get("parent_job")
    if isinstance(parent, dict):
        values.append((f"parent-only:{run_id}", parent))
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        values.append((f"run-acceptance:{run_id}", acceptance))
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            values.append((f"run-repair:{run_id}", repair))
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        values.append((f"run-publication:{run_id}", publication))
    retired = state.get("retired_semantic_attempt_owners")
    if isinstance(retired, list):
        values.extend(
            (str(owner.get("work_subject")), owner)
            for owner in retired
            if isinstance(owner, dict) and isinstance(owner.get("work_subject"), str)
        )
    variants: list[tuple[str, dict[str, Any]]] = []
    for subject, owner in values:
        variants.append((subject, owner))
        history = owner.get("review_budget_history")
        if isinstance(history, list):
            variants.extend(
                (subject, snapshot)
                for snapshot in history
                if isinstance(snapshot, dict)
            )
    return variants


def _owner_attempts(owner: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    history = owner.get("semantic_attempt_history")
    if isinstance(history, list):
        values.extend(item for item in history if isinstance(item, dict))
    pending = owner.get("pending_semantic_attempt")
    if isinstance(pending, dict):
        values.append(pending)
    return values


def _review_artifact_for_attempt(
    owners: list[dict[str, Any]],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for owner in owners:
        budget = owner.get("review_budget")
        if not isinstance(budget, dict):
            continue
        if budget.get("window") != attempt.get("budget_window"):
            continue
        artifacts = budget.get("review_artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, dict) or not isinstance(
                    item.get("artifact"), dict
                ):
                    continue
                identity = json.dumps(
                    {
                        "reviewer_thread_id": item.get("reviewer_thread_id"),
                        "candidate_sha": item.get("candidate_sha"),
                        "review_identity": item.get("review_identity"),
                        "artifact": item.get("artifact"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if identity in seen_candidates:
                    continue
                seen_candidates.add(identity)
                candidates.append(item)
    if not candidates:
        return None
    reviewer_ids = {
        value
        for invocation in invocations
        for key in ("reported_thread_id", "requested_thread_id", "thread_id")
        if isinstance((value := invocation.get(key)), str) and value
    }
    candidate_heads = {
        value
        for invocation in invocations
        for boundary in (invocation.get("currentness_boundary"),)
        if isinstance(boundary, dict)
        for key in (
            "candidate_sha",
            "reviewed_candidate_sha",
            "reviewed_head_sha",
            "run_head_sha",
            "head_sha",
            "repair_candidate_sha",
        )
        if isinstance((value := boundary.get(key)), str) and value
    }
    history_facts = attempt.get("history_facts")
    if isinstance(history_facts, dict):
        reviewer_thread_id = history_facts.get("reviewer_thread_id")
        if isinstance(reviewer_thread_id, str) and reviewer_thread_id:
            reviewer_ids.add(reviewer_thread_id)
        candidate_sha = history_facts.get("candidate_sha")
        if isinstance(candidate_sha, str) and candidate_sha:
            candidate_heads.add(candidate_sha)
        review_identity = history_facts.get("review_identity")
        if isinstance(review_identity, dict):
            for key in (
                "candidate_sha",
                "reviewed_candidate_sha",
                "reviewed_head_sha",
                "run_head_sha",
                "head_sha",
                "repair_candidate_sha",
            ):
                value = review_identity.get(key)
                if isinstance(value, str) and value:
                    candidate_heads.add(value)
    matched = candidates
    if reviewer_ids:
        matched = [
            item for item in matched if item.get("reviewer_thread_id") in reviewer_ids
        ]
    if candidate_heads:
        matched = [
            item for item in matched if item.get("candidate_sha") in candidate_heads
        ]
    # Both sides of the review identity are required. A canonical Attempt
    # without its Reviewer and Candidate boundary cannot safely borrow even a
    # unique Artifact from the same window.
    if not reviewer_ids or not candidate_heads:
        return None
    if len(matched) != 1:
        return None
    artifact = matched[0].get("artifact")
    return artifact if isinstance(artifact, dict) else None


def _development_summary_for_attempt(
    attempt: dict[str, Any], owners: list[dict[str, Any]]
) -> str | None:
    value = attempt.get("development_summary")
    if isinstance(value, str) and value.strip():
        return value
    if attempt.get("role") != "development":
        return None
    for owner in owners:
        value = owner.get("development_summary")
        if isinstance(value, str) and value.strip() and _owner_attempt_is_latest(owner, attempt):
            return value
    return None


def _publication_for_attempt(
    attempt: dict[str, Any], owners: list[dict[str, Any]]
) -> dict[str, Any] | None:
    value = attempt.get("publication")
    if isinstance(value, dict):
        return value
    if attempt.get("role") != "publication":
        return None
    for owner in owners:
        value = owner.get("publication")
        if isinstance(value, dict) and _owner_attempt_is_latest(owner, attempt):
            return value
    return None


def _owner_attempt_is_latest(owner: dict[str, Any], attempt: dict[str, Any]) -> bool:
    field = {
        "development": "modification_attempts",
        "reviewer": "validation_attempts",
        "publication": "publication_attempts",
    }.get(str(attempt.get("role")))
    if field is None:
        return False
    if field in owner:
        return owner.get(field) == attempt.get("ordinal")
    # A legacy/read-only projection with no owner history has no competing
    # attempt from which this record could have been borrowed.
    return not _owner_attempts(owner)


_SUPPORTING_RECORD_KEYS = (
    "required_checks_evidence",
    "deterministic_integration_record",
    "fallback_publication_receipt",
)
_SUPPORTING_IDENTITY_GROUPS = {
    "candidate": {
        "candidate_sha",
        "candidate_commit_sha",
        "reviewed_candidate_sha",
        "reviewed_head_sha",
        "run_head_sha",
        "repair_candidate_sha",
        "commit_sha",
    },
    "publication": {
        "publication_sha",
        "published_sha",
        "integrated_publication_sha",
    },
    "integration": {
        "integrated_sha",
        "merge_commit_sha",
    },
}
_SUPPORTING_SCOPE_KEYS = {
    "base_sha",
    "reviewed_base_sha",
    "reviewed_default_base_sha",
    "default_base_sha",
    "repair_base_run_head_sha",
    "effective_revision",
    "parent_revision",
    "ticket_graph_revision",
    "candidate_tree",
    "candidate_tree_sha",
    "reviewed_candidate_tree",
    "expected_merge_tree",
}
_SUPPORTING_NESTED_KEYS = {
    "acceptance_record",
    "candidate",
    "currentness_boundary",
    "deterministic_integration_record",
    "fallback_publication_receipt",
    "fallback_receipt",
    "pr",
    "review_budget",
    "publication",
    "required_checks_evidence",
    "review_identity",
    "semantic_attempt",
    "version",
}
_SUPPORTING_PUBLICATION_HEAD_KEYS = {"pr", "required_checks_evidence"}


def _supporting_scalar_values(value: object) -> set[str]:
    if isinstance(value, str) and value:
        return {value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {str(value)}
    return set()


def _supporting_identity_bundle(
    value: object, *, default_head_group: str = "candidate"
) -> dict[str, set[str]]:
    bundle: dict[str, set[str]] = {
        "attempt": set(),
        "candidate": set(),
        "publication": set(),
        "integration": set(),
        "window": set(),
        "pr": set(),
        "thread": set(),
    }
    bundle.update({key: set() for key in _SUPPORTING_SCOPE_KEYS})
    if not isinstance(value, dict):
        return bundle
    for key, item in value.items():
        if key in {"attempt_id", "semantic_attempt_id"}:
            bundle["attempt"].update(_supporting_scalar_values(item))
        for group, keys in _SUPPORTING_IDENTITY_GROUPS.items():
            if key in keys:
                bundle[group].update(_supporting_scalar_values(item))
        if key == "head_sha":
            bundle[default_head_group].update(_supporting_scalar_values(item))
        elif key in _SUPPORTING_SCOPE_KEYS:
            bundle[key].update(_supporting_scalar_values(item))
        elif key in {"window", "budget_window"}:
            bundle["window"].update(_supporting_scalar_values(item))
        elif key == "pr_number":
            bundle["pr"].update(_supporting_scalar_values(item))
        elif key in {
            "reviewer_thread_id",
            "reported_thread_id",
            "requested_thread_id",
            "thread_id",
        }:
            bundle["thread"].update(_supporting_scalar_values(item))
        if key in _SUPPORTING_NESTED_KEYS:
            if key == "pr" and isinstance(item, dict):
                bundle["pr"].update(_supporting_scalar_values(item.get("number")))
            nested_head_group = (
                "publication"
                if key in _SUPPORTING_PUBLICATION_HEAD_KEYS
                else "candidate"
            )
            nested = _supporting_identity_bundle(
                item, default_head_group=nested_head_group
            )
            for nested_key, nested_values in nested.items():
                bundle[nested_key].update(nested_values)
    return bundle


def _merge_supporting_bundles(*values: object) -> dict[str, set[str]]:
    merged = _supporting_identity_bundle(None)
    for value in values:
        bundle = _supporting_identity_bundle(value)
        for key, items in bundle.items():
            merged[key].update(items)
    return merged


def _supporting_attempt_ids(value: object) -> set[str]:
    return _supporting_identity_bundle(value)["attempt"]


def _attempt_invocations(
    attempt: dict[str, Any], invocations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    attempt_id = attempt.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        matched = [
            invocation
            for invocation in invocations
            if isinstance(invocation.get("semantic_attempt"), dict)
            and invocation["semantic_attempt"].get("attempt_id") == attempt_id
        ]
        if matched:
            return matched
    return [
        invocation
        for invocation in invocations
        if isinstance(invocation.get("semantic_attempt"), dict)
        and all(
            invocation["semantic_attempt"].get(key) == attempt.get(key)
            for key in ("work_subject", "generation", "role", "ordinal")
            if attempt.get(key) is not None
        )
    ]


def _supporting_record_matches_attempt(
    kind: str,
    value: dict[str, Any],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> bool:
    record = _supporting_identity_bundle(
        value,
        default_head_group=(
            "publication" if kind == "required_checks_evidence" else "candidate"
        ),
    )
    record_attempt_ids = record["attempt"]
    attempt_id = attempt.get("attempt_id")
    if record_attempt_ids:
        if not isinstance(attempt_id, str) or attempt_id not in record_attempt_ids:
            return False

    attempt_bundles = [attempt]
    history_facts = attempt.get("history_facts")
    if isinstance(history_facts, dict):
        attempt_bundles.append(history_facts)
    attempt_bundles.extend(_attempt_invocations(attempt, invocations))
    expected = _merge_supporting_bundles(*attempt_bundles)
    for key in (
        *_SUPPORTING_SCOPE_KEYS,
        "window",
        "pr",
        "thread",
    ):
        expected_values = expected[key]
        record_values = record[key]
        if len(expected_values) > 1 or len(record_values) > 1:
            return False
        if expected_values and record_values and not expected_values & record_values:
            return False

    # Candidate aliases must all identify one reviewed object. Publication and
    # integration identities are intentionally separate: a publication commit
    # and its later merge commit are both valid parts of one record.
    expected_candidates = expected["candidate"]
    record_candidates = record["candidate"]
    if len(expected_candidates) > 1 or len(record_candidates) > 1:
        return False
    if record_candidates and not expected_candidates & record_candidates:
        return False
    expected_publications = expected["publication"]
    record_publications = record["publication"]
    if (
        expected_publications
        and record_publications
        and not expected_publications & record_publications
    ):
        return False
    expected_integrations = expected["integration"]
    record_integrations = record["integration"]
    if (
        expected_integrations
        and record_integrations
        and not expected_integrations & record_integrations
    ):
        return False

    # A supporting record must prove the reviewed object or publication. A
    # shared effective revision, base, or window is not sufficient: two
    # candidates may intentionally share all of those values.
    identity_matches = bool(
        record["candidate"] & expected["candidate"]
        or record["publication"] & expected["publication"]
        or record["integration"] & expected["integration"]
    )
    if not identity_matches:
        return False
    return True


def _supporting_records(
    owners: list[dict[str, Any]],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for owner in owners:
        matched_values: dict[str, dict[str, Any]] = {}
        for key in _SUPPORTING_RECORD_KEYS:
            value = owner.get(key)
            if isinstance(value, dict) and _supporting_record_matches_attempt(
                key, value, attempt, invocations
            ):
                matched_values[key] = value
        # The live Job-level Observation has only the published ``head_sha``.
        # When it is an exact copy of a versioned integration/fallback record's
        # nested Observation, that parent record supplies the missing Candidate
        # binding. Otherwise leave the projection explicitly unrecorded.
        required_checks = owner.get("required_checks_evidence")
        if isinstance(required_checks, dict):
            for parent_key in (
                "deterministic_integration_record",
                "fallback_publication_receipt",
            ):
                parent = matched_values.get(parent_key)
                if (
                    parent is not None
                    and parent.get("required_checks_evidence") == required_checks
                ):
                    matched_values["required_checks_evidence"] = required_checks
                    break
        for key in _SUPPORTING_RECORD_KEYS:
            value = matched_values.get(key)
            if value is None:
                continue
            identity = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            records.append({"kind": key, "value": value})
    return records


def _is_history_turning_point(event: dict[str, Any]) -> bool:
    details = event.get("details")
    if isinstance(details, list) and details and _is_human_blocker_event(event):
        return True
    kind = str(event.get("kind") or "")
    status = str(event.get("status") or "")
    return kind in {
        "human_blocker",
        "supervision",
        "required_checks",
        "integration",
        "completion",
        "publication",
        "pr_creation",
        "approval",
        "resume",
        "unsupported_scope_change",
    } or status in {
        "completed",
        "abandoned",
        "abandonment_pending",
        "deterministic_contradiction",
        "parent_approval_pending",
        "parent_closeout_pending",
        "progress_exhausted",
        "requeue_required",
        "operator_stopped",
        "ready_for_human",
        "execution_failed",
        "supervision_timeout",
        "ticket_completed",
        "waiting_external",
        "waiting_checks",
        "waiting_merge",
    }


_HUMAN_BLOCKER_STATUSES = {"ready_for_human"}


def _is_human_blocker_event(event: dict[str, Any]) -> bool:
    return str(event.get("kind") or "") == "human_blocker" or str(
        event.get("status") or ""
    ) in _HUMAN_BLOCKER_STATUSES


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
        else ("进行中" if record.get("activity") == "running" else "未知时间")
    )
    interval = f"{started} → {ended}"
    ordinal = record.get("ordinal")
    round_text = f"第 {ordinal} 轮" if isinstance(ordinal, int) else "轮次未记录"
    finding_values = record.get("findings")
    findings = finding_values if isinstance(finding_values, list) else []
    title = (
        f"{_safe_text(human_delivery_object(record.get('object') or 'Delivery Run'))} · "
        f"{_safe_text(record.get('role_label') or 'Agent')} {round_text} · "
        f"{_safe_text(record.get('status_text') or '未知')}"
    )
    if findings:
        title += f"（问题 {len(findings)}）"
    lines = [
        f"  {title}",
        f"    时间区间：{interval}",
        f"    动作：{_record_action(record)}；结果：{_safe_text(record.get('status_text') or '未知')}",
    ]
    lines.append(
        f"    时长：整轮 {_duration(record.get('span_seconds'))}；"
        f"累计执行 {_duration(record.get('execution_seconds'))}"
    )
    if record.get("span_seconds") is None or record.get("execution_seconds") is None:
        lines.append("    实际执行时长未知（缺少可信结束证据）")
    lines.append(f"    {_budget_line(record)}")
    configurations = record.get("configurations")
    if isinstance(configurations, list) and configurations:
        for index, configuration in enumerate(configurations, start=1):
            lines.append(
                f"    配置 {index}：模型={_safe_text(configuration.get('model') or '未记录')}；"
                f"推理强度={_safe_text(configuration.get('reasoning_effort') or '未记录')}"
            )
    else:
        lines.append("    配置：模型=未记录；推理强度=未记录")
    if record.get("resumption_count", 0):
        lines.append(f"    显式恢复/继续：{record['resumption_count']} 次，已归入本轮")
    output_attempts = record.get("output_attempts")
    if isinstance(output_attempts, list) and any(
        isinstance(value, int) and value > 1 for value in output_attempts
    ):
        lines.append(
            "    输出续接："
            f"{max(value for value in output_attempts if isinstance(value, int)) - 1} 次，"
            "仍归入本轮"
        )
    recovery_details = record.get("recovery_details")
    if isinstance(recovery_details, list):
        lines.extend(
            f"    {_truncate_history_detail(_safe_text(detail))}"
            for detail in recovery_details
        )
    if findings:
        lines.append(f"    问题（{len(findings)}）")
        lines.extend(
            f"      {_finding_question(finding)}"
            for finding in findings
        )
    elif record.get("role") in {"reviewer", "fresh_acceptance"} and record.get(
        "acceptance_artifact"
    ) is None:
        lines.append("    验收结论：尚无可确认的验收资料")
    if details:
        lines.extend(_record_detail_lines(record, timezone=timezone))
    turning_points = record.get("turning_points")
    if isinstance(turning_points, list) and turning_points:
        lines.append("    关键转折")
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
        f"    时间：{_format_local_timestamp(point.get('at'), timezone)}",
    ]
    if point.get("kind") in {"required_checks", "approval", "pr_creation"} and record.get("span_seconds"):
        lines[1] = (
            f"    时间区间：{_format_local_timestamp(record.get('started_at'), timezone)} → "
            f"{_format_local_timestamp(record.get('ended_at'), timezone)}"
        )
        if point.get("kind") != "pr_creation":
            lines.append(f"    记录到的等待时间：{_duration(record['span_seconds'])}")
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
        return f"最终 PR #{point['pr_number']} 已创建"
    if kind == "completion":
        return "交付已放弃" if point.get("status") == "abandoned" else "整体交付完成"
    if kind == "integration":
        if point.get("status") == "parent_closeout_pending":
            return "代码已合并，正在关闭整体需求 Issue"
        return "代码已合并" if point.get("commit_sha") else "合并代码"
    if kind == "approval":
        return "已获人工批准" if point.get("approval_granted_at") else "等待人工批准"
    if kind == "required_checks":
        return _turning_point_status(point)
    if kind == "publication":
        return (
            f"创建／更新 PR #{point['pr_number']}"
            if point.get("pr_number") is not None else "准备提交代码与创建 PR"
        )
    action = point.get("github_write_action")
    if action in {"ensure_final_run_ref", "create_final_pr", "refresh_final_pr_narrative"}:
        label = {
            "ensure_final_run_ref": "准备远端分支",
            "create_final_pr": "创建最终 PR",
            "refresh_final_pr_narrative": "更新最终 PR 说明",
        }[action]
        return f"{label} · {_turning_point_status(point)}"
    return f"{_turning_point_kind(point)} · {_turning_point_status(point)}"


def _budget_line(record: dict[str, Any]) -> str:
    if _role_family(str(record.get("role") or "")) == "publication":
        return "执行额度：不适用（编写发布说明）"
    facts = record.get("budget_facts")
    if not isinstance(facts, dict):
        return "本轮开始时已用：开发次数未记录；验收次数未记录"
    window = facts.get("window")
    window_text = window if isinstance(window, int) else "未记录"
    development = _budget_fraction(
        facts.get("development_attempts"), facts.get("development_limit")
    )
    review = _budget_fraction(
        facts.get("reviewer_invocations"), facts.get("reviewer_limit")
    )
    return (
        f"第 {window_text} 次授权额度；本轮开始时已用：开发 {development} 次；"
        f"验收 {review} 次"
    )


def _budget_fraction(used: object, limit: object) -> str:
    used_text = str(used) if type(used) is int else "未记录"
    limit_text = str(limit) if type(limit) is int else "未记录"
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
    result_label: str = "合并前检查结果",
) -> None:
    if not isinstance(observation, dict):
        return
    _append_detail_scalar(
        lines,
        indent=indent,
        label="PR 编号",
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
        label="检查结果获取时间",
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
            details.append(f"名称={_safe_text(name)}")
        if isinstance(result, (str, int, float, bool)):
            details.append(f"结果={_safe_text(human_status_term(result))}")
        for key, label in (
            ("workflow", "工作流"),
            ("link", "链接"),
            ("description", "说明"),
        ):
            extra = check.get(key)
            if isinstance(extra, (str, int, float, bool)) and (
                not isinstance(extra, str) or extra.strip()
            ):
                details.append(f"{label}={_safe_text(extra)}")
        lines.append(f"{indent}检查项：{'；'.join(details)}")


def _git_integrity_detail_lines(
    evidence: dict[str, Any], *, indent: str
) -> list[str]:
    lines: list[str] = []
    for key, label in (
        ("status", "结果"),
        ("reason", "失败原因"),
        ("expected_head", "期望 HEAD"),
        ("observed_head", "实际 HEAD"),
        ("base_sha", "基础 HEAD"),
        ("previous_candidate_sha", "上一个代码版本"),
        ("workspace_clean", "工作区清洁"),
        ("recovery_head", "恢复后 HEAD"),
        ("recovery_action", "恢复方式"),
        ("recovery_error", "恢复错误"),
    ):
        value = evidence.get(key)
        if key == "status":
            value = human_status_term(value)
        elif key == "workspace_clean" and isinstance(value, bool):
            value = "是" if value else "否"
        elif key == "recovery_action" and value == "controller_reset_and_clean":
            value = "恢复已保存版本并清理工作区"
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
        "configured": "已配置合并前检查", "not_configured": "未配置合并前检查",
    }.get(str(_required_checks_mode(value)))
    if kind == "required_checks_evidence":
        if mode_text:
            lines.append(f"      {mode_text}")
        _append_check_observation_detail(lines, value, indent="      ")
        return lines

    if kind == "deterministic_integration_record":
        source = value.get("source")
        if source == "accepted":
            lines.append("      验收通过后合并")
        elif source == "fallback":
            lines.append("      通过兜底校验后合并，未经独立验收通过")
        if mode_text:
            lines.append(f"      {mode_text}")
        _append_detail_scalar(
            lines,
            indent="      ",
            label="合并前检查结果",
            value=human_status_term(_first_detail_value(value, ("required_checks", "required_checks_result"))),
        )
        _append_detail_scalar(
            lines, indent="      ", label="PR 编号", value=value.get("pr_number")
        )
        pr = value.get("pr")
        if isinstance(pr, dict):
            _append_detail_scalar(
                lines, indent="      ", label="PR 状态", value=human_status_term(pr.get("state"))
            )
        _append_detail_scalar(
            lines,
            indent="      ",
            label="集成说明",
            value=value.get("integrated_message"),
        )
        evidence = value.get("required_checks_evidence")
        if isinstance(evidence, dict):
            lines.append("      合并前检查证据")
            _append_check_observation_detail(lines, evidence, indent="        ")
        return lines

    if kind == "fallback_publication_receipt":
        lines.append("      验收额度已用尽，按兜底校验发布；不代表独立验收通过")
        _append_detail_scalar(
            lines, indent="      ", label="PR 编号", value=value.get("pr_number")
        )
        if mode_text:
            lines.append(f"      {mode_text}")
        evidence = value.get("required_checks_evidence")
        if isinstance(evidence, dict):
            lines.append("      合并前检查证据")
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
            failure_lines: list[str] = []
            label = "检查失败依据"
            if source == "git_integrity":
                label = "Git 完整性失败依据"
                failure_lines = _git_integrity_detail_lines(
                    failure_evidence, indent="        "
                )
            elif source == "acceptance":
                label = "验收失败依据"
                checks = failure_evidence.get("checks")
                if isinstance(checks, dict):
                    for lane, lane_label in (
                        ("e2e", "功能验证"),
                        ("standards", "工程审查"),
                        ("spec", "需求核对"),
                    ):
                        check = checks.get(lane)
                        if not isinstance(check, dict):
                            continue
                        _append_detail_scalar(
                            failure_lines, indent="        ", label=lane_label,
                            value=human_status_term(check.get("status")),
                        )
                        _append_detail_scalar(
                            failure_lines, indent="          ", label="证据",
                            value=check.get("evidence"),
                        )
                        findings = check.get("findings")
                        if isinstance(findings, list):
                            for finding in findings:
                                if isinstance(finding, str) and finding.strip():
                                    failure_lines.extend(
                                        _full_finding_lines(finding, indent="          ")
                                    )
            else:
                _append_check_observation_detail(
                    failure_lines, failure_evidence, indent="        ",
                    result_label="结果",
                )
            if failure_lines:
                lines.append(f"      {label}")
                lines.extend(failure_lines)
        return lines

    return lines


def _record_detail_lines(record: dict[str, Any], *, timezone: tzinfo) -> list[str]:
    lines: list[str] = []
    summary = record.get("development_summary")
    if isinstance(summary, str) and summary.strip():
        lines.extend(["    开发说明", f"      {_safe_text(summary)}"])
    elif record.get("role") == "development":
        lines.append("    开发说明：未记录")
    artifact = record.get("acceptance_artifact")
    if isinstance(artifact, dict):
        checks = artifact.get("checks")
        if not _artifact_is_structured(artifact):
            lines.append(
                "    验收资料原文："
                + _safe_text(json.dumps(artifact, ensure_ascii=False, default=str))
            )
        if isinstance(checks, dict):
            lines.append("    验收证据")
            for lane, label in (
                ("e2e", "功能验证"),
                ("standards", "工程审查"),
                ("spec", "需求核对"),
            ):
                check = checks.get(lane)
                if not isinstance(check, dict):
                    lines.append(f"      {label}：未记录")
                    continue
                lines.append(
                    f"      {label}：{_safe_text(human_status_term(check.get('status') or '未知'))}"
                )
                evidence = check.get("evidence")
                if isinstance(evidence, str) and evidence.strip():
                    lines.append(f"        证据：{_safe_text(evidence)}")
                lane_findings = check.get("findings")
                if isinstance(lane_findings, list):
                    for finding in lane_findings:
                        if isinstance(finding, str):
                            lines.extend(_full_finding_lines(finding, indent="        "))
    publication = record.get("publication")
    if isinstance(publication, dict):
        lines.append("    发布说明")
        for key, label in (
            ("commit_message", "提交说明"),
            ("pr_title", "PR 标题"),
            ("pr_body_markdown", "PR 正文"),
        ):
            value = publication.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"      {label}：{_safe_text(value)}")
        for key, label in (
            ("pr_number", "PR 编号"),
        ):
            value = publication.get(key)
            if value is not None:
                lines.append(f"      {label}：{_safe_text(value)}")
    invocations = record.get("invocations")
    if isinstance(invocations, list) and invocations:
        lines.append("    本轮执行记录")
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
                else ("进行中" if is_current_running_invocation else "未知时间")
            )
            duration = _invocation_duration(
                invocation,
                {"executor_control": {"activity": "running"}}
                if is_current_running_invocation
                else {},
            )
            lines.append(
                f"      {index}：{started} → {ended}；"
                f"状态={_safe_text(human_status_term(invocation.get('status') or '未记录'))}；"
                f"时长={_duration(duration)}"
            )
            lines.append(
                f"        模型={_safe_text(invocation.get('model') or '未记录')}；"
                f"推理强度={_safe_text(invocation.get('reasoning_effort') or '未记录')}"
            )
            if isinstance(invocation.get("attempt_count"), int) and invocation["attempt_count"] > 1:
                lines.append(f"        输出续接={_output_continuation_text(invocation['attempt_count'])} 次")
            elif invocation.get("attempt_count") is None:
                lines.append("        输出续接次数：未记录")
            for key, label in (
                ("error", "错误"),
                ("validation_error", "验证错误"),
                ("interruption_observed_at", "中断观测时间"),
                ("last_failure_at", "最近失败时间"),
                ("signal", "信号"),
                ("return_code", "返回码"),
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
    lines = [f"  状态：{_cleanup_status_text(status)}"]
    if last_error is not None:
        value = _safe_text(last_error)
        lines.append(
            f"  最后错误：{value if details else _truncate_history_detail(value)}"
        )
    for item in pending_items:
        branch = item.get("branch") or "未记录分支"
        checkout = item.get("checkout") or "未记录工作区"
        lines.append(
            f"  待清理：分支={_safe_text(branch)}；工作区={_safe_text(checkout)}；"
            f"状态={_cleanup_status_text(item.get('status'))}"
        )
        if details:
            recovery_kind = item.get("recovery_kind")
            if recovery_kind == "stale_dirty_checkout":
                lines.append("    保留原因：工作区仍有未提交修改")
            item_error = item.get("last_error")
            if item_error is not None:
                lines.append(f"    项错误：{_safe_text(item_error)}")
    return lines


def _cleanup_status_text(value: object) -> str:
    return {
        "pending": "待处理",
        "cleanup_pending": "等待清理",
        "completed": "已完成",
    }.get(str(value or ""), _safe_text(value or "未记录"))


def _turning_point_evidence_lines(
    point: dict[str, Any], *, timezone: tzinfo, details: bool
) -> list[str]:
    lines: list[str] = []
    if point.get("pr_number") is not None and point.get("kind") != "pr_creation":
        lines.append(f"        PR 编号：{_safe_text(point['pr_number'])}")
    if (
        point.get("kind") not in {"required_checks", "pr_creation"}
        and point.get("required_checks_signature") is None
        and point.get("required_checks_result") is not None
    ):
        # Older records may only retain this result on the completion event.
        # Without an observation identity, do not assume a separate check
        # record already carries it.
        lines.append(f"        合并前检查结果：{_safe_text(human_status_term(point['required_checks_result']))}")
    observed_at = point.get("required_checks_observed_at")
    if details and point.get("kind") == "required_checks" and observed_at is not None:
        lines.append(
            "        检查结果获取时间："
            f"{_format_local_timestamp(observed_at, timezone)}"
        )
    next_action = point.get("next_action")
    if next_action is not None:
        value = _safe_text(next_action)
        if not details:
            value = _truncate_history_detail(value)
        lines.append(f"        下一步：{value}")
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
                lines.append(f"        检查名称：{_safe_text('、'.join(names))}")
        if details:
            lines.append("        合并前检查证据：")
            _append_check_observation_detail(lines, evidence, indent="          ")
        if details and evidence.get("omitted_checks"):
            lines.append(f"          另有 {evidence['omitted_checks']} 项未保留明细")
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
        lines.append(f"        外部情况：{_safe_text(reason['message'])}")
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
        return [f"{indent}问题原文：{_safe_text(finding)}"]
    lines = [
        f"{indent}{label}：{_safe_text(value)}"
        for label, value in zip(("问题", "证据", "必须修复", "复验"), match.groups())
    ]
    return lines


def _finding_question(finding: object) -> str:
    if not isinstance(finding, str):
        return f"问题原文：{_safe_text(finding)}"
    match = _FINDING.fullmatch(finding)
    return (
        f"问题：{_safe_text(match.group(1))}"
        if match is not None
        else f"问题原文：{_safe_text(finding)}"
    )


def _record_action(record: dict[str, Any]) -> str:
    role = _role_family(str(record.get("role") or ""))
    return {
        "development": "实现任务",
        "review": "验收代码",
        "publication": "编写提交与 PR 说明",
    }.get(role or "", "交付状态变更")


def _turning_point_kind(event: dict[str, Any]) -> str:
    return {
        "human_blocker": "人工阻塞",
        "supervision": "确认外部操作结果",
        "required_checks": "合并前检查",
        "integration": "集成",
        "publication": "发布",
        "approval": "人工批准",
        "abandonment": "放弃处理",
        "completion": "整体完成",
        "resume": "恢复",
        "unsupported_scope_change": "范围变化",
    }.get(str(event.get("kind") or ""), "状态变化")


def _turning_point_status(event: dict[str, Any]) -> str:
    if event.get("kind") == "approval":
        return "已批准" if event.get("approval_granted_at") else "等待人工批准"
    if event.get("kind") == "required_checks":
        if event.get("required_checks_observation_status") in {"unavailable", "unknown"}:
            return "合并前检查结果无法读取"
        return {
            "pending": "等待合并前检查", "pass": "合并前检查通过",
            "none": "未配置合并前检查", "fail": "合并前检查失败",
            "unknown": "合并前检查结果未知",
        }.get(str(event.get("required_checks_result")), "合并前检查结果未知")
    if event.get("kind") == "resume":
        return "人工恢复/继续"
    if event.get("activity") == "interrupted":
        return "执行已中断，等待恢复"
    if event.get("activity") == "unknown":
        return "运行状态无法确认"
    if event.get("activity") == "capacity_wait":
        return "模型容量不足，等待自动续接"
    if event.get("activity") == "recovery_wait":
        return "执行异常，等待自动续接"
    return str(human_status_term(event.get("status") or "状态变化"))


def _format_local_timestamp(value: object, timezone: tzinfo) -> str:
    timestamp = _parse_optional_timestamp(value)
    if timestamp is None:
        return "未知时间"
    return timestamp.astimezone(timezone).strftime("%Y-%m-%d %H:%M %z")


def _execution_activity_text(activity: str) -> str:
    return {
        "running": "正在执行",
        "interrupted": "执行已中断，等待恢复",
        "unknown": "运行状态无法确认",
        "capacity_wait": "模型容量不足，等待自动续接",
        "recovery_wait": "执行异常，等待自动续接",
        "not_running": "未在执行",
    }.get(activity, "未知")


def _round_summary_text(rounds: object) -> str:
    if not isinstance(rounds, dict) or not rounds:
        return "尚无 Agent 轮次"
    return "；".join(f"{_round_label(str(key))} {value} 轮" for key, value in rounds.items())


def _rich_line(label: object, value: object) -> Any:
    from rich.text import Text

    line = Text()
    line.append(f"{_safe_text(label)}：", style="bold")
    line.append(_safe_text("未知" if value is None else value))
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
            timestamp = _utc_timestamp(raw["at"])
            if timestamp is None:
                continue
            blockers = raw.get("human_blockers")
            details = (
                [item for item in blockers if isinstance(item, str)]
                if isinstance(blockers, list)
                else []
            )
            kind = _timeline_kind(raw, details)
            details.extend(_scope_change_details(raw))
            worker = raw.get("worker")
            events.append(
                {
                    "at": timestamp,
                    "kind": kind,
                    "object": _timeline_object(state, raw),
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
            timestamp = _utc_timestamp(raw_at) if isinstance(raw_at, str) else None
            if timestamp is None:
                continue
            attempt = invocation.get("semantic_attempt")
            attempt_view = _invocation_attempt_view(invocation)
            work_subject = str(invocation.get("work_subject") or "")
            role = str(
                attempt_view.get("role")
                or invocation.get("invocation_role")
                or invocation.get("role")
                or "unknown"
            )
            round_number = attempt_view.get("ordinal")
            artifact = _review_artifact_for_attempt(
                _owners_for_attempt(state, attempt_view),
                attempt_view,
                [invocation],
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
                    "object": _subject_label(state, work_subject),
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

    responses = _human_responses_by_subject(state)
    response_audit = state.get("human_response_audit")
    audited_responses = response_audit if isinstance(response_audit, dict) else {}
    resumes = audit.get("agent_resumes")
    if isinstance(resumes, list):
        for order, resume in enumerate(resumes, start=len(events)):
            if not isinstance(resume, dict):
                continue
            raw_at = resume.get("requested_at")
            timestamp = _utc_timestamp(raw_at) if isinstance(raw_at, str) else None
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
                    "object": _subject_label(state, str(resume.get("work_subject") or "")),
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


def _human_responses_by_subject(
    state: dict[str, Any],
) -> dict[tuple[str, object], list[str]]:
    responses: dict[tuple[str, object], list[str]] = {}
    subjects: list[tuple[str, dict[str, Any]]] = []
    run_id = state.get("run_id")
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        subjects.extend(
            (f"ticket:{number}", job)
            for number, job in jobs.items()
            if isinstance(job, dict)
        )
    elif isinstance(state.get("active_ticket_job"), dict):
        active = state["active_ticket_job"]
        subjects.append((f"ticket:{active.get('ticket_number')}", active))
    for name, locator in (
        ("parent_job", f"parent-only:{run_id}"),
        ("run_acceptance", f"run-acceptance:{run_id}"),
        ("run_publication", f"run-publication:{run_id}"),
    ):
        subject = state.get(name)
        if isinstance(subject, dict):
            subjects.append((locator, subject))
            repair = subject.get("repair_job")
            if isinstance(repair, dict):
                subjects.append((f"run-repair:{run_id}", repair))
    for locator, subject in subjects:
        history = subject.get("human_response_history")
        if not isinstance(history, list):
            continue
        for entry in history:
            response = entry.get("response") if isinstance(entry, dict) else None
            if isinstance(response, str):
                generation = entry.get("generation") if isinstance(entry, dict) else None
                responses.setdefault((locator, generation), []).append(response)
    return responses


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


def _timeline_object(state: dict[str, Any], event: dict[str, Any]) -> str:
    if isinstance(event.get("ticket"), int):
        return delivery_object_label(
            state, f"ticket:{event['ticket']}", ticket_number=event["ticket"]
        )
    kind = str(event.get("kind") or "")
    if kind == "parent_phase":
        return delivery_object_label(state, "parent")
    if kind == "run_acceptance":
        return delivery_object_label(state, "run_acceptance")
    if kind == "run_publication":
        return delivery_object_label(state, "run_publication")
    return delivery_object_label(state, "run")


def _human_timeline_kind(event: dict[str, Any], details: list[str]) -> str:
    """Human lifecycle nodes are independent of the unchanged machine projection."""

    if _is_human_blocker_event(event):
        return "human_blocker"
    status, phase = event.get("status"), event.get("phase")
    if status == "abandonment_pending":
        return "abandonment"
    if status in {"operator_stopped", "execution_failed", "requeue_required"}:
        return "supervision"
    if status in {"completed", "abandoned"}:
        return "completion"
    if phase in {"merged", "completed"} and event.get("pr_number") is not None:
        return "integration"
    if status in {"run_approval_pending", "parent_approval_pending"} or (
        phase == "ready_for_approval" and status not in {"waiting_external", "supervision_timeout"}
    ):
        return "approval"
    return _timeline_kind(event, details)


def _timeline_kind(event: dict[str, Any], details: list[str]) -> str:
    if _is_human_blocker_event(event):
        return "human_blocker"
    status = str(event.get("status") or "")
    phase = str(event.get("phase") or "")
    kind = str(event.get("kind") or "delivery_state")
    if status in {"supervision_timeout", "waiting_external"}:
        return "supervision"
    if kind in {"publication", "required_checks", "integration", "completion"}:
        return kind
    if status in {"completed", "abandoned"}:
        return "completion"
    if status == "waiting_checks" or phase == "waiting_checks":
        return "required_checks"
    if phase in {"merged", "completed"} and event.get("pr_number") is not None:
        return "integration"
    if event.get("required_checks_result") is not None:
        return "required_checks"
    if kind == "run_publication" or phase in {
        "publishing",
        "publication_pending",
        "ready_for_approval",
    }:
        return "publication"
    return kind


def _scope_change_details(event: dict[str, Any], *, human: bool = False) -> list[str]:
    if event.get("kind") != "unsupported_scope_change":
        return []
    summary = event.get("graph_change_summary")
    if not isinstance(summary, dict):
        return []
    details = []
    headline = summary.get("summary")
    if isinstance(headline, str):
        details.append(headline)
    for key, label in (
        ("added_tickets", "新增子任务" if human else "新增 Ticket"),
        ("removed_tickets", "移除子任务" if human else "移除 Ticket"),
    ):
        values = summary.get(key)
        if isinstance(values, list) and values:
            details.append(f"{label} {values}")
    return details


def _subject_label(state: dict[str, Any], subject: str) -> str:
    return delivery_object_label(state, subject)


def _role_family(role: str) -> str | None:
    if role == "development":
        return "development"
    if role in {"review", "reviewer", "fresh_acceptance"}:
        return "review"
    if role in {"publication", "final_publication"}:
        return "publication"
    return None


def _invocation_kind(role: str) -> str:
    return _role_family(role) or "agent_invocation"


def _role_label(role: str) -> str:
    family = _role_family(role)
    if family is None:
        return role
    return {
        "development": "开发 Agent",
        "review": "验收 Agent",
        "publication": "发布 Agent",
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
        "development": "开发",
        "review": "验收",
        "publication": "发布",
    }.get(_role_family(role) or "", "Agent")


def _artifact_outcome(artifact: dict[str, Any]) -> str:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return "unknown"
    statuses = {
        check.get("status")
        for check in checks.values()
        if isinstance(check, dict)
    }
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    if statuses and statuses == {"pass"}:
        return "pass"
    return "unknown"


def _supporting_label(kind: str) -> str:
    return {
        "required_checks_evidence": "合并前检查证据",
        "deterministic_integration_record": "PR 合并记录",
        "fallback_publication_receipt": "兜底发布记录",
    }.get(kind, kind)


def _round_label(key: str) -> str:
    scope, _, role = key.partition("_")
    subject = {"ticket": "子任务", "parent": "整体任务", "run": "整体"}.get(scope, "任务")
    action = {"development": "开发", "review": "验收", "publication": "发布说明"}.get(role, "执行")
    return f"{subject}{action}"


def _invocation_attempt_view(invocation: dict[str, Any]) -> dict[str, Any]:
    attempt = invocation.get("semantic_attempt")
    view = dict(attempt) if isinstance(attempt, dict) else {}
    for key in ("work_subject", "generation", "role"):
        if view.get(key) is None and invocation.get(key) is not None:
            view[key] = invocation[key]
    if view.get("role") is None and invocation.get("invocation_role") is not None:
        view["role"] = invocation["invocation_role"]
    return view


def _invocation_duration(
    invocation: dict[str, Any], audit: dict[str, Any]
) -> int | None:
    return invocation_execution_seconds(
        invocation,
        allow_open=invocation_activity(invocation, audit)
        in {"running", "capacity_wait", "recovery_wait"},
    )


def _artifact_findings(artifact: dict[str, Any]) -> list[str]:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return []
    return [
        finding
        for lane in ("e2e", "standards", "spec")
        if isinstance((check := checks.get(lane)), dict)
        and isinstance((lane_findings := check.get("findings")), list)
        for finding in lane_findings
        if isinstance(finding, str)
    ]


def _elapsed_seconds(state: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    return run_elapsed_seconds(state, events=events)


def run_elapsed_seconds(
    state: dict[str, Any], *, events: list[dict[str, Any]] | None = None
) -> int | None:
    started = _parse_optional_timestamp(state.get("created_at"))
    if started is None:
        return None
    if state.get("status") not in {"completed", "abandoned"}:
        return max(0, int((datetime.now(UTC) - started).total_seconds()))
    candidates: list[datetime] = []
    if events is not None:
        candidates.extend(
            timestamp
            for event in events
            if (timestamp := _parse_optional_timestamp(event.get("at"))) is not None
        )
    else:
        candidates.extend(_persisted_event_times(state))
    end = max(candidates, default=started)
    return max(0, int((end - started).total_seconds()))


def _persisted_event_times(state: dict[str, Any]) -> list[datetime]:
    values: list[object] = []
    for key in ("timeline", "timeline_continuation"):
        timeline = state.get(key)
        if isinstance(timeline, list):
            values.extend(
                event.get("at") for event in timeline if isinstance(event, dict)
            )
    invocations = state.get("agent_invocation_history")
    if isinstance(invocations, list):
        for invocation in invocations:
            if isinstance(invocation, dict):
                values.append(
                    invocation.get("ended_at") or invocation.get("started_at")
                )
    resume_audit = state.get("resume_audit")
    resume_history = resume_audit.get("history") if isinstance(resume_audit, dict) else None
    if isinstance(resume_history, list):
        values.extend(
            event.get("requested_at")
            for event in resume_history
            if isinstance(event, dict)
        )
    return [
        timestamp
        for value in values
        if (timestamp := _parse_optional_timestamp(value)) is not None
    ]


def _utc_timestamp(value: str) -> str | None:
    try:
        return _parse_timestamp(value).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_timestamp(value).astimezone(UTC)
    except ValueError:
        return None


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
    if not isinstance(seconds, int):
        return "未知"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


def _output_continuation_text(value: object) -> str:
    if type(value) is int and value >= 1:
        return str(value - 1)
    return "未记录"


def _truncate_history_detail(value: str) -> str:
    if len(value) <= HISTORY_DETAIL_LIMIT:
        return value
    keep = HISTORY_DETAIL_LIMIT - len(_TRUNCATION_MARKER)
    return value[:keep] + _TRUNCATION_MARKER


def _operator_instruction(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    cleanup = cleanup_instruction(state)
    if cleanup:
        return cleanup
    if invocation is not None and invocation.get("status") in {"running", "resuming"}:
        return "你暂时无需操作。"
    if state.get("status") in {"completed", "abandoned"}:
        return "无需操作。"
    return "按上述命令继续；不要重复启动同一项任务。"


def _current_wait_lines(state: dict[str, Any], audit: dict[str, Any]) -> list[str]:
    waiting = waiting_presentation(state, audit)
    if waiting is None:
        return []
    lines = [f"当前工作：{_safe_text(waiting.work)}", f"执行情况：{_safe_text(waiting.activity)}"]
    lines.extend(f"{label}：{_safe_text(value)}" for label, value in waiting.details)
    lines.append(f"下一步：{_safe_text(waiting.guidance)}")
    if waiting.show_command:
        lines.append(f"命令：{human_next_action(audit.get('next_action'), run_id=state.get('run_id'))}")
    return lines
