"""Human status actions derived from existing controller and delivery facts."""
from __future__ import annotations

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
        "committing_candidate": "程序正在自动保存代码；无需操作。",
        "candidate": "程序正在准备验收；无需操作。",
        "publishing": "程序正在准备创建 PR；无需操作。",
        "publication_pending": "程序正在创建 PR；无需操作。",
        "creating_pr": "程序正在创建 PR；无需操作。",
    }.get(str(phase))


def status_heading(state: dict[str, Any], view: dict[str, Any], fallback: object) -> object:
    if state.get("status") == "completed":
        return "已合并，待清理" if final_approval_cleanup_pending(state) else "任务已完成"
    current = current_work_subject(state)
    if current and current[0] == "run_acceptance" and view.get("execution_activity") == "running":
        return "正在验收整体需求"
    if state.get("status") in {"run_approval_pending", "parent_approval_pending"}:
        subject = current[1] if current else {}
        number = subject.get("pr_number")
        return f"请批准合并 PR #{number}" if type(number) is int else "等待批准合并"
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
            lines.append(f"PR #{number} 已合并")
    if subject.get("parent_closed") is True or (state.get("delivery_type") == "parent_only" and subject.get("phase") == "completed"):
        lines.append("整体需求已关闭")
    elif state.get("delivery_type") != "parent_only":
        lines.append("整体需求关闭尚未确认")
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
