from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.delivery_policy import ticket_budget_policy_for_job
from agent_run.external_supervision import public_supervision_snapshot
from agent_run.state_contract import human_blocker_subject_count
from agent_run.review_budget import RUN_POLICY
from agent_run.resume_audit import latest_resume_audit
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.semantic_attempt import invocation_is_explicitly_resumable

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
                "abandonment": state.get("run_abandonment"),
                "delivery_cleanup": _public_delivery_cleanup(state),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _print_status(state: dict[str, object], *, as_json: bool) -> None:
    active = _active_ticket_job(state)
    active_ticket = active.get("ticket_number") if active else None
    worker = _current_worker(state)
    run_repair = _run_repair_status(state)
    review_budget = _public_review_budget(state)
    current_identity = _current_delivery_identity(state)
    invocation = state.get("active_agent_invocation")
    active_invocation = (
        invocation
        if isinstance(invocation, dict)
        and invocation.get("status") in {"running", "failed", "resuming"}
        else None
    )
    semantic_attempt = _current_semantic_attempt(state, active_invocation)
    delivery_cleanup = _public_delivery_cleanup(state)
    latest_resume = latest_resume_audit(state)
    output = {
        "run_id": state.get("run_id"),
        "repository": state.get("repository"),
        "parent": state.get("parent"),
        "status": state.get("status"),
        "active_ticket": active_ticket,
        "phase": _current_phase(state),
        "worker": worker,
        "run_repair": run_repair,
        "candidate_sha": current_identity.get("candidate_sha"),
        "pr_number": current_identity.get("pr_number"),
        "review_budget": review_budget,
        "elapsed_seconds": _elapsed_seconds(state.get("created_at")),
        "gate": "required_checks" if state.get("status") == "waiting_checks" else None,
        "next_action": _next_action(state),
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
        "abandonment": state.get("run_abandonment"),
        "agent_invocation": active_invocation,
        "semantic_agent_attempt": semantic_attempt,
        "output_attempt": _output_attempt(active_invocation),
        "budget_window": (
            semantic_attempt.get("budget_window")
            if isinstance(semantic_attempt, dict)
            else (
                review_budget.get("window") if isinstance(review_budget, dict) else None
            )
        ),
        "publication_operation_retry": _current_publication_operation_retry(state),
        "delivery_cleanup": delivery_cleanup,
        "latest_resume": latest_resume,
        "supervision": public_supervision_snapshot(state),
    }
    if as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    print(f"交付运行: {output['run_id']}")
    print(f"运行状态: {_display_term(output['status'])}")
    print(f"当前阶段: {_display_term(output['phase'])}")
    if active_ticket is not None:
        print(f"当前任务: #{active_ticket}")
    print(
        "当前 Candidate/PR: "
        f"{output['candidate_sha'] or 'none'} / {output['pr_number'] or 'none'}"
    )
    if worker is not None:
        print(
            "当前工作代理: "
            f"{worker['role']}（第 {worker['attempt']} 次尝试，{_display_term(worker['phase'])}，"
            f"会话 {worker['thread_id'] or '尚不可用'}）"
        )
    if isinstance(active_invocation, dict):
        invocation_role = (
            active_invocation.get("invocation_role")
            or active_invocation.get("role")
            or active_invocation.get("binding_role")
        )
        profile_role = active_invocation.get("profile_role") or active_invocation.get(
            "binding_role"
        )
        role_text = str(invocation_role)
        if profile_role is not None and profile_role != invocation_role:
            role_text += f"（Profile {profile_role}）"
        print(
            "当前 Codex: "
            f"{role_text}；"
            f"Thread {active_invocation.get('reported_thread_id') or active_invocation.get('requested_thread_id') or '尚不可用'}；"
            f"model {active_invocation.get('model') or '未绑定'}；"
            f"reasoning effort {active_invocation.get('reasoning_effort') or '未绑定'}；"
            f"Profile Revision {active_invocation.get('profile_revision') or '未绑定'}"
        )
    else:
        print("当前 Codex: none")
    if isinstance(semantic_attempt, dict):
        print(
            "Semantic Agent Attempt: "
            f"{semantic_attempt.get('attempt_id')}；"
            f"{semantic_attempt.get('role')}；"
            f"ordinal {semantic_attempt.get('ordinal')}；"
            f"{semantic_attempt.get('status')}"
        )
    else:
        print("Semantic Agent Attempt: none")
    if isinstance(active_invocation, dict):
        print(
            "Agent Invocation: "
            f"{active_invocation.get('role')} {active_invocation.get('status')}"
        )
    else:
        print("Agent Invocation: none")
    output_attempt = output["output_attempt"]
    print(
        "Output Attempt: "
        f"{output_attempt.get('attempt_count') if isinstance(output_attempt, dict) else 'none'}"
    )
    print(f"Budget Window: {output['budget_window'] or 'none'}")
    operation_retry = output["publication_operation_retry"]
    if isinstance(operation_retry, dict):
        print(
            "Publication Operation Retry: "
            f"{operation_retry.get('attempts')}/{operation_retry.get('limit')}"
        )
    else:
        print("Publication Operation Retry: none")
    if isinstance(delivery_cleanup, dict):
        print(
            "Delivery Cleanup: "
            f"{delivery_cleanup.get('status')}；{delivery_cleanup.get('last_error')}"
        )
        items = delivery_cleanup.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                print(
                    "  Preserved Checkout: "
                    f"{item.get('checkout')}；{item.get('last_error')}；"
                    f"恢复={item.get('recovery_action')}"
                )
    if isinstance(latest_resume, dict):
        print(
            "最近显式 Resume: "
            f"#{latest_resume.get('sequence')} {latest_resume.get('kind')}；"
            f"Thread {latest_resume.get('thread_id') or 'none'}；"
            f"Attempt {latest_resume.get('semantic_attempt_id') or 'none'}；"
            f"failure={latest_resume.get('failure_code') or 'none'}"
        )
    if isinstance(run_repair, dict):
        print(
            "运行修复: "
            f"Run Acceptance Generation {run_repair['acceptance_generation']}；"
            f"Repair Cycle Generation {run_repair['repair_cycle_generation']}；"
            f"代码修改 {run_repair['code_modification_attempts']}/10；"
            f"Candidate 验证 {run_repair['validation_attempts']} 次；"
            "Candidate 验证状态 "
            f"{_display_term(run_repair['candidate_validation_status'])}"
        )
    if isinstance(review_budget, dict):
        print(
            "Review Budget: "
            f"Window {review_budget['window']}；"
            f"Development {review_budget['development_attempts']}/{review_budget['development_limit']}；"
            f"Reviewer {review_budget['reviewer_invocations']}/{review_budget['reviewer_limit']}；"
            f"Final CI-fix {'used' if review_budget['final_ci_fix_used'] else 'available'}"
        )
    print(f"已运行: {output['elapsed_seconds']} 秒")
    if output["gate"]:
        print("当前门禁: 必需检查")
    _print_supervision(output["supervision"])
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
    abandonment = output["abandonment"]
    if isinstance(abandonment, dict):
        print(f"放弃恢复: {abandonment.get('phase')}")
    print(f"下一步: {output['next_action']}")


def _print_history(state: dict[str, object], *, as_json: bool) -> None:
    timeline = state.get("timeline", [])
    if not isinstance(timeline, list):
        raise ValueError("timeline must be an array")
    invocations = state.get("agent_invocation_history", [])
    if not isinstance(invocations, list):
        raise ValueError("agent_invocation_history must be an array")
    semantic_attempts = _semantic_attempt_history(state)
    operation_retries = _publication_operation_retries(state)
    resume_audit = state.get("resume_audit")
    public_resume_audit = resume_audit if isinstance(resume_audit, dict) else {}
    agent_resumes = public_resume_audit.get("history", [])
    if not isinstance(agent_resumes, list):
        raise ValueError("resume_audit.history must be an array")
    output = {
        "run_id": state.get("run_id"),
        "timeline": timeline,
        "next_action": _next_action(state),
        "abandonment": state.get("run_abandonment"),
        "agent_invocations": invocations,
        "semantic_agent_attempts": semantic_attempts,
        "output_attempts": [
            {
                "invocation_started_at": invocation.get("started_at"),
                "work_subject": invocation.get("work_subject"),
                "attempt_count": invocation.get("attempt_count"),
            }
            for invocation in invocations
            if isinstance(invocation, dict)
        ],
        "budget_windows": _budget_windows(semantic_attempts),
        "publication_operation_retries": operation_retries,
        "resume_audit": {
            "total": public_resume_audit.get("total", 0),
            "compacted": public_resume_audit.get("compacted", 0),
            "rolling_digest": public_resume_audit.get("rolling_digest"),
        },
        "agent_resumes": agent_resumes,
        "supervision": public_supervision_snapshot(state),
    }
    if as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    print(f"交付运行: {output['run_id']}")
    _print_supervision(output["supervision"])
    for attempt in semantic_attempts:
        print(
            "Semantic Agent Attempt "
            f"{attempt.get('attempt_id')} {attempt.get('role')} "
            f"ordinal={attempt.get('ordinal')} "
            f"Budget Window={attempt.get('budget_window') or 'none'} "
            f"status={attempt.get('status')} outcome={attempt.get('outcome')}"
        )
    for invocation in invocations:
        if not isinstance(invocation, dict):
            continue
        invocation_role = (
            invocation.get("invocation_role")
            or invocation.get("role")
            or invocation.get("binding_role")
        )
        profile_role = invocation.get("profile_role") or invocation.get("binding_role")
        role_text = str(invocation_role)
        if profile_role is not None and profile_role != invocation_role:
            role_text += f"(profile={profile_role})"
        print(
            "Agent Invocation "
            f"{role_text} {invocation.get('status')} "
            f"Output Attempt={invocation.get('attempt_count')} "
            f"return_code={invocation.get('return_code')} "
            f"signal={invocation.get('signal')} "
            f"requested={invocation.get('requested_thread_id')} "
            f"reported={invocation.get('reported_thread_id')} "
            f"model={invocation.get('model')} "
            f"effort={invocation.get('reasoning_effort')} "
            f"profile_revision={invocation.get('profile_revision')} "
            f"error={invocation.get('error')}"
        )
    for retry in operation_retries:
        print(
            "Publication Operation Retry "
            f"{retry.get('work_subject')} "
            f"{retry.get('attempts')}/{retry.get('limit')}"
        )
    for resume in agent_resumes:
        if not isinstance(resume, dict):
            continue
        print(
            "Explicit Resume "
            f"#{resume.get('sequence')} {resume.get('kind')} "
            f"status={resume.get('source_status')} "
            f"failure={resume.get('failure_code')} "
            f"Thread={resume.get('thread_id')} "
            f"Attempt={resume.get('semantic_attempt_id')} "
            f"new_thread={resume.get('new_thread')}"
        )
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
        if event.get("semantic_attempt_id") is not None:
            print(
                "  Semantic Agent Attempt "
                f"{event.get('semantic_attempt_id')} "
                f"{event.get('semantic_attempt_role')} "
                f"ordinal={event.get('semantic_attempt_ordinal')}；"
                f"Agent Invocation {event.get('agent_invocation_status')}；"
                f"Output Attempt={event.get('output_attempt')}；"
                f"Budget Window={event.get('budget_window') or 'none'}；"
                "Publication Operation Retry="
                f"{event.get('publication_operation_retry_attempts') or 'none'}/"
                f"{event.get('publication_operation_retry_limit') or 'none'}"
            )
        if event.get("explicit_resume_sequence") is not None:
            print(
                "  Explicit Resume "
                f"#{event.get('explicit_resume_sequence')} "
                f"{event.get('explicit_resume_kind')}；"
                f"Thread={event.get('explicit_resume_thread_id') or 'none'}；"
                f"Attempt={event.get('explicit_resume_attempt_id') or 'none'}"
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


def _current_semantic_attempt(
    state: dict[str, object], invocation: dict[str, object] | None
) -> dict[str, object] | None:
    invocation_attempt = (
        invocation.get("semantic_attempt") if isinstance(invocation, dict) else None
    )
    invocation_attempt_id = (
        invocation_attempt.get("attempt_id")
        if isinstance(invocation_attempt, dict)
        else None
    )
    pending_attempts = [
        pending
        for subject in semantic_attempt_subjects(state)
        if isinstance((pending := subject.get("pending_semantic_attempt")), dict)
    ]
    if invocation_attempt_id is not None:
        for attempt in pending_attempts:
            if attempt.get("attempt_id") == invocation_attempt_id:
                return attempt
    return pending_attempts[0] if pending_attempts else None


def _semantic_attempt_history(state: dict[str, object]) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    seen: set[str] = set()
    for subject in semantic_attempt_subjects(state):
        history = subject.get("semantic_attempt_history")
        values = history if isinstance(history, list) else []
        pending = subject.get("pending_semantic_attempt")
        if isinstance(pending, dict):
            values = [*values, pending]
        for attempt in values:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id in seen:
                continue
            seen.add(attempt_id)
            attempts.append(attempt)
    return attempts


def _output_attempt(
    invocation: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(invocation, dict):
        return None
    return {
        "invocation_started_at": invocation.get("started_at"),
        "attempt_count": invocation.get("attempt_count"),
    }


def _budget_windows(
    attempts: list[dict[str, object]],
) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for attempt in attempts:
        window = attempt.get("budget_window")
        if window is None:
            continue
        key = (attempt.get("work_subject"), attempt.get("role"), window)
        if key in seen:
            continue
        seen.add(key)
        windows.append(
            {
                "work_subject": attempt.get("work_subject"),
                "role": attempt.get("role"),
                "window": window,
            }
        )
    return windows


def _publication_operation_retry(
    subject: dict[str, object],
) -> dict[str, object] | None:
    retry = subject.get("publication_operation_retry")
    if not isinstance(retry, dict):
        return None
    semantic_attempt_id: object = None
    work_subject: object = None
    pending = subject.get("pending_semantic_attempt")
    if isinstance(pending, dict) and pending.get("role") == "publication":
        semantic_attempt_id = pending.get("attempt_id")
        work_subject = pending.get("work_subject")
    if work_subject is None:
        history = subject.get("semantic_attempt_history")
        if isinstance(history, list):
            for attempt in reversed(history):
                if (
                    isinstance(attempt, dict)
                    and attempt.get("role") == "publication"
                    and attempt.get("ordinal") == subject.get("publication_attempts")
                ):
                    semantic_attempt_id = attempt.get("attempt_id")
                    work_subject = attempt.get("work_subject")
                    break
    if work_subject is None and isinstance(subject.get("ticket_number"), int):
        work_subject = f"ticket:{subject['ticket_number']}"
    return {
        "semantic_attempt_id": semantic_attempt_id,
        "work_subject": work_subject,
        "attempts": retry.get("attempts"),
        "limit": retry.get("limit"),
    }


def _publication_operation_retries(
    state: dict[str, object],
) -> list[dict[str, object]]:
    retries: list[dict[str, object]] = []
    seen: set[object] = set()
    for subject in semantic_attempt_subjects(state):
        projected: list[dict[str, object]] = []
        retry = _publication_operation_retry(subject)
        if retry is not None:
            projected.append(retry)
        history = subject.get("semantic_attempt_history")
        if isinstance(history, list):
            for attempt in history:
                if not isinstance(attempt, dict):
                    continue
                attempt_retry = attempt.get("publication_operation_retry")
                if not isinstance(attempt_retry, dict):
                    continue
                projected.append(
                    {
                        "semantic_attempt_id": attempt.get("attempt_id"),
                        "work_subject": attempt.get("work_subject"),
                        "attempts": attempt_retry.get("attempts"),
                        "limit": attempt_retry.get("limit"),
                    }
                )
        for item in projected:
            key = item.get("semantic_attempt_id") or (
                item["work_subject"],
                item["attempts"],
                item["limit"],
            )
            if key in seen:
                continue
            seen.add(key)
            retries.append(item)
    return retries


def _current_publication_operation_retry(
    state: dict[str, object],
) -> dict[str, object] | None:
    current_attempt = _current_semantic_attempt(state, None)
    current_id = current_attempt.get("attempt_id") if current_attempt else None
    for subject in semantic_attempt_subjects(state):
        pending = subject.get("pending_semantic_attempt")
        if current_id is not None:
            if not isinstance(pending, dict) or pending.get("attempt_id") != current_id:
                continue
            return _publication_operation_retry(subject)
        retry = _publication_operation_retry(subject)
        if retry is not None:
            return retry
    return None


def _public_delivery_cleanup(
    state: dict[str, object],
) -> dict[str, object] | None:
    cleanup = state.get("delivery_cleanup")
    if not isinstance(cleanup, dict):
        return None
    raw_items = cleanup.get("items")
    items: list[dict[str, object]] = []
    recovery_action = f"agent-run resume {state.get('run_id')}"
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if isinstance(raw_items, dict):
        for key in sorted(raw_items, key=str):
            item = raw_items[key]
            if not isinstance(item, dict) or item.get("status") == "completed":
                continue
            item_recovery_action = recovery_action
            if item.get("recovery_kind") == "stale_dirty_checkout":
                item_recovery_action = (
                    f"inspect and copy/salvage {item.get('checkout')} to a safe location; "
                    f"make the stale checkout clean, then use agent-run run {parent_number} "
                    "to retire it and continue fresh Run Acceptance, "
                    f"or agent-run abandon {state.get('run_id')} --discard-worktree"
                )
            items.append(
                {
                    "kind": item.get("kind"),
                    "branch": item.get("branch"),
                    "checkout": item.get("checkout"),
                    "status": item.get("status"),
                    "last_error": item.get("last_error"),
                    "recovery_action": item_recovery_action,
                }
            )
    return {
        "status": cleanup.get("status"),
        "last_error": cleanup.get("last_error"),
        "items": items,
    }


def _print_supervision(wait: object) -> None:
    if not isinstance(wait, dict):
        return
    print(f"等待种类: {wait.get('kind')}")
    print(f"等待对象: {wait.get('subject')}")
    print(f"等待 head/base: {wait.get('head_sha')} / {wait.get('base_sha')}")
    print(
        "等待窗口: "
        f"开始={wait.get('started_at')} 截止={wait.get('deadline')} "
        f"剩余={wait.get('remaining_seconds')} 秒"
    )
    print(f"重试次数: {wait.get('retry_count')}")
    observation = wait.get("latest_observation")
    if isinstance(observation, dict):
        print(
            "最新观测: "
            f"{observation.get('code')} {observation.get('message')}"
        )
    else:
        print("最新观测: 无")
    failure_class = wait.get("credential_failure_class")
    if isinstance(failure_class, str):
        print(f"凭据失败类别: {failure_class}")
    http_status = wait.get("credential_http_status")
    if type(http_status) is int:
        print(f"凭据 HTTP 状态: {http_status}")
    if wait.get("timeout_resume_action") is not None:
        print(f"超时恢复: {wait['timeout_resume_action']}")


def _next_action(state: dict[str, Any]) -> str:
    status = str(state.get("status"))
    run_id = state.get("run_id")
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    cleanup = state.get("delivery_cleanup")
    if (
        isinstance(cleanup, dict)
        and cleanup.get("status") == "cleanup_pending"
        and isinstance(run_id, str)
    ):
        items = cleanup.get("items")
        if isinstance(items, dict) and any(
            isinstance(item, dict)
            and item.get("status") != "completed"
            and item.get("recovery_kind") == "stale_dirty_checkout"
            for item in items.values()
        ):
            return (
                "先检查并把 stale Managed Development Checkout 的成果转存到安全位置，"
                "再使旧 checkout 恢复 clean；"
                f"随后用 agent-run run {parent_number} 退休旧 checkout 并继续 fresh Run Acceptance，"
                f"或用 agent-run abandon {run_id} --discard-worktree 明确丢弃"
            )
        return f"agent-run resume {run_id}"
    if status in {"run_approval_pending", "parent_approval_pending"} and isinstance(
        run_id, str
    ):
        return f"agent-run approve {run_id}"
    if status == "unsupported_scope_change":
        return "查看变化摘要后执行 agent-run abandon，或在 GitHub 恢复原 Ticket Graph"
    if status == "deterministic_contradiction":
        return "处理诊断中的确定性矛盾；如需终止执行 agent-run abandon"
    if status == "abandonment_pending" and isinstance(run_id, str):
        return f"agent-run abandon {run_id}"
    if status == "requeue_required" and isinstance(run_id, str):
        return f"agent-run requeue {run_id}"
    if invocation_is_explicitly_resumable(state) and isinstance(run_id, str):
        return f"agent-run resume {run_id}"
    if (
        status == "waiting_external"
        and isinstance(state.get("requeue_transition"), dict)
    ):
        return f"agent-run run {parent_number}"
    if status == "waiting_external":
        return f"agent-run run {parent_number}"
    if status == "supervision_timeout" and isinstance(run_id, str):
        return f"agent-run resume {run_id}"
    if (
        status in {"ready_for_human", "progress_exhausted"}
        and human_blocker_subject_count(state) == 1
        and isinstance(run_id, str)
    ):
        return f"agent-run resume {run_id}"
    if status in {"ready_for_human", "progress_exhausted", "blocked"}:
        return "处理诊断中的人工事项"
    if status == "publication_pending":
        return (
            "检查已耗尽的 Publication Operation Retry；无法恢复时执行 agent-run abandon"
        )
    if status in {
        "active",
        "ticket_completed",
        "parent_delivery_pending",
        "run_acceptance_pending",
        "run_publication_pending",
        "waiting_checks",
        "waiting_merge",
        "parent_closeout_pending",
        "execution_failed",
        "supervision_timeout",
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
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            return _worker_from_job(repair, run_repair=True)
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


def _current_delivery_identity(state: dict[str, object]) -> dict[str, object]:
    """Return the Candidate/PR boundary operators need for diagnosis."""

    active = _active_ticket_job(state)
    if active is not None:
        return {
            "candidate_sha": active.get("candidate_sha"),
            "pr_number": active.get("pr_number"),
        }
    parent = state.get("parent_job")
    if isinstance(parent, dict) and parent.get("phase") not in {
        "completed",
        "merged",
        "abandoned",
    }:
        return {
            "candidate_sha": parent.get("candidate_sha"),
            "pr_number": parent.get("pr_number"),
        }
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            return {
                "candidate_sha": repair.get("candidate_sha"),
                "pr_number": repair.get("pr_number"),
            }
        candidate = acceptance.get("reviewed_head_sha") or acceptance.get(
            "candidate_sha"
        )
        publication = state.get("run_publication")
        pr_number = publication.get("pr_number") if isinstance(publication, dict) else None
        if candidate is not None or pr_number is not None:
            return {"candidate_sha": candidate, "pr_number": pr_number}
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        record = publication.get("record")
        return {
            "candidate_sha": (
                record.get("run_head_sha")
                if isinstance(record, dict)
                else publication.get("head_sha")
            ),
            "pr_number": publication.get("pr_number"),
        }
    return {"candidate_sha": None, "pr_number": None}


def _worker_from_job(
    job: dict[str, object], *, run_repair: bool = False
) -> dict[str, object] | None:
    phase = str(job.get("phase"))
    review_role = "运行修复验收工作代理" if run_repair else "独立验收工作代理"
    publication_role = "运行修复发布工作代理" if run_repair else "发布工作代理"
    development_role = "运行修复开发工作代理" if run_repair else "开发工作代理"
    if phase in {"reviewing", "validating"}:
        reviewer_ids = job.get("reviewer_thread_ids")
        thread_id = reviewer_ids[-1] if isinstance(reviewer_ids, list) and reviewer_ids else None
        return {
            "role": review_role,
            "attempt": job.get("validation_attempts"),
            "thread_id": thread_id,
            "phase": phase,
        }
    if phase in {"publishing", "publication_pending"}:
        return {
            "role": publication_role,
            "attempt": job.get("publication_attempts"),
            "thread_id": job.get("publication_thread_id"),
            "phase": phase,
        }
    if phase in {"developing", "repairing"}:
        return {
            "role": development_role,
            "attempt": job.get("pending_attempt", job.get("modification_attempts")),
            "thread_id": (
                None
                if isinstance(job.get("pending_attempt"), int)
                else job.get("development_thread_id")
            ),
            "phase": phase,
        }
    return None


def _run_repair_status(state: dict[str, object]) -> dict[str, object] | None:
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return None
    job = acceptance.get("repair_job")
    cycle = acceptance.get("repair_cycle")
    if not isinstance(job, dict) or not isinstance(cycle, dict):
        return None
    candidate_validation_status = _candidate_validation_status(job)
    return {
        # Keep ``generation`` and ``phase`` as compatibility aliases while
        # exposing each lifecycle dimension under an unambiguous public name.
        "generation": job.get("repair_generation"),
        "repair_cycle_generation": cycle.get("generation"),
        "acceptance_generation": acceptance.get("acceptance_generation"),
        "phase": job.get("phase"),
        # Keep the old phase-shaped name for clients already consuming it,
        # but expose the independently derived status as the canonical field.
        "candidate_validation_phase": candidate_validation_status,
        "candidate_validation_status": candidate_validation_status,
        "cycle_status": cycle.get("status"),
        "code_modification_attempts": cycle.get("code_modification_attempts", 0),
        "validation_attempts": cycle.get("validation_attempts", 0),
        "candidate_sha": job.get("candidate_sha"),
        "development_thread_id": cycle.get("development_thread_id"),
        "worktree": cycle.get("worktree"),
    }


def _public_review_budget(state: dict[str, object]) -> dict[str, object] | None:
    """Expose the active subject's bounded window without leaking policy logic."""

    subject: dict[str, object] | None = None
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and active.get("phase") not in {"completed", "merged"}:
        subject = active
        policy = ticket_budget_policy_for_job(
            active, state_snapshot=state.get("policy_snapshot")
        )
    else:
        parent = state.get("parent_job")
        if isinstance(parent, dict):
            subject = parent
            policy = RUN_POLICY
        else:
            acceptance = state.get("run_acceptance")
            if not isinstance(acceptance, dict):
                return None
            repair = acceptance.get("repair_job")
            subject = repair if isinstance(repair, dict) else acceptance
            policy = RUN_POLICY
    budget = subject.get("review_budget")
    if not isinstance(budget, dict):
        return None
    return {
        "window": budget.get("window"),
        "development_attempts": budget.get("development_attempts"),
        "development_limit": policy.development_limit,
        "reviewer_invocations": budget.get("reviewer_invocations"),
        "reviewer_limit": policy.review_limit,
        "final_ci_fix_used": budget.get("final_ci_fix_used"),
        "final_ci_fix_limit": policy.final_ci_fix_limit,
        "checkpoint_reason": budget.get("checkpoint_reason"),
        "candidate_sha": _current_delivery_identity(state).get("candidate_sha"),
        "pr_number": _current_delivery_identity(state).get("pr_number"),
    }


def _candidate_validation_status(job: dict[str, object]) -> str:
    """Report the current Candidate verdict independently of delivery progress."""

    if job.get("phase") == "stale":
        return "stale"
    record = job.get("acceptance_record")
    candidate_sha = job.get("candidate_sha")
    if (
        isinstance(record, dict)
        and isinstance(candidate_sha, str)
        and record.get("reviewed_candidate_sha") == candidate_sha
    ):
        try:
            artifact = AcceptanceArtifact.parse(record.get("artifact"))
        except ValueError:
            # An interrupted or legacy record is not a completed verdict.
            pass
        else:
            if artifact.is_accepted:
                return "pass"
            if artifact.has_failures:
                return "fail"
            return "blocked"
    if job.get("phase") == "reviewing":
        return "reviewing"
    if job.get("phase") == "blocked":
        return "blocked"
    return "unreviewed"


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
        "deterministic_contradiction": "确定性矛盾，需人工处理",
        "abandonment_pending": "等待放弃恢复",
        "progress_exhausted": "无可推进任务",
        "execution_failed": "执行失败，可恢复",
        "supervision_timeout": "监督超时暂停，可恢复",
        "blocked": "已阻塞",
        "unreviewed": "未验收",
        "pass": "已通过",
        "fail": "未通过",
        "stale": "已失效",
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
