"""Shared facts and role routing for model-visible worker instructions."""

from __future__ import annotations

import json
from typing import Any


def pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def prompt_context(request: dict[str, Any], *fields: str) -> dict[str, Any]:
    context = {
        field: request[field]
        for field in fields
        if field != "human_response_history"
        and field in request
        and request[field] is not None
    }
    if "human_response_history" in fields:
        response = _latest_maintainer_response(request)
        if response is not None:
            context["latest_maintainer_response"] = response
    return context


def _latest_maintainer_response(request: dict[str, Any]) -> str | None:
    history = request.get("human_response_history")
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if isinstance(item, dict):
            response = item.get("response")
            if isinstance(response, str) and response.strip():
                return response
    return None


def uses_short_role_prompt(
    request: dict[str, Any], *, reviewer: bool = False
) -> bool:
    if request.get("_invocation_mode") == "new-thread":
        return False
    thread_id = request.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return False
    if reviewer and not isinstance(request.get("current_review_identity"), dict):
        return False
    if request.get("_invocation_mode") == "resume" or reviewer:
        return True
    blockers = request.get("prior_human_blockers")
    return isinstance(blockers, list) and bool(blockers)


def task_brief(
    request: dict[str, Any], *, read_issues: bool, development: bool = False
) -> str:
    """Label requirement URLs without copying request bodies or controller state."""
    scope = request.get("acceptance_scope")
    parent = request.get("parent_issue_url")
    task = request.get("task_issue_url")
    lines: list[str] = []
    if scope in ("parent_only", "run"):
        if isinstance(parent, str) and parent.strip():
            lines.append(f"本次负责的完整需求：{parent}")
        lines.append("该需求的标题、正文和全部验收条件确定本次范围。")
        if scope == "run":
            lines.append(
                "本次责任覆盖多个子任务的整体结果。核对累计改动、"
                "任务之间的配合和最终用户路径；不能用单个子任务或局部修复的通过代替整体完成。"
            )
            if read_issues:
                lines.append("读取最终子任务及依赖，取得本次整体需求。")
    else:
        if isinstance(task, str) and task.strip():
            lines.append(f"本次负责的具体任务：{task}")
        if isinstance(parent, str) and parent.strip():
            lines.append(f"用于理解整体需求的背景：{parent}")
        lines.append(
            "具体任务的标题、正文和验收条件确定本次范围。背景用于理解整体目标和任务明确引用的"
            "必要约束，不自动增加其他子任务的工作。"
        )
        if development:
            lines.append(
                "工作区可能已有前序任务成果；只有它们直接阻碍当前任务、破坏当前累计集成结果，"
                "或修复它们是满足当前验收条件所必需时，才进行最小必要修复。"
            )
    lines.append("当前修改直接造成的问题也属于本次责任。")
    if read_issues:
        lines.extend(
            [
                "",
                "开始前先读取具体任务，再读取背景；只有完整需求链接时读取该需求。",
                "使用继承环境中的只读 `gh issue view`，按链接中的 Issue 编号读取标题、正文和验收条件；"
                "保留继承的 PATH 与认证环境，不另找客户端、重新认证或改动认证配置。",
                "Issue 评论、历史 PR 和其他人的总结只提供调查线索，不能覆盖当前需求或代替当前代码的验证。",
            ]
        )
    return "\n".join(lines)


def human_continuation(request: dict[str, Any]) -> str:
    facts = prompt_context(request, "prior_human_blockers", "human_response_history")
    if not facts:
        return ""
    if "prior_human_blockers" in facts:
        facts["current_human_blockers"] = facts.pop("prior_human_blockers")
    return (
        "当前求助与维护者最新回复（原文）：\n"
        + pretty(facts)
        + "\n回复不等于问题已经解决。重新核对相关权威来源和受影响工作，解决后继续当前任务；"
        "仍需人处理时，按本角色输出格式说明最新情况。"
    )


def review_budget(request: dict[str, Any], *, reviewer: bool) -> str:
    context = request.get("review_budget_context")
    if context is None:
        return ""
    if not isinstance(context, dict):
        raise ValueError("review_budget_context must be an object")
    remaining = context.get("remaining_review_attempts")
    if type(remaining) is not int or remaining < 0:
        raise ValueError("remaining_review_attempts must be a non-negative integer")
    if reviewer:
        current = context.get("current_review_attempt")
        if type(current) is not int or current < 1:
            raise ValueError("current_review_attempt must be a positive integer")
        text = (
            f"这是本任务的第 {current} 次独立验收，本轮结束后最多还可启动 {remaining} 次独立验收。"
        )
    else:
        completed = context.get("completed_review_attempts")
        if type(completed) is not int or completed < 0:
            raise ValueError("completed_review_attempts must be a non-negative integer")
        text = f"本任务已完成 {completed} 次独立验收，最多还可启动 {remaining} 次独立验收。"
    return text + "次数只用于合理安排本轮工作，不改变验收标准；不要隐瞒、降级或放行必须修复的问题。"


def structured_output_repair_prompt(output_name: str, contract_error: str) -> str:
    roles = {
        "Development result": ("开发或修复", "开发结果 JSON"),
        "Acceptance Artifact": ("独立验收", "审查结果 JSON"),
        "Publication Artifact": ("交付说明编写", "文案结果 JSON"),
    }
    if output_name not in roles:
        raise ValueError(f"unknown structured output role: {output_name}")
    work, result = roles[output_name]
    return (
        f"你已完成本次{work}。现在只负责根据已经完成的真实工作，重新输出符合要求的{result}。\n"
        f"格式错误：{contract_error}\n\n"
        "只修正结果格式，不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实。"
    )
