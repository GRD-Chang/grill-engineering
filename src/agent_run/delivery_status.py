from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.text import Text

from agent_run.waiting_presentation import cleanup_instruction, waiting_presentation
from agent_run.final_approval_operation import final_approval_cleanup_pending
from agent_run.operator_action_presentation import (
    human_action_type,
    human_preserved_results,
)
from agent_run.presentation_helpers import (
    current_work_subject,
    delivery_object_label,
    human_ci_fix_usage,
    human_delivery_object,
    human_agent_role,
    human_next_action,
    human_pause_reason,
    human_status_term,
    local_timestamp,
    terminal_safe,
    status_diagnostic_command,
    unknown_execution_guidance,
)


def status_progress_view(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    """Build the operator-facing status facts without weakening the audit view."""

    parent = state.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    invocation = _progress_invocation(state, audit)
    worker = audit.get("worker")
    current = current_work_subject(state)
    current_object = _current_object(state, current)
    current_agent = _agent_view(
        state,
        invocation,
        worker if isinstance(worker, dict) else None,
        current_object=current_object,
    )
    activity = invocation_activity(invocation, audit)
    findings = _current_findings(state)
    if current_agent is not None:
        current_agent["activity"] = activity
        if activity != "running":
            current_agent.update(
                is_active=False,
                duration_seconds=_invocation_duration(
                    invocation or {},
                    allow_open=activity in {"capacity_wait", "recovery_wait"},
                ),
                remaining_seconds=None,
            )
    return {
        "repository": state.get("repository"),
        "parent": {
            "number": parent_view.get("number"),
            "title": parent_view.get("title"),
        },
        "status": audit.get("status"),
        "phase": audit.get("phase"),
        "current_object": current_object,
        "ticket_progress": _ticket_progress(state),
        "round_progress": _round_progress(
            state, current, audit.get("review_budget"), invocation
        ),
        "run_repair": _run_repair_progress(audit.get("run_repair")),
        "elapsed_seconds": audit.get("elapsed_seconds"),
        "current_agent": current_agent,
        "execution_activity": activity,
        "findings": findings,
        "conclusion": _current_conclusion(
            state, current, findings, activity=activity
        ),
        "next_action": audit.get("next_action"),
    }


def invocation_recovery_details(invocation: dict[str, Any]) -> list[str]:
    """Summarize cumulative recovery facts without adding timeline events."""
    details: list[str] = []
    if invocation.get("ordinary_recovery_used") is True:
        details.append("本轮累计：普通异常自动续接已触发 1 次")
    capacity_count = invocation.get("capacity_recovery_count")
    if type(capacity_count) is int and capacity_count > 0:
        details.append(f"本轮累计：模型容量不足等待已触发 {capacity_count} 次，每次等待 30 秒后自动续接")
    return details


def invocation_execution_seconds(
    invocation: dict[str, Any], *, allow_open: bool = False
) -> int | None:
    """Return trusted active execution time, excluding persisted recovery waits."""

    started = _parse_optional_timestamp(invocation.get("started_at"))
    ended = _parse_optional_timestamp(invocation.get("ended_at"))
    if started is None:
        return None
    if ended is None:
        if not allow_open:
            return None
        ended = datetime.now(UTC)
    if ended < started:
        return None

    waits = _recovery_wait_intervals(invocation, started=started, ended=ended)
    if waits is None:
        return None
    wait_seconds = 0.0
    previous_end = started
    for wait_start, wait_end in sorted(waits):
        if wait_start < started or wait_end > ended or wait_start < previous_end:
            return None
        wait_seconds += (wait_end - wait_start).total_seconds()
        previous_end = wait_end
    return max(0, int((ended - started).total_seconds() - wait_seconds))


def _recovery_wait_intervals(
    invocation: dict[str, Any], *, started: datetime, ended: datetime
) -> list[tuple[datetime, datetime]] | None:
    raw_intervals = invocation.get("recovery_wait_intervals")
    if isinstance(raw_intervals, list) and not raw_intervals:
        # An explicitly persisted empty list belongs to this Invocation. In
        # particular, a successor may retain the semantic Attempt's cumulative
        # recovery count while having no waits of its own.
        if not invocation.get("recovery_waiting") and not any(
            invocation.get(key)
            for key in ("last_failure_at", "last_recovery_started_at")
        ):
            return []
        # A legacy/current waiting record can have an empty list because its
        # interval was not persisted. Fall through to the conservative legacy
        # reconstruction below rather than claiming all elapsed time ran.
        raw_intervals = None
    if raw_intervals is None:
        # Older records only have enough evidence for one capacity wait. More
        # than one unversioned wait cannot be reconstructed safely.
        count = invocation.get("capacity_recovery_count")
        if count in (None, 0) and not any(
            invocation.get(key)
            for key in ("last_failure_at", "last_recovery_started_at")
        ):
            return []
        if count not in (None, 0, 1):
            return None
        raw_intervals = [
            {
                "started_at": invocation.get("last_failure_at"),
                "ended_at": invocation.get("last_recovery_started_at"),
            }
        ]
    if not isinstance(raw_intervals, list):
        return None

    intervals: list[tuple[datetime, datetime]] = []
    for raw in raw_intervals:
        if not isinstance(raw, dict):
            return None
        wait_start = _parse_optional_timestamp(raw.get("started_at"))
        wait_end = _parse_optional_timestamp(raw.get("ended_at"))
        if wait_start is None:
            return None
        if wait_end is None:
            if not invocation.get("recovery_waiting"):
                return None
            wait_end = ended
        if wait_end < wait_start:
            return None
        intervals.append((wait_start, wait_end))
    return intervals


def invocation_activity(
    invocation: dict[str, Any] | None, audit: dict[str, Any]
) -> str:
    """Project execution evidence separately from persisted Invocation status."""
    if not invocation or invocation.get("status") not in {"running", "resuming"}:
        return "not_running"
    control = audit.get("executor_control")
    activity = control.get("activity") if isinstance(control, dict) else "unknown"
    if activity == "not_running":
        return "interrupted"
    if activity != "running":
        return "unknown"
    if invocation.get("recovery_waiting") is True:
        return (
            "capacity_wait"
            if invocation.get("recovery_kind") == "capacity"
            else "recovery_wait"
        )
    return "running"


def execution_guidance(
    state: dict[str, Any], activity: str, *, include_resume_command: bool = True
) -> str:
    if activity == "capacity_wait":
        return "模型容量不足，等待 30 秒后自动续接；当前 Agent 未在生成，可使用 stop 停止。"
    if activity == "recovery_wait":
        return "执行异常，正在自动续接原工作；当前 Agent 未在生成。"
    if activity == "interrupted":
        guidance = "执行已中断，等待恢复"
        if include_resume_command:
            parent = state.get("parent")
            number = parent.get("number") if isinstance(parent, dict) else None
            repository = state.get("repository")
            command = (
                f"agent-run resume {number} --repo {repository}"
                if type(number) is int and isinstance(repository, str)
                else "agent-run resume <run-id>"
            )
            guidance += f"；使用 {command} 继续原工作"
        return f"{guidance}。"
    return unknown_execution_guidance(state, include_command=include_resume_command)


def print_status_progress(
    state: dict[str, Any],
    audit: dict[str, Any],
    view: dict[str, Any],
    *,
    display_term: Callable[[object], object],
    print_operator_action: Callable[[dict[str, Any]], None],
) -> None:
    parent = view["parent"]
    print(f"仓库:       {_terminal_safe(view['repository'] or '未知')}")
    print(
        "整体需求:   "
        f"#{_terminal_safe(parent.get('number', '?'))} "
        f"{_terminal_safe(parent.get('title') or '未命名需求')}"
    )
    status_label = (
        "已合并，待清理" if state.get("status") == "completed"
        and final_approval_cleanup_pending(state) else display_term(view["status"])
    )
    print(f"状态:       {_terminal_safe(status_label)}")
    phase = _phase_term(view["phase"], view["current_object"], display_term)
    print(
        f"阶段:       {_terminal_safe(phase if phase is not None else '未进入具体阶段')}"
    )
    print(f"结论:       {_terminal_safe(_human_conclusion(view['conclusion']))}")
    print(f"当前对象:   {_terminal_safe(human_delivery_object(view['current_object']))}")

    print("\n进度")
    print(f"  {_ticket_progress_text(state, view)}")
    rounds = view["round_progress"]
    if rounds is not None:
        for line in _round_details(rounds):
            print(f"  {_terminal_safe(line)}")
    print(f"  总运行时长           {_duration(view['elapsed_seconds'])}")

    repair = view["run_repair"]
    if repair is not None:
        print("\n整体修复")
        print(
            "  当前版本验收 "
            f"{_terminal_safe(display_term(repair['candidate_validation_status']))}"
        )
        print(
            "  代码修改 "
            f"{repair['code_modification_attempts']} / "
            f"{repair['code_modification_limit']}"
        )

    wait_view = waiting_presentation(state, audit)
    if wait_view is not None:
        print("\n当前工作")
        print(f"  {_terminal_safe(wait_view.work)}")
        print(f"  {_terminal_safe(wait_view.activity)}")
        for label, text in wait_view.details:
            print(f"  {_terminal_safe(label)}：{_terminal_safe(text)}")
        _print_cleanup(audit.get("delivery_cleanup"))
        _print_scope_change(audit.get("scope_change"))
        print("\n下一步")
        print(f"  {_terminal_safe(wait_view.guidance)}")
        action = audit.get("operator_action")
        if isinstance(action, dict):
            print_operator_action(action)
        elif wait_view.show_command:
            print(f"  {_terminal_safe(view['next_action'])}")
        _print_findings(view["findings"])
        return

    print("\n当前工作")
    executor_control = audit.get("executor_control")
    if (
        isinstance(executor_control, dict)
        and executor_control.get("activity") == "unknown"
    ):
        print("  Agent 活跃状态: 无法确认（运行状态无法确认）")
    activity = view["execution_activity"]
    if activity == "interrupted":
        print("  执行已中断，等待恢复")
    agent = view["current_agent"]
    if agent is None:
        print("  当前没有运行中的 Agent")
    else:
        agent_label = "当前 Agent" if agent["is_active"] else "最近 Agent"
        print(
            f"  {agent_label}: {_terminal_safe(human_agent_role(agent['role']))} · "
            f"{_terminal_safe(human_delivery_object(agent['object']))}"
        )
        print(f"  模型                  {_terminal_safe(agent['model'])}")
        print(f"  推理强度              {_terminal_safe(agent['reasoning_effort'])}")
        if agent.get("started_at"):
            print(f"  开始时间              {local_timestamp(agent['started_at'])}")
        if agent["duration_seconds"] is None:
            print("  实际执行时长未知")
        if agent["duration_seconds"] is not None:
            duration_label = "本轮已运行" if agent["is_active"] else "本轮耗时"
            print(f"  {duration_label:<20}{_duration(agent['duration_seconds'])}")
        if agent["remaining_seconds"] is not None:
            print(f"  本轮剩余              {_duration(agent['remaining_seconds'])}")
        for detail in agent["recovery_details"]:
            print(f"  {_terminal_safe(detail)}")

    if activity in {"interrupted", "unknown", "capacity_wait", "recovery_wait"}:
        print("\n下一步")
        print(f"  {_terminal_safe(execution_guidance(state, activity))}")
        _print_findings(view["findings"])
        return

    operator_action = audit.get("operator_action")
    if not isinstance(operator_action, dict):
        _print_cleanup(audit.get("delivery_cleanup"))
        _print_scope_change(audit.get("scope_change"))
        print("\n下一步")
        cleanup = audit.get("delivery_cleanup")
        if isinstance(cleanup, dict) and cleanup.get("status") == "cleanup_pending":
            print("  查看 --json 中的交付清理诊断，整理受管工作区后按恢复操作继续。")
            print(
                "  恢复命令: "
                f"{_terminal_safe(human_next_action(view['next_action'], run_id=state.get('run_id')))}"
            )
        elif activity != "running":
            print(
                "  下一步: "
                f"{_terminal_safe(human_next_action(view['next_action'], run_id=state.get('run_id')))}"
            )
        current_invocation = audit.get("agent_invocation")
        running_invocation = (
            current_invocation if isinstance(current_invocation, dict) else None
        )
        print(
            "  "
            f"{_operator_instruction(state, running_invocation)}"
        )
        _print_findings(view["findings"])
        return

    _print_scope_change(audit.get("scope_change"))
    print()
    print_operator_action(operator_action)
    _print_findings(view["findings"])


def print_rich_status_progress(
    state: dict[str, Any],
    audit: dict[str, Any],
    view: dict[str, Any],
) -> None:
    """Render one static status card for an interactive terminal."""

    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # Let Rich wrap Text within panels instead of treating long values as
    # no-wrap content and clipping their business data at the panel edge.
    console = Console(highlight=False, soft_wrap=False)
    raw_status = str(view.get("status") or "")
    status_style = _status_style(raw_status)
    status_label = (
        "已合并，待清理" if raw_status == "completed"
        and final_approval_cleanup_pending(state) else _status_term(raw_status)
    )

    parent = view.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    identity = Table.grid(expand=True, padding=(0, 1))
    narrow = console.width < 72
    if narrow:
        identity.add_column(ratio=1, overflow="fold")
    else:
        identity.add_column(style="bold", no_wrap=True)
        identity.add_column(ratio=1, overflow="fold")

    def add_identity(label: str, value: object) -> None:
        if narrow:
            identity.add_row(_rich_labeled(label, value))
        else:
            identity.add_row(label, _rich_value(value))

    add_identity(
        "任务",
        f"{view.get('repository') or 'unknown'} · "
        f"#{parent_view.get('number', '?')} "
        f"{parent_view.get('title') or '未命名需求'}",
    )
    add_identity(
        "状态",
        f"{_status_symbol(raw_status)} {_human_conclusion(view.get('conclusion'))} · "
        f"{status_label}",
    )
    add_identity(
        "阶段",
        _phase_term(view.get("phase"), view.get("current_object"), _status_term)
        or "未进入具体阶段",
    )
    add_identity("当前对象", human_delivery_object(view.get("current_object")))

    progress_text = _ticket_progress_text(state, view)
    rounds = view.get("round_progress")
    if isinstance(rounds, dict):
        progress_text += "；" + "；".join(_round_details(rounds))
    repair = view.get("run_repair")
    if isinstance(repair, dict):
        progress_text += (
            "；整体修复："
            f"当前版本验收 {_status_term(repair.get('candidate_validation_status'))}，"
            f"代码修改 {repair.get('code_modification_attempts')} / "
            f"{repair.get('code_modification_limit')}"
        )
    progress_text += f"；总运行时长 {_duration(view.get('elapsed_seconds'))}"
    add_identity("进度与预算", progress_text)

    wait_view = waiting_presentation(state, audit)
    agent = view.get("current_agent")
    if wait_view is not None:
        agent_text = f"{wait_view.work}\n{wait_view.activity}"
    elif isinstance(agent, dict):
        label = "当前 Agent" if agent.get("is_active") else "最近 Agent"
        agent_text = (
            f"{label}：{human_agent_role(agent.get('role'))} · "
            f"{human_delivery_object(agent.get('object'))}\n"
            f"模型：{agent.get('model') or '未绑定'}；推理强度："
            f"{agent.get('reasoning_effort') or '未绑定'}\n"
            f"执行时长：{_duration(agent.get('duration_seconds'))}"
        )
        if agent.get("started_at"):
            agent_text += f"；开始时间：{local_timestamp(agent['started_at'])}"
        if agent.get("remaining_seconds") is not None:
            agent_text += f"；本轮剩余：{_duration(agent['remaining_seconds'])}"
        recovery_details = agent.get("recovery_details")
        if isinstance(recovery_details, list):
            agent_text += "\n" + "\n".join(str(item) for item in recovery_details)
    else:
        agent_text = "当前没有可确认的 Agent 工作记录"
    add_identity("当前工作", agent_text)

    console.print(
        Panel(
            identity,
            title=Text("agent-run · 交付状态卡", style="bold"),
            border_style=status_style,
            expand=True,
        )
    )

    action = audit.get("operator_action")
    activity = view.get("execution_activity")
    control = audit.get("executor_control")
    unknown_wait = (
        wait_view is not None and isinstance(control, dict)
        and control.get("activity") == "unknown" and state.get("status") != "supervision_timeout"
    )
    needs_diagnostic = activity == "unknown" or unknown_wait
    agent_running = wait_view is None and activity == "running" and not isinstance(action, dict)
    command = (
        status_diagnostic_command(state) if needs_diagnostic else view.get("next_action")
        or (action.get("next_action") if isinstance(action, dict) else None)
    )
    command_line = _rich_labeled("命令", command)
    action_lines: list[Text] = []
    if wait_view is not None and not isinstance(action, dict):
        action_lines.append(_rich_value(
            unknown_execution_guidance(state, include_command=False)
            if unknown_wait else wait_view.guidance
        ))
    elif activity in {"interrupted", "unknown", "capacity_wait", "recovery_wait"}:
        action_lines.append(
            _rich_value(
                execution_guidance(
                    state, str(activity), include_resume_command=False
                )
            )
        )
    elif isinstance(action, dict):
        action_lines.append(
            _rich_labeled("类型", _human_action_type(action.get("type")))
        )
        for reason in action.get("reasons", []):
            message = reason if action.get("type") == "Human Blocker" else human_pause_reason(reason)
            action_lines.append(_rich_labeled("原因", message))
        action_lines.append(
            _rich_labeled("已保留成果", _rich_preserved_results(action.get("preserved")))
        )
        action_lines.append(
            _rich_labeled(
                "阻塞阶段",
                _status_term(action.get("phase")) or action.get("phase") or "未知",
            )
        )
        triggering = action.get("trigger_invocation")
        if isinstance(triggering, dict):
            action_lines.append(
                _rich_labeled(
                    "触发阻塞的 Agent",
                    (
                        f"{_rich_role_label(triggering.get('role'))}；"
                        f"模型 {triggering.get('model')}；"
                        f"推理强度 {triggering.get('reasoning_effort')}；"
                        f"本轮时长 {triggering.get('duration_seconds')} 秒"
                    ),
                )
            )
        action_lines.append(Text("整项任务已暂停，其他子任务也不会继续。"))
        if action.get("type") == "Review Budget Checkpoint":
            action_lines.append(Text("继续执行后，将按配置补充本次开发与验收额度，继续已有工作。"))
    else:
        action_lines.append(
            _rich_value(_operator_instruction(state, audit.get("agent_invocation")))
        )
    if wait_view is not None:
        action_lines.extend(_rich_labeled(label, text) for label, text in wait_view.details)
    cleanup = audit.get("delivery_cleanup")
    if isinstance(cleanup, dict):
        cleanup_status = {
            "completed": "已完成", "cleanup_pending": "等待清理", "pending": "待处理",
        }.get(str(cleanup.get("status")), cleanup.get("status"))
        action_lines.append(_rich_labeled("工作区清理", cleanup_status))
        reason = _cleanup_reason(cleanup)
        if reason:
            action_lines.append(_rich_labeled("保留原因", reason))
        for path in _cleanup_paths(cleanup):
            action_lines.append(_rich_labeled("待处理工作区", path))
    scope_change = audit.get("scope_change")
    if isinstance(scope_change, dict):
        summary = scope_change.get("graph_change_summary")
        action_lines.append(
            _rich_labeled(
                "任务与依赖变化",
                summary.get("summary") if isinstance(summary, dict) else "见 --json",
            )
        )
    # Put the executable next step before the detailed action panel so it is
    # visible in the first screen. Keep it outside Rich panels so wrapping
    # never inserts copy-breaking borders, padding, or hard newlines.
    if not agent_running and command and command != "无" and (
        needs_diagnostic or wait_view is None or wait_view.show_command or isinstance(action, dict)
    ):
        console.file.write(command_line.plain + "\n")
    console.print(
        Panel(
            Group(*action_lines),
            title=Text("下一步", style="bold yellow"),
            border_style="yellow" if action is not None else "cyan",
            expand=True,
        )
    )

    findings = view.get("findings")
    finding_values = findings if isinstance(findings, list) else []
    finding_renderables: list[Text] = []
    if not finding_values:
        finding_renderables.append(Text("当前没有属于本工作和当前代码版本的问题"))
    else:
        for index, finding in enumerate(finding_values, start=1):
            finding_renderables.append(Text(f"{index}.", style="bold red"))
            match = _FINDING_PARTS.fullmatch(str(finding))
            if match is None:
                finding_renderables.append(_rich_value(finding))
                continue
            for label, value in zip(
                ("问题", "证据", "必须修复", "复验"), match.groups()
            ):
                finding_renderables.append(_rich_labeled(label, value))
    console.print(
        Panel(
            Group(*finding_renderables),
            title=Text(f"当前问题（{len(finding_values)}）", style="bold"),
            border_style="red" if finding_values else "bright_black",
            expand=True,
        )
    )


def _status_style(value: object) -> str:
    return {
        "completed": "green",
        "accepted": "green",
        "ticket_completed": "green",
        "merged": "green",
        "run_publication_pending": "green",
        "active": "cyan",
        "starting": "cyan",
        "developing": "cyan",
        "repairing": "cyan",
        "reviewing": "cyan",
        "validating": "cyan",
        "publishing": "cyan",
        "ready_for_human": "yellow",
        "blocked": "yellow",
        "pending": "yellow",
        "run_approval_pending": "yellow",
        "run_acceptance_pending": "yellow",
        "ready_for_approval": "yellow",
        "waiting_checks": "yellow",
        "waiting_merge": "yellow",
        "waiting_external": "yellow",
        "publication_pending": "yellow",
        "parent_approval_pending": "yellow",
        "parent_delivery_pending": "yellow",
        "operator_stopped": "yellow",
        "supervision_timeout": "yellow",
        "execution_failed": "red",
    }.get(str(value), "cyan")


def _rich_value(value: object) -> Text:
    from rich.text import Text

    return Text(_terminal_safe("未知" if value is None else value))


def _rich_labeled(label: object, value: object) -> Text:
    from rich.text import Text

    text = Text()
    text.append(f"{_terminal_safe(label)}：", style="bold")
    text.append(_terminal_safe("未知" if value is None else value))
    return text


def _terminal_safe(value: object) -> str:
    """Keep persisted/user text from injecting terminal control sequences."""

    return terminal_safe(value)


def _rich_preserved_results(value: object) -> str:
    return human_preserved_results(value)


def _status_symbol(value: str) -> str:
    if value in {"completed"}:
        return "✓"
    if value in {"execution_failed", "blocked"}:
        return "!"
    if value in {"ready_for_human", "operator_stopped", "supervision_timeout"}:
        return "?"
    return "→"


def _status_term(value: object) -> object:
    return human_status_term(value)


def _phase_term(
    value: object,
    current_object: object,
    fallback: Callable[[object], object],
) -> object:
    if value == "pending":
        return {
            "Run Acceptance": "待验收",
            "Run Publication": "待发布",
        }.get(str(current_object), "待处理")
    return fallback(value)


def _human_conclusion(value: object) -> object:
    return "当前版本已通过验收" if value == "当前有效通过" else value


def _human_action_type(value: object) -> str:
    return human_action_type(value)


def _agent_view(
    state: dict[str, Any],
    invocation: dict[str, Any] | None,
    worker: dict[str, Any] | None,
    *,
    current_object: str,
) -> dict[str, Any] | None:
    if invocation is None and worker is None:
        return None
    source = invocation or {}
    role = (
        source.get("invocation_role")
        or source.get("role")
        or (worker or {}).get("role")
        or "Agent"
    )
    started_at = source.get("started_at")
    deadline_at = source.get("deadline_at")
    is_active = source.get("status") in {"running", "resuming"}
    work_subject = source.get("work_subject")
    agent_object = (
        _subject_label(state, work_subject)
        if isinstance(work_subject, str) and work_subject
        else current_object
    )
    return {
        "role": _role_label(str(role)),
        "started_at": started_at,
        "recovery_details": invocation_recovery_details(source),
        "model": source.get("model") or "未绑定",
        "reasoning_effort": source.get("reasoning_effort") or "未绑定",
        "duration_seconds": _invocation_duration(source, allow_open=is_active),
        "remaining_seconds": _remaining_until(deadline_at) if is_active else None,
        "is_active": is_active and agent_object == current_object,
        "object": agent_object,
    }


def _progress_invocation(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any] | None:
    active = audit.get("agent_invocation")
    if isinstance(active, dict):
        return active
    history = state.get("agent_invocation_history")
    if isinstance(history, list):
        for invocation in reversed(history):
            if isinstance(invocation, dict):
                return invocation
    return None


def _run_repair_progress(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "acceptance_generation": value.get("acceptance_generation"),
        "repair_cycle_generation": value.get("repair_cycle_generation"),
        "candidate_validation_status": value.get("candidate_validation_status"),
        "cycle_status": value.get("cycle_status"),
        "code_modification_attempts": value.get("code_modification_attempts"),
        "code_modification_limit": value.get("code_modification_limit"),
    }


def _ticket_progress_text(state: dict[str, Any], view: dict[str, Any]) -> str:
    if state.get("delivery_type") == "parent_only":
        return "无子任务，直接推进需求"
    progress = view.get("ticket_progress")
    if state.get("delivery_type") != "ticket_run" or not isinstance(progress, dict):
        return "无法确认进度"
    completed, total = progress.get("completed"), progress.get("total")
    if (
        type(completed) is not int or type(total) is not int
        or total <= 0 or not 0 <= completed <= total
    ):
        return "无法确认进度"
    text = f"已完成 {completed} / {total} 个子任务"
    if completed != total or state.get("status") == "completed":
        return text
    # Only active, normal workflow states may add a next-stage promise.
    if state.get("status") not in {
        "active", "run_acceptance_pending", "run_publication_pending",
        "run_approval_pending",
    }:
        return text
    publication = state.get("run_publication")
    publication_phase = publication.get("phase") if isinstance(publication, dict) else None
    acceptance = state.get("run_acceptance")
    acceptance_phase = acceptance.get("phase") if isinstance(acceptance, dict) else None
    if publication_phase == "ready_for_approval":
        return text + "，等待你确认后发布"
    if publication_phase == "publishing":
        return text + "，正在发布"
    if publication_phase not in {None, "pending"}:
        return text
    if acceptance_phase == "reviewing":
        return text + "，正在验收"
    if acceptance_phase in {None, "pending"}:
        return text + "，还要验收并发布"
    return text


def _ticket_progress(state: dict[str, Any]) -> dict[str, int]:
    graph = state.get("ticket_graph")
    tickets = graph.get("tickets") if isinstance(graph, dict) else None
    total = len(tickets) if isinstance(tickets, dict) else 0
    jobs = state.get("ticket_jobs")
    graph_numbers = set(tickets) if isinstance(tickets, dict) else set()
    completed = (
        sum(
            1
            for number, job in jobs.items()
            if (not graph_numbers or str(number) in graph_numbers)
            if isinstance(job, dict)
            and job.get("phase") in {"completed", "merged"}
        )
        if isinstance(jobs, dict)
        else 0
    )
    return {"completed": completed, "total": total}


def _round_progress(
    state: dict[str, Any],
    current: tuple[str, dict[str, Any]] | None,
    budget: object,
    invocation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(budget, dict):
        return None
    location = current[0] if current is not None else ""
    if location.startswith("ticket:"):
        prefix = "Ticket"
    elif location == "parent":
        prefix = "Parent"
    else:
        prefix = "Run"
    cycle_subject = current[1] if current is not None else {}
    if location == "run_publication":
        acceptance = state.get("run_acceptance")
        if isinstance(acceptance, dict):
            cycle_subject = acceptance
    cycle_development_attempts = _attempt_count(
        cycle_subject, "modification_attempts", "code_modification_attempts"
    )
    cycle_review_attempts = _attempt_count(cycle_subject, "validation_attempts")
    development_attempt, review_attempt = _attempt_ordinals(
        cycle_subject, invocation
    )
    return {
        **budget,
        "development_label": f"{prefix} Development",
        "review_label": f"{prefix} Review",
        "cycle_development_attempts": cycle_development_attempts,
        "cycle_review_attempts": cycle_review_attempts,
        "development_attempt": development_attempt,
        "review_attempt": review_attempt,
    }


def _round_details(rounds: dict[str, Any]) -> list[str]:
    scope = {
        "Ticket Development": "当前子任务",
        "Parent Development": "当前整体需求",
        "Run Development": "当前整体验收周期",
    }.get(str(rounds.get("development_label")), "当前工作")
    details = []
    if rounds.get("cycle_development_attempts") is not None:
        details.append(
            f"{scope}累计：开发 {rounds['cycle_development_attempts']} 次，"
            f"验收 {rounds.get('cycle_review_attempts', '未知')} 次"
        )
    details.append(
        f"本次授权已用：开发 {rounds.get('development_attempts')} / "
        f"{rounds.get('development_limit')} 次，验收 {rounds.get('reviewer_invocations')} / "
        f"{rounds.get('reviewer_limit')} 次"
    )
    if rounds.get("checkpoint_reason"):
        details.append(f"暂停原因：{human_pause_reason(rounds['checkpoint_reason'])}")
    details.append(
        "额外 CI 修复：" + human_ci_fix_usage(
            rounds.get("final_ci_fix_used"), rounds.get("final_ci_fix_limit")
        )
    )
    return details


def _attempt_count(subject: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = subject.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _attempt_ordinals(
    subject: dict[str, Any], invocation: dict[str, Any] | None
) -> tuple[int | None, int | None]:
    development: int | None = None
    review: int | None = None
    candidates: list[dict[str, Any]] = []
    pending = subject.get("pending_semantic_attempt")
    if isinstance(pending, dict):
        candidates.append(pending)
    semantic = invocation.get("semantic_attempt") if isinstance(invocation, dict) else None
    if isinstance(semantic, dict):
        candidates.insert(0, semantic)
    for attempt in candidates:
        ordinal = attempt.get("ordinal")
        if type(ordinal) is not int:
            continue
        role = attempt.get("role")
        if role == "development":
            development = ordinal
        elif role in {"reviewer", "fresh_acceptance"}:
            review = ordinal
    if development is None:
        development = _attempt_count(subject, "modification_attempts", "code_modification_attempts")
    if review is None:
        review = _attempt_count(subject, "validation_attempts")
    return development, review


def _current_object(
    state: dict[str, Any], current: tuple[str, dict[str, Any]] | None
) -> str:
    if current is not None:
        return delivery_object_label(state, current[0])
    return "Delivery Run"


def _current_findings(state: dict[str, Any]) -> list[str]:
    current = current_work_subject(state)
    if current is None:
        return []
    subject = _acceptance_subject(state, current)
    artifact = _current_artifact(subject)
    if artifact is not None:
        return _artifact_findings(artifact)
    if subject.get("phase") in {
        "developing",
        "repairing",
        "committing_candidate",
        "candidate",
        "reviewing",
    }:
        unresolved = _unresolved_acceptance_artifact(state, current, subject)
        if unresolved is not None:
            return _artifact_findings(unresolved)
    return []


def _unresolved_acceptance_artifact(
    state: dict[str, Any],
    current: tuple[str, dict[str, Any]],
    subject: dict[str, Any],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = [subject]
    if current[0] in {"run_acceptance", "run_repair"}:
        acceptance = state.get("run_acceptance")
        repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if isinstance(repair, dict) and repair is not subject:
            candidates.append(repair)
        if isinstance(acceptance, dict) and acceptance is not subject:
            candidates.append(acceptance)
    for candidate in candidates:
        if candidate.get("repair_source") != "acceptance":
            continue
        unresolved = candidate.get("unresolved_acceptance_artifact")
        if isinstance(unresolved, dict):
            return unresolved
        record = candidate.get("acceptance_record")
        if not isinstance(record, dict):
            continue
        artifact = record.get("artifact")
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        candidate_sha = _subject_candidate_identity(candidate)
        if (
            isinstance(artifact, dict)
            and isinstance(reviewed, str)
            and isinstance(candidate_sha, str)
            and reviewed != candidate_sha
            and _artifact_has_failures(artifact)
        ):
            return artifact
    return None


def _artifact_has_failures(artifact: dict[str, Any]) -> bool:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return False
    return bool(_artifact_findings(artifact)) or any(
        isinstance(check, dict) and check.get("status") == "fail"
        for check in checks.values()
    )


def _current_artifact(subject: dict[str, Any]) -> dict[str, Any] | None:
    """Return only an artifact proven to belong to the current candidate."""

    candidate = _subject_candidate_identity(subject)
    if not isinstance(candidate, str) or not candidate:
        return None
    record = subject.get("acceptance_record")
    if isinstance(record, dict):
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        artifact = record.get("artifact")
        if isinstance(artifact, dict) and reviewed == candidate:
            return artifact
        return None

    budget = subject.get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if isinstance(artifacts, list):
        for review in reversed(artifacts):
            if not isinstance(review, dict):
                continue
            review_candidate = review.get("candidate_sha")
            if review_candidate is None:
                identity = review.get("review_identity")
                if isinstance(identity, dict):
                    review_candidate = identity.get("run_head_sha")
            if review_candidate != candidate:
                continue
            artifact = review.get("artifact")
            if isinstance(artifact, dict):
                return artifact

    return None


def _completed_acceptance_artifact(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return a terminal acceptance artifact only when its candidate matches."""

    for key in ("parent_job", "active_ticket_job", "run_acceptance"):
        subject = state.get(key)
        if not isinstance(subject, dict) or subject.get("phase") not in {
            "accepted",
            "completed",
            "merged",
        }:
            continue
        artifact = _current_artifact(subject)
        if not isinstance(artifact, dict):
            continue
        checks = artifact.get("checks")
        if not isinstance(checks, dict):
            continue
        statuses = {
            check.get("status")
            for check in checks.values()
            if isinstance(check, dict)
        }
        if statuses == {"pass"}:
            return artifact
    return None


def _acceptance_subject(
    state: dict[str, Any], current: tuple[str, dict[str, Any]]
) -> dict[str, Any]:
    """Use Run Acceptance evidence while publication is waiting on its gate."""

    location, subject = current
    if location == "run_publication":
        acceptance = state.get("run_acceptance")
        publication_head = _publication_boundary_head(subject)
        acceptance_head = (
            _acceptance_publication_boundary_head(acceptance)
            if isinstance(acceptance, dict)
            else None
        )
        if (
            isinstance(acceptance, dict)
            and isinstance(publication_head, str)
            and publication_head == acceptance_head
        ):
            return acceptance
        return {}
    return subject


def _acceptance_publication_boundary_head(
    acceptance: dict[str, Any],
) -> object:
    """Return the explicitly promoted Run head, when its proof is intact."""

    record = acceptance.get("acceptance_record")
    if not isinstance(record, dict) or record.get("acceptance_state") != "integrated":
        return _subject_candidate_identity(acceptance)

    candidate = acceptance.get("candidate_sha")
    reviewed_candidate = record.get("reviewed_candidate_sha")
    reviewed_head = record.get("reviewed_head_sha")
    if (
        not isinstance(candidate, str)
        or not candidate
        or not isinstance(reviewed_candidate, str)
        or reviewed_candidate != candidate
        or not isinstance(reviewed_head, str)
        or not reviewed_head
    ):
        return None

    for key in ("reviewed_head_sha", "integrated_sha"):
        value = acceptance.get(key)
        if value is not None and value != reviewed_head:
            return None
    record_run_head = record.get("run_head_sha")
    if record_run_head is not None and record_run_head != reviewed_head:
        return None
    return reviewed_head


def _publication_boundary_head(subject: dict[str, Any]) -> object:
    record = subject.get("record")
    if isinstance(record, dict) and isinstance(record.get("run_head_sha"), str):
        return record["run_head_sha"]
    for key in ("head_sha", "run_head_sha"):
        value = subject.get(key)
        if isinstance(value, str) and value:
            return value
    pending = subject.get("pending_semantic_attempt")
    history = subject.get("semantic_attempt_history")
    attempts: list[object] = []
    if isinstance(pending, dict):
        attempts.append(pending)
    if isinstance(history, list):
        attempts.extend(reversed(history))
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("role") != "publication":
            continue
        boundary = attempt.get("currentness_boundary")
        if not isinstance(boundary, dict):
            continue
        for key in ("reviewed_head_sha", "run_head_sha"):
            value = boundary.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _subject_candidate_identity(subject: dict[str, Any]) -> object:
    """Read the current version key used by each delivery subject shape."""

    for key in ("candidate_sha", "reviewed_head_sha", "head_sha", "run_head_sha"):
        value = subject.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _current_conclusion(
    state: dict[str, Any],
    current: tuple[str, dict[str, Any]] | None,
    findings: list[str],
    *,
    activity: str,
) -> str:
    if current is None:
        if _completed_acceptance_artifact(state) is not None:
            return "当前有效通过"
        return "尚无有效验收结论"
    location, public_subject = current
    subject = _acceptance_subject(state, current)
    phase = subject.get("phase")
    if phase == "reviewing":
        if activity == "interrupted":
            return "验收已中断，等待恢复"
        if activity == "unknown":
            return "验收状态无法确认"
        if activity == "not_running":
            return "验收等待执行"
        review_kind = "复验" if _has_prior_acceptance_failure(subject) else "首次验收"
        if activity == "capacity_wait":
            return f"模型容量不足，等待自动续接{review_kind}"
        if activity == "recovery_wait":
            return f"验收异常，正在自动续接{review_kind}"
        return "正在复验" if review_kind == "复验" else "首次验收中"
    if phase in {"developing", "repairing"} and findings:
        if activity == "interrupted":
            return "上次验收失败，修复已中断，等待恢复"
        if activity == "unknown":
            return "上次验收失败，修复状态无法确认"
        if activity == "capacity_wait":
            return "上次验收失败，等待模型容量后自动续接修复"
        if activity == "recovery_wait":
            return "上次验收失败，修复异常，正在自动续接"
        if activity == "not_running":
            return "上次验收失败，等待修复执行"
        return "上次验收失败，正在修复"
    if (
        phase in {"candidate", "committing_candidate"}
        and subject.get("repair_source") == "acceptance"
    ):
        return "修复完成，待复验"
    artifact = _current_artifact(subject)
    if artifact is not None:
        checks = artifact.get("checks")
        if isinstance(checks, dict):
            statuses = {
                check.get("status")
                for check in checks.values()
                if isinstance(check, dict)
            }
            if "fail" in statuses or findings:
                return "上次验收失败，等待修复"
            if "blocked" in statuses:
                return "验收受阻，等待人工处理"
            if statuses == {"pass"}:
                if location == "run_publication" and public_subject.get("phase") not in {
                    "completed",
                    "merged",
                }:
                    return "验收通过，等待发布"
                return "当前有效通过"
    return "尚无有效验收结论"


def _has_prior_acceptance_failure(subject: dict[str, Any]) -> bool:
    if isinstance(subject.get("unresolved_acceptance_artifact"), dict):
        return True
    record = subject.get("acceptance_record")
    if isinstance(record, dict):
        artifact = record.get("artifact")
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        candidate = _subject_candidate_identity(subject)
        if (
            isinstance(artifact, dict)
            and isinstance(reviewed, str)
            and isinstance(candidate, str)
            and reviewed != candidate
            and _artifact_has_failures(artifact)
        ):
            return True
    budget = subject.get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if not isinstance(artifacts, list):
        return False
    for review in artifacts:
        if not isinstance(review, dict):
            continue
        artifact = review.get("artifact")
        if not isinstance(artifact, dict):
            continue
        checks = artifact.get("checks")
        if not isinstance(checks, dict):
            continue
        if any(
            isinstance(check, dict)
            and (check.get("status") == "fail" or _artifact_findings(artifact))
            for check in checks.values()
        ):
            return True
    return False


def _latest_artifact(subject: dict[str, Any]) -> dict[str, Any] | None:
    record = subject.get("acceptance_record")
    record_artifact = record.get("artifact") if isinstance(record, dict) else None
    if isinstance(record_artifact, dict):
        return record_artifact
    artifact = subject.get("acceptance_artifact")
    if isinstance(artifact, dict):
        return artifact
    budget = subject.get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if isinstance(artifacts, list) and artifacts:
        latest = artifacts[-1]
        latest_artifact = latest.get("artifact") if isinstance(latest, dict) else None
        if isinstance(latest_artifact, dict):
            return latest_artifact
    return None


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


_FINDING_PARTS = re.compile(
    r"^问题：(.*?)；证据：(.*?)；必须修复：(.*?)；复验：(.*)$"
)


def _print_findings(findings: object) -> None:
    values = findings if isinstance(findings, list) else []
    print(f"\n当前问题（{len(values)}）")
    if not values:
        print("  无")
        return
    for index, finding in enumerate(values, start=1):
        text = _terminal_safe(finding)
        match = _FINDING_PARTS.fullmatch(text)
        if match is None:
            print(f"  {index}. {text}")
            continue
        print(f"  {index}.")
        for label, value in zip(
            ("问题", "证据", "必须修复", "复验"), match.groups()
        ):
            print(f"     {_terminal_safe(label)}：{_terminal_safe(value)}")


def _rich_role_label(value: object) -> str:
    return human_agent_role(value)


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


def _role_label(role: str) -> str:
    family = _role_family(role)
    if family is None:
        return role
    return {
        "development": "Development Agent",
        "review": "Review Agent",
        "publication": "Publication Agent",
    }[family]


def _invocation_duration(
    invocation: dict[str, Any], *, allow_open: bool = False
) -> int | None:
    return invocation_execution_seconds(invocation, allow_open=allow_open)


def _remaining_until(value: object) -> int | None:
    deadline = _parse_optional_timestamp(value)
    return max(0, int((deadline - datetime.now(UTC)).total_seconds())) if deadline else None


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


def _print_cleanup(cleanup: object) -> None:
    if not isinstance(cleanup, dict):
        return
    print("\n工作区清理")
    print(f"  状态: {_terminal_safe(human_status_term(cleanup.get('status')))}")
    if cleanup.get("status") == "completed":
        return
    reason = _cleanup_reason(cleanup)
    if reason:
        print(f"  保留原因: {_terminal_safe(reason)}")
    items = cleanup.get("items")
    if isinstance(items, list):
        pending = sum(1 for item in items if isinstance(item, dict))
        if pending:
            print(f"  已保留 {pending} 个开发工作区")
        for path in _cleanup_paths(cleanup):
            print(f"  待处理工作区: {_terminal_safe(path)}")


def _cleanup_paths(cleanup: dict[str, Any]) -> list[str]:
    items = cleanup.get("items")
    if cleanup.get("status") == "completed" or not isinstance(items, list):
        return []
    return [item["checkout"] for item in items
            if isinstance(item, dict) and isinstance(item.get("checkout"), str)]


def _cleanup_reason(cleanup: dict[str, Any]) -> str | None:
    error = cleanup.get("last_error")
    if cleanup.get("status") == "completed" or not isinstance(error, str) or not error:
        return None
    if "source head changed" in error or "foreign head" in error:
        return "源分支或工作区的提交已变化，已保留新增工作。"
    if "branch identity changed" in error or "HEAD is not the managed branch" in error:
        return "工作区已切换分支或处于游离 HEAD，已保留现场。"
    if any(reason in error for reason in ("tracked modifications", "untracked", "dirty checkout")):
        return "工作区存在尚未提交的文件，已保留现场。"
    return error if len(error) <= 240 else error[:232] + "…（已截断）"


def _print_scope_change(scope_change: object) -> None:
    if not isinstance(scope_change, dict):
        return
    summary = scope_change.get("graph_change_summary")
    print("\n任务与依赖变化")
    if isinstance(summary, dict):
        print(f"  {_terminal_safe(summary.get('summary'))}")
