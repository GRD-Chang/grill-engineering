from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


_TERMINAL_CHANGE_JOB_PHASES = {"completed", "merged", "abandoned"}
_TERMINAL_RUN_ACCEPTANCE_PHASES = {"accepted", "completed"}
_CURRENT_PUBLICATION_GATE_PHASES = {
    "blocked",
    "ready_for_human",
    "publication_pending",
}
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def terminal_safe(value: object) -> str:
    """Render persisted or user-provided text without terminal controls."""

    text = str(value)
    # Remove control bytes, but keep their printable payload.  For example,
    # ESC[2J becomes the harmless, copyable text [2J instead of silently
    # erasing the user's finding content.
    return _UNSAFE_CONTROL.sub("", text)


def human_ci_fix_usage(used: object, limit: object) -> str:
    """Describe the extra CI allowance without exposing its boolean encoding."""
    if limit == 0:
        return "不适用"
    count = int(used) if isinstance(used, bool) else used
    return f"已用 {count if count is not None else '未知'} / {limit if limit is not None else '未知'} 次"


def human_delivery_object(value: object) -> str:
    """Translate a display label, leaving machine work-subject identities intact."""
    text = str(value or "Delivery Run")
    labels = {
        "Delivery Run": "整体交付", "Run Acceptance": "整体验收",
        "Run Publication": "整体交付", "Run Repair": "整体修复",
    }
    if text in labels:
        return labels[text]
    match = re.fullmatch(r"Ticket (#\S+)", text)
    if match:
        return f"子任务 {match[1]}"
    match = re.fullmatch(r"Parent Issue (#\S+)", text)
    if match:
        return f"整体需求 {match[1]}"
    match = re.fullmatch(r"Delivery Run（Parent Issue (#\S+)）", text)
    if match:
        return f"整体交付（需求 {match[1]}）"
    return text


def human_agent_role(value: object) -> str:
    raw = str(value or "Agent")
    if "开发" in raw or raw in {"development", "Development Agent"}:
        return "开发 Agent"
    if "验收" in raw or raw in {"review", "reviewer", "fresh_acceptance", "Review Agent"}:
        return "验收 Agent"
    if "发布" in raw or raw in {"publication", "final_publication", "Publication Agent"}:
        return "发布 Agent"
    return "Agent" if raw != "Agent" else raw


def human_status_term(value: object) -> object:
    """One human vocabulary shared by plain/Rich status and history rendering."""
    if not isinstance(value, str):
        return value
    return {
        "active": "进行中", "starting": "正在初始化", "ticket_completed": "子任务已完成",
        "waiting_checks": "等待合并前检查", "required_checks": "合并前检查",
        "waiting_merge": "等待合并确认", "waiting_external": "等待 GitHub 操作结果",
        "github_convergence": "等待 GitHub 操作结果", "publication_pending": "发布待继续",
        "parent_delivery_pending": "准备交付整体需求", "parent_approval_pending": "等待人工批准",
        "parent_closeout_pending": "代码已合并，正在关闭整体需求 Issue",
        "run_acceptance_pending": "等待整体验收", "run_publication_pending": "等待整体发布",
        "run_approval_pending": "等待人工批准", "requeue_required": "需要按更新后的需求重新开始",
        "ready_for_human": "等待人工处理", "unsupported_scope_change": "需求或依赖发生了无法自动处理的变化",
        "deterministic_contradiction": "交付记录与实际结果不一致，需人工处理",
        "abandonment_pending": "正在完成放弃操作", "progress_exhausted": "暂无可继续的子任务",
        "execution_failed": "执行失败，可恢复", "operator_stopped": "已手动停止，可恢复",
        "supervision_timeout": "自动等待已超时，可恢复", "pending": "待处理", "blocked": "已受阻",
        "incompatible_run_state": "保存的任务记录不兼容", "unreviewed": "未验收",
        "pass": "已通过", "fail": "未通过", "stale": "已失效", "completed": "已完成",
        "abandoned": "已放弃", "developing": "开发中", "repairing": "修复中",
        "reviewing": "验收中", "validating": "验证中", "publishing": "发布中",
        "ready_for_approval": "等待人工批准", "merged": "已合并", "accepted": "验收通过",
        "candidate": "代码已准备，等待验收", "committing_candidate": "正在保存代码修改",
        "ticket_phase": "子任务阶段", "parent_phase": "整体需求阶段", "run_acceptance": "整体验收",
        "run_publication": "整体发布", "run_status": "任务状态", "timeline_capacity": "历史记录已达上限",
        "cleanup_pending": "等待清理", "当前有效通过": "当前版本已通过验收",
        "success": "已通过", "failure": "未通过", "failed": "执行失败",
        "open": "未关闭", "closed": "已关闭", "none": "未配置合并前检查",
        "unknown": "暂时无法确认", "unavailable": "暂时无法获取",
        "cancel": "已取消", "cancelled": "已取消", "canceled": "已取消",
        "skipping": "已跳过", "skipped": "已跳过", "in_progress": "进行中",
        "queued": "等待执行", "running": "执行中", "not_running": "未运行",
        "neutral": "无通过或失败结论", "timed_out": "已超时",
        "action_required": "需要人工处理", "startup_failure": "启动失败",
        "stale_result": "结果已过期",
    }.get(value.lower(), "状态未知，详见 --json 诊断")


def status_diagnostic_command(state: dict[str, Any]) -> str:
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, dict) else None
    repository = state.get("repository")
    if isinstance(repository, str) and type(number) is int:
        return f"agent-run status --repo {repository} --parent {number} --json"
    return "agent-run status --json"


def unknown_execution_guidance(state: dict[str, Any], *, include_command: bool = True) -> str:
    diagnostic = (
        f"\n诊断命令: {status_diagnostic_command(state)}" if include_command else ""
    )
    return (
        "运行状态无法确认；请先查看后台诊断。"
        "继续操作时程序会先核验原执行的归属和退出状态，确认安全后才恢复。"
        f"{diagnostic}"
    )


def human_pause_reason(value: object) -> str:
    """Translate exact controller reasons without rewriting user evidence."""
    raw = str(value)
    if raw in {
        "execution_failed", "ready_for_human", "run_approval_pending", "parent_approval_pending",
        "abandonment_pending", "supervision_timeout", "operator_stopped", "unsupported_scope_change",
        "publication_pending", "progress_exhausted", "requeue_required", "deterministic_contradiction",
    }:
        return str(human_status_term(raw))
    return {
        "modification_budget_exhausted": "本次授权的开发次数已用尽",
        "review_budget_exhausted": "本次授权的验收次数已用尽",
        "agent_requires_human": "工作需要人工处理",
        "reviewer_requires_human": "验收需要人工处理",
        "acceptance_record_mismatch": "验收记录与当前代码版本不一致",
        "candidate_or_acceptance_inconsistent": "代码版本与验收结果不一致",
        "merged_result_mismatch": "实际合并结果与交付记录不一致",
        "published_head_mismatch": "远端代码版本与发布记录不一致",
        "unexpected_external_merge": "发现了未记录的外部合并",
        "Run abandonment recovery is incomplete.": "放弃操作尚未完成",
        "Parent Issue requires explicit human intervention": "整体需求需要你处理后才能继续",
        "外部状态在本次监督窗口内未收敛": "在本次等待时限内仍未确认 GitHub 操作结果",
        "initial Worker read credential is temporarily unavailable": "暂时无法取得 GitHub 工作凭据",
    }.get(raw, raw)


def local_timestamp(value: object) -> str:
    """Render persisted UTC timestamps in the querying device's local timezone."""
    if not isinstance(value, str):
        return "未知"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
    except ValueError:
        return "未知"
    fallback = False
    try:
        local = instant.astimezone()
    except (OSError, OverflowError, ValueError):
        local, fallback = instant.astimezone(UTC), True
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    suffix = "（本地时区不可用，已回退 UTC）" if fallback else ""
    return f"{local:%Y-%m-%d %H:%M:%S} {zone}{suffix}"


def current_work_subject(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Select the one Work Subject represented by the public status view."""

    active = state.get("active_ticket_job")
    if (
        isinstance(active, dict)
        and active.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        ticket_number = active.get("ticket_number")
        locator = (
            f"ticket:{ticket_number}"
            if isinstance(ticket_number, int)
            else "ticket:unknown"
        )
        return locator, active

    parent = state.get("parent_job")
    if (
        isinstance(parent, dict)
        and parent.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        return "parent", parent

    acceptance = state.get("run_acceptance")
    publication = state.get("run_publication")
    if (
        isinstance(publication, dict)
        and publication.get("phase") in _CURRENT_PUBLICATION_GATE_PHASES
    ):
        return "run_publication", publication

    if (
        isinstance(acceptance, dict)
        and acceptance.get("phase") not in _TERMINAL_RUN_ACCEPTANCE_PHASES
    ):
        repair = acceptance.get("repair_job")
        if acceptance.get("phase") == "repairing" and isinstance(repair, dict):
            return "run_repair", repair
        return "run_acceptance", acceptance

    if isinstance(publication, dict):
        return "run_publication", publication
    if isinstance(acceptance, dict):
        return "run_acceptance", acceptance
    return None


def human_next_action(value: object, *, run_id: object = None) -> object:
    """Hide managed Run identifiers from ordinary operator instructions."""

    if not isinstance(value, str):
        return value
    if isinstance(run_id, str) and run_id:
        return re.sub(
            rf"(?<!\S){re.escape(run_id)}(?!\S)",
            "<run-id>",
            value,
        )
    return value


def elapsed_seconds_since(value: object, *, end: datetime | None = None) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    finished = end or datetime.now(UTC)
    return max(0, int((finished - started).total_seconds()))


def delivery_object_label(
    state: dict[str, Any],
    locator: str,
    *,
    ticket_number: object = None,
    include_parent_for_run: bool = False,
) -> str:
    """Render one stable label for a delivery work subject or state location."""

    if locator.startswith("ticket:"):
        number = ticket_number or locator.split(":", 1)[1]
        return f"Ticket #{number}"
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if locator == "parent" or locator.startswith("parent-only:"):
        return f"Parent Issue #{parent_number}"
    if locator in {"run_acceptance", "run_repair"} or locator.startswith(
        ("run-acceptance:", "run-repair:")
    ):
        return "Run Acceptance"
    if locator == "run_publication" or locator.startswith("run-publication:"):
        return "Run Publication"
    if include_parent_for_run:
        return f"Delivery Run（Parent Issue #{parent_number}）"
    return "Delivery Run"
