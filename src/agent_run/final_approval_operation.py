"""Recognize final-approval work without granting new merge authority."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.task_control import TaskControlStore, TaskKey, action_receipt_matches
from agent_run.managed_workspace import workspace_state_root


def has_final_approval(state: Mapping[str, Any]) -> bool:
    """An existing grant identifies recovery work; engines still check currentness."""
    subject = state.get(
        "parent_job" if state.get("delivery_type") == "parent_only" else "run_publication"
    )
    return isinstance(subject, Mapping) and isinstance(
        subject.get("approval_grant"), Mapping
    )


def is_final_approval_action(
    action: Mapping[str, Any], state: Mapping[str, Any]
) -> bool:
    return action.get("kind") == "approve" or (
        action.get("kind") == "resume" and has_final_approval(state)
    )


def final_approval_cleanup_pending(state: Mapping[str, Any]) -> bool:
    """Include final cleanup not yet scheduled when closeout committed first."""
    cleanup = state.get("delivery_cleanup")
    if isinstance(cleanup, Mapping) and cleanup.get("status") != "completed":
        return True
    if state.get("status") != "completed" or not has_final_approval(state):
        return False
    parent_only = state.get("delivery_type") == "parent_only"
    subject = state.get("parent_job" if parent_only else "run_publication")
    if not isinstance(subject, Mapping) or subject.get("phase") != (
        "completed" if parent_only else "merged"
    ):
        return False
    if not parent_only and subject.get("parent_closed") is not True:
        return False
    branch = subject.get("parent_branch") if parent_only else state.get("run_branch")
    if not isinstance(branch, str):
        return False
    items = cleanup.get("items") if isinstance(cleanup, Mapping) else None
    item = items.get(branch) if isinstance(items, Mapping) else None
    return not isinstance(item, Mapping) or item.get("status") != "completed"


def final_approval_failure(state: Mapping[str, Any]) -> str | None:
    """Only completed delivery with finished cleanup is a successful approval."""
    if state.get("status") == "completed" and not final_approval_cleanup_pending(state):
        return None
    if not has_final_approval(state):
        return "原批准已无法继续，请按当前状态提示重新验收和批准"
    if state.get("status") in {
        "unsupported_scope_change", "deterministic_contradiction", "requeue_required", "blocked"
    }:
        return "最终批准无法继续，请先处理当前状态中的问题"
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, Mapping) else None
    selector = str(number) if type(number) is int else "<parent-issue>"
    return f"最终批准未完成，请使用 agent-run resume {selector} 继续未完成部分"


def final_approval_busy_message(
    action: Mapping[str, Any], state: Mapping[str, Any]
) -> str:
    if is_final_approval_action(action, state):
        return "正在执行最终批准，暂不支持停止、放弃或提交其他操作；可用 status 查看进度"
    return "当前 Delivery Task 已有未完成 Lifecycle Action；不会等待或排队"


def has_unfinished_final_receipt(state: Mapping[str, Any], workspace: Path) -> bool:
    """Select a completed Run only if its exact final Action still needs reconciliation."""
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, Mapping) else None
    repository = state.get("repository")
    if type(number) is not int or not isinstance(repository, str):
        return False
    control = TaskControlStore(workspace_state_root(workspace))
    task = TaskKey(workspace, repository, number)
    record = control.load(task)
    action = record.get("action") if isinstance(record, Mapping) else None
    payload = action.get("payload") if isinstance(action, Mapping) else None
    target_run = action.get("run_id") if isinstance(action, Mapping) else None
    if target_run is None and isinstance(payload, Mapping):
        target_run = payload.get("run_id")
    if target_run != state.get("run_id"):
        return False
    control.inspect_run(task, state)
    receipt = state.get("action_application_receipt")
    if not isinstance(action, Mapping):
        return False
    executor = record.get("executor") if isinstance(record, Mapping) else None
    unfinished_action = action.get("status") in {"accepted", "applying"}
    unfinished_exit = (
        action.get("status") == "completed"
        and isinstance(executor, Mapping)
        and executor.get("status") in {"starting", "running"}
        and executor.get("action_id") == action.get("action_id")
        and executor.get("generation") == action.get("executor_generation")
        and executor.get("run_id") == action.get("run_id") == state.get("run_id")
    )
    if not (unfinished_action or unfinished_exit):
        return False
    if not is_final_approval_action(action, state) or not isinstance(receipt, Mapping):
        return False
    payload = action.get("payload")
    unbound_matches = (
        action.get("run_id") is None and isinstance(payload, Mapping)
        and payload.get("run_id") == state.get("run_id") == receipt.get("run_id")
        and all(receipt.get(key) == action.get(key) for key in (
            "action_id", "kind", "payload_digest", "executor_generation"
        ))
    )
    return action_receipt_matches(state, action) or unbound_matches
