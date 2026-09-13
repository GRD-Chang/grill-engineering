from __future__ import annotations

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
        return "已合并，待清理；按恢复命令完成工作区清理。"
    cleanup = state.get("delivery_cleanup")
    if isinstance(cleanup, dict) and cleanup.get("status") == "cleanup_pending":
        return "交付清理尚未完成；受管工作区已保留，请查看清理诊断。"
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
        f"等待 {target} 的合并前检查" if checks
        else f"等待 {target} 的合并确认" if status == "waiting_merge"
        else f"等待 {target} 的操作结果"
    )
    credential_failure = wait.get("credential_failure_class")
    if isinstance(credential_failure, str):
        work = "等待工作凭据恢复可用"
    control = audit.get("executor_control")
    activity = control.get("activity") if isinstance(control, dict) else "unknown"
    timed_out = status == "supervision_timeout"
    if timed_out:
        description = "本轮自动等待已超时"
        guidance = "需要恢复本轮等待，执行以下命令。"
    elif activity == "running":
        description = "正在后台检查合并前检查结果" if checks else "正在后台等待操作结果"
        cleanup = cleanup_instruction(state)
        guidance = f"后台等待会继续。{cleanup}" if cleanup else "无需操作。"
    elif activity == "not_running":
        description = "自动等待已停止"
        guidance = "需要继续时，执行以下命令。"
    else:
        description = "无法确认后台等待是否仍在继续"
        guidance = unknown_execution_guidance(state)
    details = _wait_times(window, wait, timed_out=timed_out)
    if isinstance(credential_failure, str):
        reason = (
            "暂时无法取得 GitHub 工作凭据" if credential_failure == "credential_unavailable"
            else "GitHub 工作凭据不可用，详细原因见 --json 诊断"
        )
        details.append(("原因", reason))
        if type(wait.get("retry_count")) is int:
            details.append(("重试次数", str(wait["retry_count"])))
    details.extend(_check_results(subject, checks=checks))
    observation = wait.get("latest_observation")
    if isinstance(observation, dict) and isinstance(observation.get("message"), str):
        details.append(("外部情况", human_pause_reason(observation["message"])))
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
        ("已等待", _duration(elapsed)),
        ("本轮等待" if timed_out else "本轮最多还可等待",
         "已超时" if timed_out else _duration(remaining)),
    ]


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _duration(value: object) -> str:
    if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return "未知"
    minutes = int(value) // 60
    if minutes < 1:
        return "不足 1 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"


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
        return [("合并前检查结果", "尚未取得当前版本的检查结果")]
    unavailable = subject.get("required_checks_observation_status") in {"unavailable", "unknown"}
    result = _check_result(evidence.get("result"))
    details = [("上次合并前检查结果" if unavailable else "合并前检查结果", result)]
    if unavailable:
        details.append(("当前观测", "暂时无法确认最新检查结果"))
    values = evidence.get("checks")
    if isinstance(values, list):
        for check in values:
            if isinstance(check, dict) and isinstance(check.get("name"), str):
                details.append((check["name"], _check_result(check.get("bucket"))))
    return details


def _check_result(value: object) -> str:
    return {
        "pending": "等待完成", "pass": "已通过", "fail": "未通过",
        "cancel": "已取消", "none": "未配置合并前检查", "skipping": "已跳过",
    }.get(str(value), "暂时无法确认")
