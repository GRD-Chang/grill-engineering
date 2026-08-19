from __future__ import annotations

import argparse
from typing import Any

from agent_run.controller import Controller
from agent_run.state import StateStore
from agent_run.state_contract import (
    human_blocker_subject_count,
    require_current_run_state,
)
from agent_run.cli_presentation import _print_precondition_failure


def _run_to_human_gate(
    parsed: argparse.Namespace,
    states: StateStore,
    controller: Controller,
    driver: Any,
) -> tuple[dict[str, Any], bool]:
    state, resumed = controller.start_or_resume_unfinished(parsed.parent)
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("Delivery Run is missing its Run ID")
    state = _load_local_run(states, run_id)
    return driver.advance(state), resumed


def _is_lifecycle_action(command: str) -> bool:
    return command in {
        "approve",
        "revise",
        "requeue",
    }


def _resume_is_ready(state: dict[str, object]) -> bool:
    """Whether `resume` has a supported, bounded recovery boundary."""

    if state.get("status") == "supervision_timeout":
        wait = state.get("supervision_wait")
        return isinstance(wait, dict) and wait.get("resume_status") in {
            "waiting_checks",
            "waiting_merge",
            "waiting_external",
        }

    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, dict) and invocation.get("status") == "failed":
        return True
    return (
        human_blocker_subject_count(state) == 1
    )


def _command_is_ready(state: dict[str, object], command: str) -> bool:
    status = state.get("status")
    if status == "abandoned":
        return True
    if status == "execution_failed":
        invocation = state.get("active_agent_invocation")
        # A failed Agent Invocation has exactly one recovery path: `resume`.
        # Deterministic publisher/check reconciliation retains its historical
        # lifecycle command recovery path.
        return not (
            isinstance(invocation, dict) and invocation.get("status") == "failed"
        )
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
