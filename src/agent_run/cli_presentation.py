from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

def _print_precondition_failure(state: dict[str, object]) -> None:
    active = _active_ticket_job(state)
    diagnostics = state.get("diagnostics")
    current_diagnostics = diagnostics if isinstance(diagnostics, list) else []
    print(
        json.dumps(
            {
                "result": "resumed",
                "run_id": state["run_id"],
                "status": state["status"],
                "run_branch": state.get("run_branch", state.get("parent_branch")),
                "active_ticket": active.get("ticket_number") if active else None,
                "diagnostics": [
                    *current_diagnostics,
                    {
                        "code": "command_precondition",
                        "message": "当前交付运行尚未满足此命令的执行条件",
                    },
                ],
                "scope_change": state.get("unsupported_scope_change"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _print_status(state: dict[str, object], *, as_json: bool) -> None:
    active = _active_ticket_job(state)
    active_ticket = active.get("ticket_number") if active else None
    worker = _current_worker(state)
    output = {
        "run_id": state.get("run_id"),
        "repository": state.get("repository"),
        "parent": state.get("parent"),
        "status": state.get("status"),
        "active_ticket": active_ticket,
        "phase": _current_phase(state),
        "worker": worker,
        "elapsed_seconds": _elapsed_seconds(state.get("created_at")),
        "gate": "required_checks" if state.get("status") == "waiting_checks" else None,
        "next_action": _next_action(state),
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
    }
    if as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    print(f"交付运行: {output['run_id']}")
    print(f"运行状态: {_display_term(output['status'])}")
    print(f"当前阶段: {_display_term(output['phase'])}")
    if active_ticket is not None:
        print(f"当前任务: #{active_ticket}")
    if worker is not None:
        print(
            "当前工作代理: "
            f"{worker['role']}（第 {worker['attempt']} 次尝试，{_display_term(worker['phase'])}，"
            f"会话 {worker['thread_id'] or '尚不可用'}）"
        )
    print(f"已运行: {output['elapsed_seconds']} 秒")
    if output["gate"]:
        print("当前门禁: 必需检查")
    scope_change = output["scope_change"]
    if isinstance(scope_change, dict):
        print(
            "Ticket Graph: "
            f"accepted={scope_change.get('accepted_graph_revision')} "
            f"observed={scope_change.get('observed_graph_revision')}"
        )
        summary = scope_change.get("graph_change_summary")
        if isinstance(summary, dict):
            print(f"变化摘要: {summary.get('summary')}")
    print(f"下一步: {output['next_action']}")


def _print_history(state: dict[str, object], *, as_json: bool) -> None:
    timeline = state.get("timeline", [])
    if not isinstance(timeline, list):
        raise ValueError("timeline must be an array")
    output = {
        "run_id": state.get("run_id"),
        "timeline": timeline,
        "next_action": _next_action(state),
    }
    if as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    print(f"交付运行: {output['run_id']}")
    for event in timeline:
        if not isinstance(event, dict):
            continue
        detail = " ".join(
            str(_display_term(event[key]) if key == "phase" else event[key])
            for key in (
                "worker",
                "ticket",
                "attempt",
                "thread_id",
                "phase",
                "pr_number",
                "commit_sha",
                "result",
            )
            if event.get(key) is not None
        )
        print(
            f"{event.get('at')} {_display_term(event.get('kind'))} "
            f"{_display_term(event.get('status'))} {detail}".rstrip()
        )
        if event.get("kind") == "unsupported_scope_change":
            print(
                "  Ticket Graph: "
                f"accepted={event.get('accepted_graph_revision')} "
                f"observed={event.get('observed_graph_revision')}"
            )
            summary = event.get("graph_change_summary")
            if isinstance(summary, dict):
                print(
                    "  变化明细: "
                    f"新增 Ticket {summary.get('added_tickets', [])}；"
                    f"移除 Ticket {summary.get('removed_tickets', [])}；"
                    f"新增依赖 {summary.get('added_dependencies', [])}；"
                    f"移除依赖 {summary.get('removed_dependencies', [])}"
                )
    print(f"下一步: {output['next_action']}")


def _next_action(state: dict[str, Any]) -> str:
    status = str(state.get("status"))
    run_id = state.get("run_id")
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if status in {"run_approval_pending", "parent_approval_pending"} and isinstance(
        run_id, str
    ):
        return f"agent-run approve {run_id}"
    if status == "unsupported_scope_change":
        return "查看变化摘要后执行 agent-run abandon，或在 GitHub 恢复原 Ticket Graph"
    if status in {"ready_for_human", "progress_exhausted", "blocked"}:
        return "处理诊断中的人工事项"
    if status in {
        "active",
        "ticket_completed",
        "parent_delivery_pending",
        "publication_pending",
        "run_acceptance_pending",
        "run_publication_pending",
        "waiting_checks",
        "waiting_merge",
        "parent_closeout_pending",
        "execution_failed",
    }:
        return f"agent-run run {parent_number}"
    return "无"


def _current_worker(state: dict[str, object]) -> dict[str, object] | None:
    publication = state.get("run_publication")
    if isinstance(publication, dict) and publication.get("phase") == "publishing":
        return {
            "role": "运行发布工作代理",
            "attempt": publication.get("publication_attempts"),
            "thread_id": publication.get("thread_id"),
            "phase": publication.get("phase"),
        }
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict) and acceptance.get("phase") == "reviewing":
        reviewer_ids = acceptance.get("reviewer_thread_ids")
        thread_id = (
            reviewer_ids[-1]
            if isinstance(reviewer_ids, list) and reviewer_ids
            else acceptance.get("development_thread_id")
        )
        return {
            "role": "运行验收工作代理",
            "attempt": acceptance.get("validation_attempts"),
            "thread_id": thread_id,
            "phase": acceptance.get("phase"),
        }
    active = _active_ticket_job(state)
    if active:
        return _worker_from_job(active)
    parent_job = state.get("parent_job")
    if isinstance(parent_job, dict):
        return _worker_from_job(parent_job)
    return None


def _current_phase(state: dict[str, object]) -> object:
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        return publication.get("phase")
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        return acceptance.get("phase")
    active = _active_ticket_job(state)
    if active:
        return active.get("phase")
    parent_job = state.get("parent_job")
    if isinstance(parent_job, dict):
        return parent_job.get("phase")
    return state.get("status")


def _active_ticket_job(state: dict[str, object]) -> dict[str, object] | None:
    active = state.get("active_ticket_job")
    if not isinstance(active, dict):
        return None
    if active.get("phase") in {"merged", "completed", "abandoned"}:
        return None
    return active


def _worker_from_job(job: dict[str, object]) -> dict[str, object] | None:
    phase = str(job.get("phase"))
    if phase in {"reviewing", "validating"}:
        reviewer_ids = job.get("reviewer_thread_ids")
        thread_id = reviewer_ids[-1] if isinstance(reviewer_ids, list) and reviewer_ids else None
        return {
            "role": "独立验收工作代理",
            "attempt": job.get("validation_attempts"),
            "thread_id": thread_id,
            "phase": phase,
        }
    if phase in {"publishing", "publication_pending"}:
        return {
            "role": "发布工作代理",
            "attempt": job.get("publication_attempts"),
            "thread_id": job.get("publication_thread_id"),
            "phase": phase,
        }
    if phase in {"developing", "repairing"}:
        return {
            "role": "开发工作代理",
            "attempt": job.get("pending_attempt", job.get("modification_attempts")),
            "thread_id": (
                None
                if isinstance(job.get("pending_attempt"), int)
                else job.get("development_thread_id")
            ),
            "phase": phase,
        }
    return None


def _elapsed_seconds(created_at: object) -> int | None:
    if not isinstance(created_at, str):
        return None
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - started).total_seconds()))


def _display_term(value: object) -> object:
    if not isinstance(value, str):
        return value
    return {
        "active": "进行中",
        "ticket_completed": "任务已完成",
        "waiting_checks": "等待必需检查",
        "waiting_merge": "等待合并确认",
        "publication_pending": "等待发布",
        "parent_delivery_pending": "等待父项交付",
        "parent_approval_pending": "等待父项人工批准",
        "parent_closeout_pending": "等待父项收口",
        "run_acceptance_pending": "等待运行整体验收",
        "run_publication_pending": "等待运行发布",
        "run_approval_pending": "等待人工批准",
        "ready_for_human": "等待人工处理",
        "unsupported_scope_change": "不支持的范围变化",
        "progress_exhausted": "无可推进任务",
        "execution_failed": "执行失败，可恢复",
        "blocked": "已阻塞",
        "completed": "已完成",
        "abandoned": "已放弃",
        "developing": "开发中",
        "repairing": "修复中",
        "reviewing": "验收中",
        "validating": "验证中",
        "publishing": "发布中",
        "publication_pending": "等待发布",
        "ready_for_approval": "等待人工批准",
        "merged": "已合并",
        "ticket_phase": "任务阶段",
        "parent_phase": "父项阶段",
        "run_acceptance": "运行验收",
        "run_publication": "运行发布",
        "run_status": "运行状态",
        "timeline_capacity": "时间线容量已达上限",
    }.get(value, "未知内部状态")
