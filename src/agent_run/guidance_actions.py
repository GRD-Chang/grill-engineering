"""Shared recovery decisions; rendering never determines the selected action."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_run.final_approval_operation import final_approval_cleanup_pending, has_final_approval
from agent_run.delivery_facts import current_work_subject, ticket_phase_execution_activity
from agent_run.execution_timing import invocation_activity
from agent_run.messages import text
from agent_run.semantic_attempt import invocation_is_explicitly_resumable
from agent_run.state_contract import human_blocker_subject_count


@dataclass(frozen=True)
class NextAction:
    kind: str
    command: str | None = None
    selector: object = None
    ticket_number: int | None = None


def action_command(state: dict[str, Any], verb: str, selector: object, *, human: bool) -> str:
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, dict) else None
    repository = state.get("repository")
    if human and isinstance(repository, str) and type(number) is int:
        return f"agent-run {verb} {number} --repo {repository}"
    if human and selector == state.get("run_id") and isinstance(selector, str):
        selector = "<run-id>"
    suffix = f" {selector}" if selector is not None else ""
    return f"agent-run {verb}{suffix}"


def render_next_action(
    action: NextAction, state: dict[str, Any], *, language: str = "zh", human: bool = False,
) -> str:
    if action.kind == "command":
        assert action.command is not None
        command = action_command(state, action.command, action.selector, human=human)
        return command if action.ticket_number is None else f"{command} --ticket {action.ticket_number}"
    parent = state.get("parent")
    number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    key = f"guidance.action.{action.kind}"
    if human and action.kind in {"stale_checkout", "scope_change", "contradiction", "publication_retry"}:
        key += ".human"
    return text(
        key, language=language,
        run=action_command(state, "run", number, human=human),
        abandon=action_command(state, "abandon", None, human=human),
        discard=action_command(state, "abandon", state.get("run_id"), human=human) + " --discard-worktree",
    )


def next_action_fact(state: dict[str, Any]) -> NextAction:
    status = str(state.get("status"))
    run_id = state.get("run_id")
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    cleanup = state.get("delivery_cleanup")
    if status == "completed" and final_approval_cleanup_pending(state) and isinstance(run_id, str):
        return NextAction("command", "resume", run_id)
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
            return NextAction("stale_checkout")
        return NextAction("command", "resume", run_id)
    if has_final_approval(state) and isinstance(run_id, str) and status in {
        "run_approval_pending", "parent_approval_pending", "waiting_checks",
        "waiting_external", "parent_closeout_pending", "execution_failed", "supervision_timeout",
    }:
        return NextAction("command", "resume", run_id)
    if status in {"run_approval_pending", "parent_approval_pending"} and isinstance(
        run_id, str
    ):
        return NextAction("command", "approve", run_id)
    if status == "unsupported_scope_change":
        return NextAction("scope_change")
    if status == "deterministic_contradiction":
        return NextAction("contradiction")
    if status == "abandonment_pending" and isinstance(run_id, str):
        return NextAction("command", "abandon", run_id)
    if status == "operator_stopped" and isinstance(run_id, str):
        return NextAction("command", "resume", run_id)
    if status == "requeue_required" and isinstance(run_id, str):
        return NextAction("command", "requeue", run_id)
    if invocation_is_explicitly_resumable(state) and isinstance(run_id, str):
        return NextAction("command", "resume", run_id)
    if (
        status == "waiting_external"
        and isinstance(state.get("requeue_transition"), dict)
    ):
        return NextAction("command", "run", parent_number)
    if status == "waiting_external":
        return NextAction("command", "run", parent_number)
    if status == "supervision_timeout" and isinstance(run_id, str):
        return NextAction("command", "resume", run_id)
    current = current_work_subject(state)
    pauses = [
        job for job in state.get("ticket_jobs", {}).values()
        if isinstance(job, dict) and job.get("blocked_reason") in {
            "ticket_execution_failed", "ticket_supervision_timeout", "ticket_paused",
        }
    ]
    pause = current[1] if current and current[1] in pauses else pauses[0] if len(pauses) == 1 else None
    if status in {"blocked", "progress_exhausted"} and pause is not None and isinstance(run_id, str):
        return NextAction("command", "resume", run_id, int(pause["ticket_number"]))
    execution_audit = {"executor_control": state.get("_executor_control")}
    if (
        isinstance(run_id, str)
        and ticket_phase_execution_activity(
            state, execution_audit,
            invocation_activity(state.get("active_agent_invocation"), execution_audit),
        ) == "interrupted"
    ):
        return NextAction("command", "resume", run_id)
    if (
        status in {"blocked", "ready_for_human"}
        and isinstance(run_id, str)
        and current is not None
        and current[1].get("blocked_reason") in {
            "ci_inner_budget_exhausted", "ci_outer_budget_exhausted"
        }
    ):
        return NextAction("command", "resume", run_id)
    if (
        status in {"ready_for_human", "progress_exhausted"}
        and human_blocker_subject_count(state) == 1
        and isinstance(run_id, str)
    ):
        return NextAction("command", "resume", run_id)
    if status in {"ready_for_human", "progress_exhausted", "blocked"}:
        return NextAction("human_items")
    if status == "publication_pending":
        return NextAction("publication_retry")
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
        return NextAction("command", "run", parent_number)
    return NextAction("none")
