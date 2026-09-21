"""修复触发已确定、Candidate 尚未创建的有限 Run 前置。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.state import StateStore
from support.run_artifacts import _canonical_run_budget, _repair_artifact


def prepare_run_repair(
    state: dict[str, Any], states: StateStore, *, repair_request: dict[str, Any],
) -> None:
    """持久化合法 repair 起点；调用方仍显式提供真实触发与 Git 身份。

    不接受任意字段 overrides，也不修复缺损状态。非法输入测试直接写原始状态。
    Candidate 创建、refs、触发 currentness 检查由被测 Engine 正常执行。
    """
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": deepcopy(repair_request),
    }
    state.update({
        "status": "run_acceptance_pending",
        "terminal_kind": "run_repair_pending",
        "diagnostics": [],
    })
    states.save_run(str(state["run_id"]), state)
    states.load_current_run(str(state["run_id"]))
