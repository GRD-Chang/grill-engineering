from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.delivery_policy import (
    parent_only_budget_policy_for_job,
    run_repair_budget_policy_for_job,
    ticket_budget_policy_for_job,
)
from agent_run.final_approval_operation import (
    final_approval_cleanup_pending, has_final_approval,
)
from agent_run.delivery_progress import (
    history_progress_view,
    print_history_progress,
    print_rich_history_progress,
    print_rich_status_progress,
    print_status_progress,
    run_elapsed_seconds,
    status_progress_view,
)
from agent_run.external_supervision import public_supervision_snapshot
from agent_run.operator_action_presentation import (
    operator_action_view,
    print_operator_action as _print_operator_action,
)
from agent_run.presentation_helpers import current_work_subject, human_next_action
from agent_run.state_contract import human_blocker_subject_count
from agent_run.resume_audit import latest_resume_audit
from agent_run.run_lifecycle import ActionReceipt
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.semantic_attempt import invocation_is_explicitly_resumable


def public_action_receipt(
    receipt: ActionReceipt,
    *,
    repository: object,
    parent: object,
    next_action: object,
) -> dict[str, object]:
    """Project one lifecycle result into the operator-facing receipt."""

    parent_view = (
        {
            "number": parent.get("number"),
            "title": parent.get("title"),
        }
        if isinstance(parent, Mapping)
        else {"number": None, "title": None}
    )
    status = {
        "completed": "applied",
        "executor_active": "applied",
        "accepted": "in_progress",
        "applying": "in_progress",
        "failed": "failed",
    }.get(receipt.status, "in_progress")
    return {
        "repository": repository,
        "parent": parent_view,
        "operation": receipt.kind,
        "submission": "attached" if receipt.attached else "started",
        "status": status,
        "next_action": next_action,
    }

def _print_precondition_failure(
    state: dict[str, object], *, as_json: bool = False
) -> None:
    active = _active_ticket_job(state)
    diagnostics = state.get("diagnostics")
    current_diagnostics = diagnostics if isinstance(diagnostics, list) else []
    diagnostic = {
        "code": "command_precondition",
        "message": "当前交付运行尚未满足此命令的执行条件",
    }
    if as_json:
        print(
            json.dumps(
                {
                    "result": "rejected",
                    "run_id": state["run_id"],
                    "status": state["status"],
                    "run_branch": state.get(
                        "run_branch", state.get("parent_branch")
                    ),
                    "active_ticket": (
                        active.get("ticket_number") if active else None
                    ),
                    "diagnostics": [*current_diagnostics, diagnostic],
                    "scope_change": state.get("unsupported_scope_change"),
                    "abandonment": state.get("run_abandonment"),
                    "delivery_cleanup": _public_delivery_cleanup(state),
                    "next_action": _next_action(state),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else "?"
    print(f"Repository: {state.get('repository')}")
    print(f"Parent Issue: #{parent_number}")
    print(f"交付状态: {human_delivery_status(state.get('status'))}")
    print(f"命令状态: 未应用（{diagnostic['message']}）")
    print("下一步: " + str(human_next_action_for_state(state)))


def _print_status(
    state: dict[str, object], *, as_json: bool, plain: bool = False
) -> None:
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
    operator_action = _operator_action_view(state)
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
        "elapsed_seconds": run_elapsed_seconds(state),
        "gate": "required_checks" if state.get("status") == "waiting_checks" else None,
        "next_action": (
            operator_action["next_action"]
            if operator_action is not None
            else _next_action(state)
        ),
        "operator_action": operator_action,
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
        "abandonment": state.get("run_abandonment"),
        "agent_invocation": _public_invocation_view(active_invocation),
        "semantic_agent_attempt": (
            _history_attempt_view(semantic_attempt, include_history_facts=False)
            if isinstance(semantic_attempt, dict)
            else None
        ),
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
        "executor_control": state.get("_executor_control"),
    }
    progress_source = output
    if not as_json:
        progress_source = {
            **output,
            "next_action": human_next_action_for_state(state),
        }
    progress = status_progress_view(state, progress_source)
    output["progress"] = progress
    if as_json:
        output["lifecycle_action"] = state.get("action_application_receipt")
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    if _use_rich_status(plain):
        print_rich_status_progress(state, output, progress)
    else:
        print_status_progress(
            state,
            output,
            progress,
            display_term=_display_term,
            print_operator_action=lambda action: _print_human_operator_action(
                state, action
            ),
        )


def _print_history(
    state: dict[str, object],
    *,
    as_json: bool,
    plain: bool = False,
    details: bool = False,
) -> None:
    timeline = state.get("timeline", [])
    if not isinstance(timeline, list):
        raise ValueError("timeline must be an array")
    timeline_continuation = state.get("timeline_continuation", [])
    if not isinstance(timeline_continuation, list):
        raise ValueError("timeline_continuation must be an array")
    invocations = state.get("agent_invocation_history", [])
    if not isinstance(invocations, list):
        raise ValueError("agent_invocation_history must be an array")
    semantic_attempts = _semantic_attempt_history(state)
    human_semantic_attempts = _semantic_attempt_history(
        state, include_history_facts=True
    )
    operation_retries = _publication_operation_retries(state)
    resume_audit = state.get("resume_audit")
    public_resume_audit = resume_audit if isinstance(resume_audit, dict) else {}
    agent_resumes = public_resume_audit.get("history", [])
    if not isinstance(agent_resumes, list):
        raise ValueError("resume_audit.history must be an array")
    operator_action = _operator_action_view(state)
    output = {
        "run_id": state.get("run_id"),
        "timeline": timeline,
        "timeline_continuation": timeline_continuation,
        "next_action": (
            operator_action["next_action"]
            if operator_action is not None
            else _next_action(state)
        ),
        "operator_action": operator_action,
        "abandonment": state.get("run_abandonment"),
        "agent_invocations": [
            _public_invocation_view(invocation)
            if isinstance(invocation, dict)
            else invocation
            for invocation in invocations
        ],
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
        "executor_control": state.get("_executor_control"),
    }
    progress_source = output
    if not as_json:
        progress_source = {
            **output,
            "next_action": human_next_action_for_state(state),
            "semantic_agent_attempts": human_semantic_attempts,
        }
    progress = history_progress_view(state, progress_source)
    output.update(progress)
    if as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return
    if _use_rich_status(plain):
        print_rich_history_progress(state, progress_source, progress, details=details)
    else:
        print_history_progress(
            state,
            progress_source,
            progress,
            display_term=_display_term,
            print_operator_action=lambda action: _print_human_operator_action(
                state, action
            ),
            details=details,
        )


def _use_rich_status(plain: bool) -> bool:
    """Select decoration only when the output is an interactive color terminal."""

    if plain or "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(callable(isatty) and isatty())


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


def _semantic_attempt_history(
    state: dict[str, object], *, include_history_facts: bool = False
) -> list[dict[str, object]]:
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
            attempts.append(_history_attempt_view(attempt, include_history_facts))
    # The durable state keeps Attempts on their owning subject.  Accept the
    # compact top-level projection as a read-only legacy/test input as well;
    # this does not change the JSON contract emitted by history.
    projected = state.get("semantic_agent_attempts")
    if isinstance(projected, list):
        for attempt in projected:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id in seen:
                continue
            seen.add(attempt_id)
            attempts.append(_history_attempt_view(attempt, include_history_facts))
    return attempts


def _history_attempt_view(
    attempt: dict[str, object], include_history_facts: bool
) -> dict[str, object]:
    view = dict(attempt)
    if not include_history_facts:
        # These bounded facts are for the human history projection only.  The
        # machine-readable history keeps the existing Attempt shape.
        view.pop("development_summary", None)
        view.pop("publication", None)
        view.pop("budget_snapshot", None)
        view.pop("history_facts", None)
    return view


def _public_invocation_view(
    invocation: dict[str, object] | None,
) -> dict[str, object] | None:
    """Keep private bounded Attempt facts out of machine-facing CLI JSON."""

    if invocation is None:
        return None
    view = dict(invocation)
    semantic_attempt = view.get("semantic_attempt")
    if isinstance(semantic_attempt, dict):
        view["semantic_attempt"] = _history_attempt_view(
            semantic_attempt, include_history_facts=False
        )
    return view


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


def _next_action(state: dict[str, Any]) -> str:
    status = str(state.get("status"))
    run_id = state.get("run_id")
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    cleanup = state.get("delivery_cleanup")
    if status == "completed" and final_approval_cleanup_pending(state) and isinstance(run_id, str):
        return f"agent-run resume {run_id}"
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
    if has_final_approval(state) and isinstance(run_id, str) and status in {
        "run_approval_pending", "parent_approval_pending", "waiting_checks",
        "waiting_external", "parent_closeout_pending", "execution_failed", "supervision_timeout",
    }:
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
    if status == "operator_stopped" and isinstance(run_id, str):
        return f"agent-run resume {run_id}"
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
        "starting",
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


def _operator_action_view(state: dict[str, Any]) -> dict[str, Any] | None:
    return operator_action_view(
        state,
        current_identity=_current_delivery_identity(state),
        fallback_next_action=_next_action(state),
    )


def human_delivery_status(value: object) -> object:
    """Render the same delivery terminology used by status and history."""

    return _display_term(value)


def human_next_action_for_state(state: Mapping[str, Any]) -> object:
    """Project an ordinary recovery command through the Parent selector."""

    public_state = state if isinstance(state, dict) else dict(state)
    operator_action = _operator_action_view(public_state)
    value: object = _next_action(public_state)
    if isinstance(operator_action, Mapping):
        next_action = operator_action.get("next_action")
        if isinstance(next_action, str):
            value = next_action
    if not isinstance(value, str):
        return value
    repository = state.get("repository")
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    run_id = state.get("run_id")
    if (
        isinstance(repository, str)
        and isinstance(parent_number, int)
        and isinstance(run_id, str)
        and f"agent-run abandon {run_id} --discard-worktree" in value
    ):
        return value.replace(
            f"agent-run run {parent_number}",
            f"agent-run run {parent_number} --repo {repository}",
        ).replace(
            f"agent-run abandon {run_id} --discard-worktree",
            f"agent-run abandon {parent_number} --repo {repository} --discard-worktree",
        )
    parts = value.split()
    if (
        len(parts) == 3
        and parts[0] == "agent-run"
        and parts[1] in {"run", "resume", "approve", "requeue", "abandon"}
        and isinstance(repository, str)
        and isinstance(parent_number, int)
    ):
        return f"agent-run {parts[1]} {parent_number} --repo {repository}"
    return human_next_action(value, run_id=run_id)


def _print_human_operator_action(
    state: Mapping[str, Any], action: dict[str, Any]
) -> None:
    human_action = dict(action)
    human_action["next_action"] = human_next_action_for_state(state)
    _print_operator_action(human_action)


def _current_worker(state: dict[str, object]) -> dict[str, object] | None:
    current = current_work_subject(state)
    if current is None:
        return None
    location, subject = current
    if location == "run_publication" and subject.get("phase") == "publishing":
        return {
            "role": "运行发布工作代理",
            "attempt": subject.get("publication_attempts"),
            "thread_id": subject.get("thread_id"),
            "phase": subject.get("phase"),
        }
    if location == "run_repair":
        return _worker_from_job(subject, run_repair=True)
    if location == "run_acceptance" and subject.get("phase") == "reviewing":
        reviewer_ids = subject.get("reviewer_thread_ids")
        thread_id = (
            reviewer_ids[-1]
            if isinstance(reviewer_ids, list) and reviewer_ids
            else subject.get("development_thread_id")
        )
        return {
            "role": "运行验收工作代理",
            "attempt": subject.get("validation_attempts"),
            "thread_id": thread_id,
            "phase": subject.get("phase"),
        }
    if location.startswith("ticket:") or location == "parent":
        return _worker_from_job(subject)
    return None


def _current_phase(state: dict[str, object]) -> object:
    current = current_work_subject(state)
    if current is not None:
        return current[1].get("phase")
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
    policy = run_repair_budget_policy_for_job(
        job, state_snapshot=state.get("policy_snapshot")
    )
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
        "code_modification_limit": policy.development_limit,
        "validation_attempts": cycle.get("validation_attempts", 0),
        "candidate_sha": job.get("candidate_sha"),
        "development_thread_id": cycle.get("development_thread_id"),
        "worktree": cycle.get("worktree"),
    }


def _public_review_budget(state: dict[str, object]) -> dict[str, object] | None:
    """Expose the active subject's bounded window without leaking policy logic."""

    current = current_work_subject(state)
    if current is None:
        return None
    location, subject = current
    if location.startswith("ticket:"):
        policy = ticket_budget_policy_for_job(
            subject, state_snapshot=state.get("policy_snapshot")
        )
    elif location == "parent":
        policy = parent_only_budget_policy_for_job(
            subject, state_snapshot=state.get("policy_snapshot")
        )
    else:
        if location == "run_publication":
            acceptance = state.get("run_acceptance")
            if not isinstance(acceptance, dict):
                return None
            subject = acceptance
        policy = run_repair_budget_policy_for_job(
            subject, state_snapshot=state.get("policy_snapshot")
        )
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


def _display_term(value: object) -> object:
    if not isinstance(value, str):
        return value
    return {
        "active": "进行中",
        "starting": "正在启动",
        "ticket_completed": "任务已完成",
        "waiting_checks": "等待自动检查",
        "waiting_merge": "等待合并确认",
        "waiting_external": "等待外部系统收敛",
        "github_convergence": "GitHub 状态收敛",
        "required_checks": "自动检查",
        "publication_pending": "等待发布",
        "parent_delivery_pending": "等待父项交付",
        "parent_approval_pending": "等待父项人工批准",
        "parent_closeout_pending": "等待父项收口",
        "run_acceptance_pending": "等待运行整体验收",
        "run_publication_pending": "等待运行发布",
        "run_approval_pending": "等待人工批准",
        "requeue_required": "需要重新排队",
        "ready_for_human": "等待人工处理",
        "unsupported_scope_change": "不支持的范围变化",
        "deterministic_contradiction": "确定性矛盾，需人工处理",
        "abandonment_pending": "等待放弃恢复",
        "progress_exhausted": "无可推进任务",
        "execution_failed": "执行失败，可恢复",
        "operator_stopped": "操作者已停止，可恢复",
        "supervision_timeout": "监督超时暂停，可恢复",
        "pending": "待处理",
        "blocked": "已阻塞",
        "incompatible_run_state": "状态协议不兼容",
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
