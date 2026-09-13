from __future__ import annotations

import argparse
from typing import Any, Callable

from agent_run.final_approval_operation import (
    final_approval_cleanup_pending,
    has_final_approval,
)
from agent_run.controller import Controller
from agent_run.state import StateStore
from agent_run.state_contract import (
    human_blocker_subject_count,
    require_current_run_state,
)
from agent_run.cli_presentation import _print_precondition_failure
from agent_run.operator_gate import has_non_invocation_execution_failure
from agent_run.review_budget import budget_checkpoint_subjects
from agent_run.semantic_attempt import (
    invocation_is_explicitly_resumable,
)


def _run_to_human_gate(
    parsed: argparse.Namespace,
    states: StateStore,
    controller: Controller,
    driver: Any,
    initialize_profile: Callable[[dict[str, Any], bool], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    state, resumed = controller.start_or_resume_unfinished(parsed.parent)
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("Delivery Run is missing its Run ID")
    state = _load_local_run(states, run_id)
    if initialize_profile is not None:
        initialize_profile(state, resumed)
    return driver.advance(state), resumed


def _is_lifecycle_action(command: str) -> bool:
    return command in {
        "approve",
        "revise",
        "requeue",
    }


def _resume_is_ready(state: dict[str, object]) -> bool:
    """Whether `resume` has a supported, bounded recovery boundary."""

    if _resume_is_publication_recovery(state):
        return True

    status = state.get("status")
    if status == "operator_stopped":
        return isinstance(state.get("operator_stop"), dict)
    if status == "supervision_timeout":
        wait = state.get("supervision_wait")
        return isinstance(wait, dict) and wait.get("resume_status") in {
            "waiting_checks",
            "waiting_merge",
            "waiting_external",
        }

    if invocation_is_explicitly_resumable(state):
        return True
    if has_non_invocation_execution_failure(state):
        return True
    return (
        human_blocker_subject_count(state) == 1
        or _review_budget_checkpoint_count(state) == 1
    )


def _resume_is_publication_recovery(state: dict[str, object]) -> bool:
    """Recognize a durable merge/closeout intent that only needs reconciliation."""

    status = state.get("status")
    if has_final_approval(state) and (
        status in {"waiting_checks", "waiting_external", "run_approval_pending",
                   "parent_approval_pending", "execution_failed", "supervision_timeout"}
        or (status == "completed" and final_approval_cleanup_pending(state))
    ):
        return True
    if status == "parent_closeout_pending":
        return True
    parent_job = state.get("parent_job")
    if (
        state.get("delivery_type") == "parent_only"
        and status == "parent_delivery_pending"
        and isinstance(parent_job, dict)
        and parent_job.get("phase") == "merging"
    ):
        # The merge/closeout intent is already durable.  Resume may only
        # reconcile that exact publication generation; it does not create a
        # new delivery intent.
        return True
    return False


def _review_budget_checkpoint_count(state: dict[str, object]) -> int:
    return len(budget_checkpoint_subjects(state))


def _command_is_ready(state: dict[str, object], command: str) -> bool:
    status = state.get("status")
    if status == "abandoned":
        return True
    if status == "execution_failed":
        return False
    if status in {
        "unsupported_scope_change",
        "deterministic_contradiction",
        "abandonment_pending",
    }:
        return False
    if command == "requeue":
        return status == "requeue_required"
    if status == "requeue_required":
        return False
    if command == "approve":
        return (
            state.get("delivery_type") == "parent_only"
            and status in {"parent_approval_pending", "parent_closeout_pending"}
        ) or status == "run_approval_pending"
    if command == "revise":
        return status in {"ready_for_human", "run_approval_pending"}
    return False


def _progress_marker(state: dict[str, Any], command: str) -> tuple[object, ...]:
    active = state.get("active_ticket_job")
    active_phase = active.get("phase") if isinstance(active, dict) else None
    acceptance = state.get("run_acceptance")
    acceptance_phase = acceptance.get("phase") if isinstance(acceptance, dict) else None
    publication = state.get("run_publication")
    publication_phase = (
        publication.get("phase") if isinstance(publication, dict) else None
    )
    return (
        command,
        state.get("status"),
        active_phase,
        active.get("modification_attempts") if isinstance(active, dict) else None,
        active.get("validation_attempts") if isinstance(active, dict) else None,
        active.get("publication_attempts") if isinstance(active, dict) else None,
        active.get("pull_number") if isinstance(active, dict) else None,
        acceptance_phase,
        acceptance.get("validation_attempts") if isinstance(acceptance, dict) else None,
        publication_phase,
        (
            publication.get("publication_attempts")
            if isinstance(publication, dict)
            else None
        ),
        publication.get("pr_number") if isinstance(publication, dict) else None,
    )


def _load_local_run(states: StateStore, run_id: str) -> dict[str, object]:
    state = states.load_current_run(run_id)
    if state is None:
        raise ValueError(f"unknown Delivery Run: {run_id}")
    return state
