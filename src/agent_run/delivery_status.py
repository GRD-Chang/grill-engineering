from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent_run.delivery_budget import delivery_budget_owner

from agent_run.acceptance_facts import (
    current_findings,
    current_artifact,
    completed_acceptance_artifact,
    acceptance_subject,
    has_prior_acceptance_failure,
)
from agent_run.delivery_facts import current_work_subject, delivery_object_label, ticket_progress, multi_ticket_progress, role_family, ticket_phase_execution_activity, scheduler_progress
from agent_run.execution_timing import invocation_execution_seconds, invocation_activity, parse_optional_timestamp

from agent_run.messages import display_text, display_language

from agent_run.status_actions import status_timing
from agent_run.waiting_presentation import cleanup_instruction
from agent_run.presentation_helpers import human_ci_fix_usage, human_agent_role, human_pause_reason, human_status_term, terminal_safe, unknown_execution_guidance


def _ci_recovery_usage(recovery: dict[str, Any]) -> str:
    return display_text('presentation.status.ci_development_usage',
                        window=recovery.get('window'),
                        used=recovery.get('developments_used'),
                        limit=recovery.get('development_limit'))


def _ci_recovery_details(recovery: dict[str, Any]) -> list[str]:
    if not recovery.get("failure_head"):
        return []
    details = [display_text(
        'presentation.status.ci_review_required'
        if recovery.get('review_required') else 'presentation.status.ci_review_exempt'
    )]
    head = recovery.get('failure_head')
    if isinstance(head, str) and head:
        details.append(display_text('presentation.status.ci_failure_head', head=head))
    checks = recovery.get('failed_checks')
    if isinstance(checks, list):
        names = [
            f"{check['workflow']} / {check['name']}"
            for check in checks
            if isinstance(check, dict)
            and isinstance(check.get('workflow'), str)
            and isinstance(check.get('name'), str)
        ]
        if names:
            details.append(display_text(
                'presentation.status.ci_failed_checks', checks=', '.join(names)
            ))
    return details


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
    control = audit.get("executor_control")
    if (activity == "not_running" and current is not None
        and current[1].get("phase") in {"committing_candidate", "creating_pr"}
        and (not isinstance(control, dict) or control.get("activity") == "unknown")):
        activity = "unknown"
    activity = ticket_phase_execution_activity(state, audit, activity)
    findings = current_findings(state)
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
    timing = status_timing(state, invocation)
    if current_agent is not None and "round_seconds" in timing:
        current_agent["duration_seconds"] = timing["round_seconds"]
    result = {
        "repository": state.get("repository"),
        "parent": {
            "number": parent_view.get("number"),
            "title": parent_view.get("title"),
        },
        "status": audit.get("status"),
        "phase": audit.get("phase"),
        "current_object": current_object,
        "ticket_progress": ticket_progress(state),
        "round_progress": _round_progress(
            state, current, audit.get("review_budget"), invocation
        ),
        "run_repair": _run_repair_progress(audit.get("run_repair")),
        "integration_resync": audit.get("integration_resync"),
        "integration_development": audit.get("integration_development"),
        "ci_recovery": audit.get("ci_recovery"),
        "elapsed_seconds": audit.get("elapsed_seconds"),
        "current_agent": current_agent,
        "execution_activity": activity,
        "scheduler": scheduler_progress(control),
        "findings": findings,
        "conclusion": _current_conclusion(
            state, current, findings, activity=activity
        ),
        "next_action": audit.get("next_action"),
    }
    multi_ticket = multi_ticket_progress(
        state,
        executor_activity=(
            control.get("activity", "unknown") if isinstance(control, dict) else "unknown"
        ),
    )
    if multi_ticket is not None:
        result["multi_ticket"] = multi_ticket
    return result


def invocation_recovery_details(invocation: dict[str, Any]) -> list[str]:
    """Summarize cumulative recovery facts without adding timeline events."""
    details: list[str] = []
    if invocation.get("ordinary_recovery_used") is True:
        details.append(display_text('presentation.status.recovery_this_round_ordinary_automatic_recovery_triggered_once'))
    capacity_count = invocation.get("capacity_recovery_count")
    if type(capacity_count) is int and capacity_count > 0:
        details.append(display_text('presentation.status.recovery_this_round_capacity_wait_triggered_v1_times_resume_autom', v1=capacity_count))
    return details


def execution_guidance(
    state: dict[str, Any], activity: str, *, include_resume_command: bool = True
) -> str:
    if activity == "capacity_wait":
        return display_text('presentation.status.model_capacity_unavailable_resuming_automatically_after_30_second')
    if activity == "recovery_wait":
        return display_text('presentation.status.execution_error_automatically_resuming_existing_work_the_agent_is')
    if activity == "interrupted":
        guidance = display_text('presentation.status.execution_interrupted_waiting_to_resume')
        if include_resume_command:
            parent = state.get("parent")
            number = parent.get("number") if isinstance(parent, dict) else None
            repository = state.get("repository")
            command = (
                f"agent-run resume {number} --repo {repository}"
                if type(number) is int and isinstance(repository, str)
                else "agent-run resume <run-id>"
            )
            guidance += display_text('presentation.status.use_v1_to_continue_existing_work', v1=command)
        return f"{guidance}。"
    return unknown_execution_guidance(state, include_command=include_resume_command)


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


def _terminal_safe(value: object) -> str:
    """Keep persisted/user text from injecting terminal control sequences."""

    return terminal_safe(value)


def _status_symbol(value: str) -> str:
    if value in {"completed"}:
        return "✓"
    if value in {"execution_failed", "blocked"}:
        return "!"
    if value in {"ready_for_human", "operator_stopped", "supervision_timeout"}:
        return "?"
    return "→"


def _phase_term(
    value: object,
    current_object: object,
    fallback: Callable[[object], object],
) -> object:
    if value == "pending":
        return {
            "Run Acceptance": display_text('presentation.status.awaiting_acceptance'),
            "Run Publication": display_text('presentation.status.awaiting_publication'),
        }.get(str(current_object), display_text('presentation.status.pending'))
    return fallback(value)


def _human_conclusion(value: object) -> object:
    if display_language() != "zh" and isinstance(value, str):
        return display_text("presentation.conclusion." + value)
    return display_text('presentation.status.current_candidate_passed_acceptance') if value == "当前有效通过" else value


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
        "model": source.get("model") or display_text('presentation.status.unbound'),
        "reasoning_effort": source.get("reasoning_effort") or display_text('presentation.status.unbound'),
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
        return display_text('presentation.status.no_tickets_working_directly_on_the_requirement')
    progress = view.get("ticket_progress")
    if state.get("delivery_type") != "ticket_run" or not isinstance(progress, dict):
        return display_text('presentation.status.progress_cannot_be_confirmed')
    completed, total = progress.get("completed"), progress.get("total")
    if (
        type(completed) is not int or type(total) is not int
        or total <= 0 or not 0 <= completed <= total
    ):
        return display_text('presentation.status.progress_cannot_be_confirmed')
    text = display_text('presentation.status.completed_v1_v3_tickets', v1=completed, v3=total)
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
        return text + display_text('presentation.status.awaiting_your_confirmation_before_publication')
    if publication_phase == "publishing":
        return text + display_text('presentation.status.publishing')
    if publication_phase not in {None, "pending"}:
        return text
    if acceptance_phase == "reviewing":
        return text + display_text('presentation.status.acceptance_in_progress')
    if acceptance_phase in {None, "pending"}:
        return text + display_text('presentation.status.acceptance_and_publication_still_required')
    return text


def _multi_ticket_lines(view: dict[str, Any]) -> list[str]:
    multi = view.get("multi_ticket")
    if not isinstance(multi, dict):
        return []
    zh = display_language() == "zh"
    holder = multi.get("integration_holder")
    limit = multi.get("work_concurrency_limit")
    lines = [
        f"Ticket Agent 工作上限：{limit}；集成席位：{f'Ticket #{holder}' if holder is not None else '空闲'}"
        if zh else
        f"Ticket Agent work limit: {limit}; integration holder: {f'Ticket #{holder}' if holder is not None else 'free'}"
    ]
    executor_activity = multi.get("executor_activity")
    activity_key = {
        "running": "executor_activity_running",
        "not_running": "executor_activity_exited",
    }.get(str(executor_activity), "executor_activity_unknown")
    lines.append(display_text(
        'presentation.status.executor_activity',
        activity=display_text(f'presentation.status.{activity_key}'),
    ))
    for item in multi.get("tickets", []):
        if not isinstance(item, dict):
            continue
        phase = item.get("phase")
        phase_label = str(human_status_term(phase))
        parts = [f"Ticket #{item.get('ticket_number')}: {phase_label}"]
        position = item.get("queue_position")
        if type(position) is int:
            parts.append(f"集成队列第 {position} 位" if zh else f"integration queue #{position}")
        work_position = item.get("work_queue_position")
        if item.get("resume_accepted"):
            parts.append("恢复已接受" if zh else "Resume accepted")
        if type(work_position) is int:
            parts.append(f"等待工作名额，第 {work_position} 位" if zh else f"waiting for work slot #{work_position}")
        stop_reason = item.get("stop_wait_reason")
        if stop_reason:
            descriptions = {
                "agent_exit": ("正在停止，等待 Agent 退出", "stopping; waiting for Agent exit"),
                "remote_reconciliation": ("正在停止，等待远端结果对账", "stopping; reconciling remote result"),
                "local_boundary": ("正在停止，等待本地步骤收口", "stopping; finishing local step"),
            }
            parts.append(descriptions[str(stop_reason)][0 if zh else 1])
        if item.get("has_work_slot") and executor_activity == "running" and not stop_reason:
            parts.append("正在工作" if zh else "working")
        if item.get("holds_integration"):
            parts.append("持有集成席位" if zh else "holds integration")
        usage = item.get("usage")
        if isinstance(usage, dict) and usage.get("development_attempts") is not None:
            labels = (
                ("开发", "development", "development_attempts", "development_limit"),
                ("验收", "review", "reviewer_invocations", "review_limit"),
                ("CI 开发", "CI development", "ci_developments_used", "ci_development_limit"),
                ("集成开发", "integration development", "integration_development_used", "integration_development_limit"),
                ("同步", "resync", "integration_resync_used", "integration_resync_limit"),
            )
            parts.append(" / ".join(
                f"{cn if zh else en} {usage[used]}/{usage[limit]}"
                for cn, en, used, limit in labels
                if usage.get(used) is not None and usage.get(limit) is not None
            ))
        blockers = item.get("blocked_by")
        if blockers:
            parts.append(("依赖未完成：" if zh else "dependencies incomplete: ") + ", ".join(f"#{number}" for number in blockers))
        reason = item.get("blocked_reason")
        if reason and reason != "waiting_for_dependencies":
            parts.append(("阻塞：" if zh else "blocked: ") + str(human_pause_reason(reason)))
        action = item.get("recovery_action")
        if action:
            parts.append(("可恢复动作：" if zh else "recovery action: ") + str(action))
        lines.append(" · ".join(parts))
    return lines


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
        "Ticket Development": display_text('presentation.status.current_ticket'),
        "Parent Development": display_text('presentation.status.current_parent_requirement'),
        "Run Development": display_text('presentation.status.current_run_acceptance_cycle'),
    }.get(str(rounds.get("development_label")), display_text('presentation.status.current_work'))
    details = []
    if rounds.get("cycle_development_attempts") is not None:
        details.append(
            display_text('presentation.status.v0_totals_development_v2_acceptance_v4', v0=scope, v2=rounds['cycle_development_attempts'], v4=rounds.get('cycle_review_attempts', display_text('presentation.status.unknown')))
        )
    details.append(
        display_text('presentation.status.current_allowance_used_development_v1_v3_acceptance_v5_v7', v1=rounds.get('development_attempts'), v3=rounds.get('development_limit'), v5=rounds.get('reviewer_invocations'), v7=rounds.get('reviewer_limit'))
    )
    if rounds.get("checkpoint_reason"):
        details.append(display_text('presentation.status.pause_reason_v1', v1=human_pause_reason(rounds['checkpoint_reason'])))
    details.append(
        display_text('presentation.status.extra_ci_repair') + human_ci_fix_usage(
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


def _current_conclusion(
    state: dict[str, Any],
    current: tuple[str, dict[str, Any]] | None,
    findings: list[str],
    *,
    activity: str,
) -> str:
    if current is None:
        if completed_acceptance_artifact(state) is not None:
            return "当前有效通过"
        return "尚无有效验收结论"
    location, public_subject = current
    subject = acceptance_subject(state, current)
    phase = subject.get("phase")
    if phase == "reviewing":
        if activity == "interrupted":
            return "验收已中断，等待恢复"
        if activity == "unknown":
            return "验收状态无法确认"
        if activity == "not_running":
            return "验收等待执行"
        review_kind = "复验" if has_prior_acceptance_failure(subject, budget_owner=delivery_budget_owner(state, subject)) else "首次验收"
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
    artifact = current_artifact(subject, budget_owner=delivery_budget_owner(state, subject))
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


_FINDING_PARTS = re.compile(
    r"^问题：(.*?)；证据：(.*?)；必须修复：(.*?)；复验：(.*)$"
)


def _subject_label(state: dict[str, Any], subject: str) -> str:
    return delivery_object_label(state, subject)


def _role_label(role: str) -> str:
    family = role_family(role)
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
    deadline = parse_optional_timestamp(value)
    return max(0, int((deadline - datetime.now(UTC)).total_seconds())) if deadline else None


def _duration(seconds: object) -> str:
    from agent_run.presentation_helpers import execution_duration

    return execution_duration(seconds)


def _operator_instruction(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    cleanup = cleanup_instruction(state)
    if cleanup:
        return cleanup
    if invocation is not None and invocation.get("status") in {"running", "resuming"}:
        return display_text('presentation.status.no_action_is_required_at_present')
    if state.get("status") in {"completed", "abandoned"}:
        return display_text('presentation.status.no_action_required')
    return display_text('presentation.status.continue_with_the_command_above_do_not_start_this_task_again')


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
        return display_text('presentation.status.source_branch_or_checkout_commits_changed_new_work_has_been_prese')
    if "branch identity changed" in error or "HEAD is not the managed branch" in error:
        return display_text('presentation.status.the_checkout_changed_branch_or_has_detached_head_it_has_been_pres')
    if any(reason in error for reason in ("tracked modifications", "untracked", "dirty checkout")):
        return display_text('presentation.status.the_checkout_has_uncommitted_files_it_has_been_preserved')
    return error if len(error) <= 240 else error[:232] + display_text('presentation.status.truncated')
