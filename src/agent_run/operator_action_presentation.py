from __future__ import annotations

from datetime import datetime
from itertools import chain
from typing import Any

from agent_run.guidance_actions import action_command
from agent_run.messages import text
from agent_run.operator_gate import (
    operator_gate_evidence,
    operator_gate_subjects,
)
from agent_run.presentation_helpers import (
    delivery_object_label,
    human_agent_role,
    human_delivery_object,
    human_next_action,
    human_pause_reason,
    human_status_term,
    terminal_safe,
)


def operator_action_view(
    state: dict[str, Any],
    *,
    current_identity: dict[str, object],
    fallback_next_action: str,
    language: str = "zh",
    human: bool = False,
) -> dict[str, Any] | None:
    """Project one canonical Run-wide gate for public CLI consumers."""

    subjects = operator_gate_subjects(state)
    if not subjects:
        return None
    location, subject = subjects[0] if len(subjects) == 1 else ("run", state)
    evidence = operator_gate_evidence(state)
    reason = (
        evidence["reason"]
        if evidence is not None and len(subjects) == 1
        else subject.get("blocked_reason") or _diagnostic_code(state)
    )
    blockers = subject.get("human_blockers")
    is_human_blocker = (
        state.get("status") != "abandonment_pending"
        and reason in {"agent_requires_human", "reviewer_requires_human"}
        and isinstance(blockers, list)
        and all(isinstance(item, str) for item in blockers)
    )
    action_kind = (
        evidence["action_kind"]
        if evidence is not None and len(subjects) == 1 and not is_human_blocker
        else _operator_action_kind(state, reason, is_human_blocker)
    )
    phase = (
        subject.get("human_blocker_phase")
        if is_human_blocker
        else (
            evidence["phase"]
            if evidence is not None and len(subjects) == 1
            else subject.get("phase", state.get("status"))
        )
    )
    reasons = (
        [str(blocker) for blocker in blockers]
        if is_human_blocker and isinstance(blockers, list)
        else _operator_reasons(state, reason)
    )
    return {
        "type": _operator_action_label(action_kind),
        "object": delivery_object_label(
            state,
            location,
            ticket_number=subject.get("ticket_number"),
            include_parent_for_run=True,
        ),
        "phase": phase,
        "reasons": reasons,
        "trigger_invocation": (
            _triggering_invocation(state, location) if is_human_blocker else None
        ),
        "preserved": _preserved_results(subject, current_identity),
        "next_action": _operator_next_action(
            state,
            action_kind,
            reason,
            fallback_next_action=fallback_next_action,
            language=language, human=human,
        ),
    }


def print_operator_action(
    action: dict[str, Any], *, run_id: object = None
) -> None:
    print("需要你处理:")
    print(f"类型: {terminal_safe(human_action_type(action['type']))}")
    print(f"对象: {terminal_safe(human_delivery_object(action['object']))}")
    print(f"阶段: {terminal_safe(human_status_term(action['phase']))}")
    for reason in action["reasons"]:
        message = reason if action["type"] == "Human Blocker" else human_pause_reason(reason)
        print(f"原因: {terminal_safe(message)}")
    invocation = action.get("trigger_invocation")
    if isinstance(invocation, dict):
        print(
            "触发阻塞的 Agent: "
            f"{terminal_safe(_human_agent_role(invocation['role']))}；"
            f"模型 {terminal_safe(invocation['model'])}；"
            f"推理强度 {terminal_safe(invocation['reasoning_effort'])}；"
            f"本轮时长: {terminal_safe(invocation['duration_seconds'])} 秒"
        )
    print(
        f"已保留成果: {terminal_safe(human_preserved_results(action['preserved']))}"
    )
    print("整项任务已暂停，其他子任务也不会继续。")
    if action["type"] == "Review Budget Checkpoint":
        print("继续执行后，将按配置补充本次开发与验收额度，继续已有工作。")
    print(
        "下一步: "
        f"{terminal_safe(human_next_action(action['next_action'], run_id=run_id))}"
    )


def human_action_type(value: object) -> str:
    raw = str(value)
    localized = {
        "Human Blocker": "需要人工处理",
        "Review Budget Checkpoint": "本次开发或验收额度已用尽",
        "Execution Failure": "执行失败",
        "Deterministic Contradiction": "交付记录与实际结果不一致",
        "Supervision Timeout Pause": "自动等待已超时",
        "Operator Stopped": "已手动停止",
        "Requeue Required": "需要按更新后的需求重新开始",
        "Final Approval": "等待最终批准",
        "Publication Retry Exhausted": "发布重试已耗尽",
        "Abandonment Recovery": "正在完成放弃操作",
    }.get(raw)
    return localized or "需要人工处理"


def _human_agent_role(value: object) -> str:
    return human_agent_role(value)


def human_preserved_results(value: object) -> str:
    if not isinstance(value, str):
        return "当前状态与已有审计证据"
    parts: list[str] = []
    for item in value.split("；"):
        if item.startswith("Candidate "):
            parts.append("当前代码版本已保存")
        elif item.startswith("Managed Checkout "):
            parts.append("开发工作区已保留")
        else:
            parts.append(item)
    return "；".join(parts)


def _operator_action_kind(
    state: dict[str, Any], reason: object, is_human_blocker: bool
) -> str:
    if is_human_blocker:
        return "human_blocker"
    if reason in {"modification_budget_exhausted", "review_budget_exhausted"}:
        return "review_budget_checkpoint"
    if reason in {
        "acceptance_record_mismatch",
        "candidate_or_acceptance_inconsistent",
        "merged_result_mismatch",
        "published_head_mismatch",
        "unexpected_external_merge",
    }:
        return "deterministic_contradiction"
    return {
        "execution_failed": "execution_failure",
        "deterministic_contradiction": "deterministic_contradiction",
        "unsupported_scope_change": "deterministic_contradiction",
        "supervision_timeout": "supervision_timeout",
        "operator_stopped": "operator_stopped",
        "requeue_required": "requeue_required",
        "run_approval_pending": "final_approval",
        "parent_approval_pending": "final_approval",
        "publication_pending": "publication_retry_exhausted",
        "abandonment_pending": "abandonment_recovery",
    }.get(str(state.get("status")), "operator_action")


def _operator_action_label(kind: str) -> str:
    return {
        "human_blocker": "Human Blocker",
        "review_budget_checkpoint": "Review Budget Checkpoint",
        "execution_failure": "Execution Failure",
        "deterministic_contradiction": "Deterministic Contradiction",
        "supervision_timeout": "Supervision Timeout Pause",
        "operator_stopped": "Operator Stopped",
        "requeue_required": "Requeue Required",
        "final_approval": "Final Approval",
        "publication_retry_exhausted": "Publication Retry Exhausted",
        "abandonment_recovery": "Abandonment Recovery",
    }.get(kind, "Operator Action")


def _operator_reasons(state: dict[str, Any], reason: object) -> list[str]:
    diagnostics = state.get("diagnostics")
    messages = (
        [
            str(item["message"])
            for item in diagnostics
            if isinstance(item, dict) and item.get("message")
        ]
        if isinstance(diagnostics, list)
        else []
    )
    if messages:
        return messages
    if state.get("status") == "abandonment_pending":
        return ["Run abandonment recovery is incomplete."]
    return [str(reason or state.get("status"))]


def _diagnostic_code(state: dict[str, Any]) -> str | None:
    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, list) or len(diagnostics) != 1:
        return None
    diagnostic = diagnostics[0]
    code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
    return code if isinstance(code, str) and code else None


def _triggering_invocation(
    state: dict[str, Any], location: str
) -> dict[str, Any] | None:
    run_id = state.get("run_id")
    expected_subject = {
        "parent": f"parent-only:{run_id}",
        "run_acceptance": f"run-acceptance:{run_id}",
        "run_repair": f"run-repair:{run_id}",
        "run_publication": f"run-publication:{run_id}",
    }.get(location, location if location.startswith("ticket:") else None)
    active = state.get("active_agent_invocation")
    history = state.get("agent_invocation_history")
    history_candidates = (
        (item for item in reversed(history) if isinstance(item, dict))
        if isinstance(history, list)
        else iter(())
    )
    candidates = (
        chain((active,), history_candidates)
        if isinstance(active, dict)
        else history_candidates
    )
    for invocation in candidates:
        if (
            expected_subject is not None
            and invocation.get("work_subject") != expected_subject
        ):
            continue
        semantic_attempt = invocation.get("semantic_attempt")
        role = (
            semantic_attempt.get("role")
            if isinstance(semantic_attempt, dict)
            else invocation.get("role")
        )
        return {
            "role": role or "unknown",
            "model": invocation.get("model") or "未绑定",
            "reasoning_effort": invocation.get("reasoning_effort") or "未绑定",
            "duration_seconds": _invocation_duration_seconds(invocation),
        }
    return None


def _invocation_duration_seconds(invocation: dict[str, Any]) -> int | None:
    started_at = invocation.get("started_at")
    ended_at = invocation.get("ended_at")
    if not isinstance(started_at, str) or not isinstance(ended_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return None
    return max(0, int((ended - started).total_seconds()))


def _preserved_results(
    subject: dict[str, Any], current_identity: dict[str, object]
) -> str:
    candidate = subject.get("candidate_sha") or current_identity.get("candidate_sha")
    pr_number = subject.get("pr_number") or current_identity.get("pr_number")
    results: list[str] = []
    if isinstance(candidate, str):
        results.append(f"Candidate {candidate}")
    if isinstance(pr_number, int):
        results.append(f"PR #{pr_number}")
    for key in ("managed_checkout", "repair_checkout", "parent_checkout"):
        checkout = subject.get(key)
        if isinstance(checkout, str):
            results.append(f"Managed Checkout {checkout}")
            break
    return "；".join(results) if results else "当前状态与已有审计证据"


def _operator_next_action(
    state: dict[str, Any],
    action_kind: str,
    reason: object,
    *,
    fallback_next_action: str,
    language: str = "zh",
    human: bool = False,
) -> str:
    repository = state.get("repository")
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, dict) else None
    if action_kind in {
        "human_blocker",
        "review_budget_checkpoint",
        "execution_failure",
        "supervision_timeout",
        "operator_stopped",
    } and isinstance(repository, str) and isinstance(parent_number, int):
        return f"agent-run resume {parent_number} --repo {repository}"
    if (
        action_kind == "final_approval"
        and isinstance(repository, str)
        and isinstance(parent_number, int)
    ):
        return f"agent-run approve {parent_number} --repo {repository}"
    if action_kind == "deterministic_contradiction":
        run_id = state.get("run_id")
        if state.get("status") == "unsupported_scope_change":
            return fallback_next_action
        if reason in {
            "candidate_or_acceptance_inconsistent",
            "foreign_run_pr",
        }:
            if isinstance(repository, str) and isinstance(parent_number, int):
                return f"agent-run abandon {parent_number} --repo {repository}"
            if isinstance(run_id, str):
                return action_command(state, "abandon", run_id, human=human)
        if isinstance(repository, str) and isinstance(parent_number, int):
            key = "guidance.action.external_contradiction"
            if human:
                key += ".human"
            return text(
                key, language=language,
                run=f"agent-run run {parent_number} --repo {repository}",
            )
    return fallback_next_action
