from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_run.delivery_status import (
    execution_guidance,
    invocation_activity,
    invocation_recovery_details,
)
from agent_run.presentation_helpers import (
    delivery_object_label,
    human_next_action,
)


HISTORY_DETAIL_LIMIT = 240
_TRUNCATION_MARKER = "…（已截断；完整内容见 --json）"

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
) -> None:
    timezone, timezone_label = _local_timezone()
    parent = state.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    print(f"Repository: {state.get('repository') or 'unknown'}")
    print(
        "Parent:     "
        f"#{parent_view.get('number', '?')} {parent_view.get('title') or '未命名 Parent'}"
    )
    print(f"Time zone: {timezone_label}")
    print(f"Elapsed:   {_duration(progress['summary']['elapsed_seconds'])}")
    executor_control = audit.get("executor_control")
    if (
        isinstance(executor_control, dict)
        and executor_control.get("activity") == "unknown"
    ):
        print("Agent 活跃状态: 无法确认（运行状态无法确认）")
    if progress["execution_activity"] == "interrupted":
        print("执行已中断，等待恢复")
    print("\n事件")
    for event in progress["events"]:
        timestamp = _parse_timestamp(event["at"])
        local_time = timestamp.astimezone(timezone).strftime("%Y-%m-%d %H:%M %z")
        subject = event.get("object") or "Delivery Run"
        role = event.get("role")
        round_number = event.get("round")
        role_text = f" · {role}" if role else ""
        round_text = f" 第 {round_number} 轮" if isinstance(round_number, int) else ""
        status = (
            "执行已中断，等待恢复" if event.get("activity") == "interrupted"
            else "运行状态无法确认" if event.get("activity") == "unknown"
            else "模型容量不足，等待自动续接" if event.get("activity") == "capacity_wait"
            else "执行异常，等待自动续接" if event.get("activity") == "recovery_wait"
            else display_term(event.get("status"))
        )
        print(f"{local_time}  {subject}{role_text}{round_text} · {status}")
        model = event.get("model")
        effort = event.get("reasoning_effort")
        invocation_role = event.get("invocation_role")
        duration = event.get("duration_seconds")
        config = []
        if invocation_role:
            config.append(f"Agent Invocation {invocation_role}")
        if model:
            config.append(f"model={model}")
        if effort:
            config.append(f"reasoning_effort={effort}")
        if invocation_role and duration is None:
            config.append("实际执行时长未知")
        if duration is not None:
            config.append(f"耗时 {_duration(duration)}")
        if config:
            print(f"       {' / '.join(config)}")
        for detail in event.get("details", []):
            print(f"       - {_truncate_history_detail(str(detail))}")

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
    if progress["execution_activity"] in {"interrupted", "unknown", "capacity_wait", "recovery_wait"}:
        print(f"\n下一步: {execution_guidance(state, progress['execution_activity'])}")
        return
    operator_action = audit.get("operator_action")
    _print_wait(
        audit.get("supervision"),
        display_term=display_term,
        run_id=state.get("run_id"),
    )
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
            events.append(
                {
                    "at": timestamp,
                    "kind": kind,
                    "object": _timeline_object(state, raw),
                    "phase": raw.get("phase"),
                    "status": raw.get("status"),
                    "role": raw.get("worker"),
                    "round": raw.get("attempt"),
                    "details": details,
                    "_order": source_order,
                }
            )
            source_order += 1

    findings = _review_findings_by_subject(state)
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
            attempt_view = attempt if isinstance(attempt, dict) else {}
            work_subject = str(invocation.get("work_subject") or "")
            role = str(
                attempt_view.get("role")
                or invocation.get("invocation_role")
                or invocation.get("role")
                or "unknown"
            )
            round_number = attempt_view.get("ordinal")
            review_details = _findings_for_review(
                findings,
                work_subject,
                attempt_view.get("budget_window"),
                round_number,
                role,
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
                    "role": _role_label(role),
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


def _review_findings_by_subject(
    state: dict[str, Any],
) -> dict[str, dict[int, list[list[str]]]]:
    pools: dict[str, dict[int, list[list[str]]]] = {}
    run_id = state.get("run_id")
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for number, job in jobs.items():
            if isinstance(job, dict):
                pools[f"ticket:{number}"] = _subject_review_findings(job)
    parent = state.get("parent_job")
    if isinstance(parent, dict):
        pools[f"parent-only:{run_id}"] = _subject_review_findings(parent)
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        pools[f"run-acceptance:{run_id}"] = _subject_review_findings(acceptance)
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            pools[f"run-repair:{run_id}"] = _subject_review_findings(repair)
    return pools


def _subject_review_findings(subject: dict[str, Any]) -> dict[int, list[list[str]]]:
    budgets = (
        [
            snapshot.get("review_budget")
            for snapshot in subject.get("review_budget_history", [])
            if isinstance(snapshot, dict)
        ]
        if isinstance(subject.get("review_budget_history"), list)
        else []
    )
    budgets.append(subject.get("review_budget"))
    findings: dict[int, list[list[str]]] = {}
    for budget in budgets:
        window = budget.get("window") if isinstance(budget, dict) else None
        if not isinstance(window, int):
            continue
        artifacts = (
            budget.get("review_artifacts") if isinstance(budget, dict) else None
        )
        if not isinstance(artifacts, list):
            continue
        window_findings: list[list[str]] = []
        for record in artifacts:
            artifact = record.get("artifact") if isinstance(record, dict) else None
            window_findings.append(
                _artifact_findings(artifact) if isinstance(artifact, dict) else []
            )
        findings[window] = window_findings
    return findings


def _findings_for_review(
    pools: dict[str, dict[int, list[list[str]]]],
    subject: str,
    budget_window: object,
    ordinal: object,
    role: str,
) -> list[str]:
    if (
        role not in {"reviewer", "fresh_acceptance"}
        or not isinstance(budget_window, int)
        or not isinstance(ordinal, int)
    ):
        return []
    reviews = pools.get(subject, {}).get(budget_window, [])
    return reviews[ordinal - 1] if 0 < ordinal <= len(reviews) else []


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


def _timeline_kind(event: dict[str, Any], details: list[str]) -> str:
    if details:
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


def _scope_change_details(event: dict[str, Any]) -> list[str]:
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
        ("added_tickets", "新增 Ticket"),
        ("removed_tickets", "移除 Ticket"),
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
        "development": "Development Agent",
        "review": "Review Agent",
        "publication": "Publication Agent",
    }[family]


def _round_label(key: str) -> str:
    scope, _, role = key.partition("_")
    return f"{scope.title()} {'Review' if role == 'review' else role.title()}"


def _invocation_duration(
    invocation: dict[str, Any], audit: dict[str, Any]
) -> int | None:
    start = _parse_optional_timestamp(invocation.get("started_at"))
    end = _parse_optional_timestamp(invocation.get("ended_at"))
    if start is None:
        return None
    if end is None:
        if invocation_activity(invocation, audit) != "running":
            return None
        end = datetime.now(UTC)
    return max(0, int((end - start).total_seconds()))


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


def _truncate_history_detail(value: str) -> str:
    if len(value) <= HISTORY_DETAIL_LIMIT:
        return value
    keep = HISTORY_DETAIL_LIMIT - len(_TRUNCATION_MARKER)
    return value[:keep] + _TRUNCATION_MARKER


def _operator_instruction(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    if invocation is not None and invocation.get("status") in {"running", "resuming"}:
        return "你暂时无需操作。"
    if state.get("status") in {"completed", "abandoned"}:
        return "无需操作。"
    return "按上述命令继续；不要重复启动另一个 Run。"


def _print_wait(
    wait: object,
    *,
    display_term: Callable[[object], object],
    run_id: object,
) -> None:
    if not isinstance(wait, dict):
        return
    print("\n当前等待")
    print(f"  等待种类: {display_term(wait.get('kind'))}")
    print(f"  等待对象: {wait.get('subject')}")
    boundary_status = (
        "已记录（完整值见 --json）"
        if wait.get("head_sha") is not None or wait.get("base_sha") is not None
        else "尚未取得"
    )
    print(f"  等待 head/base: {boundary_status}")
    print(
        "  等待窗口: "
        f"截止={wait.get('deadline')}；剩余={wait.get('remaining_seconds')} 秒"
    )
    print(f"  重试次数: {wait.get('retry_count')}")
    observation = wait.get("latest_observation")
    if isinstance(observation, dict):
        print(f"  最新观测: {observation.get('message')}")
    else:
        print("  最新观测: 无")
    if wait.get("timeout_resume_action"):
        print(
            "  超时恢复: "
            f"{human_next_action(wait['timeout_resume_action'], run_id=run_id)}"
        )
    if wait.get("credential_failure_class"):
        print(f"  凭据失败类别: {wait['credential_failure_class']}")
    if wait.get("credential_http_status") is not None:
        print(f"  凭据 HTTP 状态: {wait['credential_http_status']}")
