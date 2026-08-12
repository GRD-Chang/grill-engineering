from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

from agent_run.controller import Controller
from agent_run.controller import _human_blocker_subject_count
from agent_run.state import StateStore
from agent_run.cli_presentation import _print_precondition_failure

def _run_to_human_gate(
    parsed: argparse.Namespace, states: StateStore, controller: Controller
) -> tuple[dict[str, Any], bool]:
    arguments = _nested_arguments(parsed)
    state, resumed = controller.start_or_resume_unfinished(parsed.parent)
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("Delivery Run is missing its Run ID")
    state = _load_local_run(states, run_id)
    previous_marker: tuple[object, ...] | None = None
    while True:
        command = _next_automatic_command(state)
        if command is None:
            return state, resumed
        marker = _progress_marker(state, command)
        if marker == previous_marker:
            return state, resumed
        previous_marker = marker
        print(f"推进: {state['status']} → {command}", file=sys.stderr)
        agent_arguments = _agent_fixture_arguments(parsed, command)
        _invoke_nested(command, run_id, *arguments, *agent_arguments)
        state = _load_local_run(states, run_id)
        if command == "requeue":
            # `requeue` itself enters the replacement Job Loop. Returning
            # here enforces the one automatic replacement budget for this
            # top-level `run` command.
            return state, resumed
def _nested_arguments(parsed: argparse.Namespace) -> list[str]:
    arguments: list[str] = []
    if parsed.repo:
        arguments.extend(["--repo", parsed.repo])
    if parsed.state_dir:
        arguments.extend(["--state-dir", parsed.state_dir])
    if parsed.github_fixture:
        arguments.extend(["--github-fixture", parsed.github_fixture])
    crash_after_save = getattr(parsed, "crash_after_save", None)
    if isinstance(crash_after_save, int):
        arguments.extend(["--crash-after-save", str(crash_after_save)])
    return arguments


def _agent_fixture_arguments(
    parsed: argparse.Namespace, command: str
) -> list[str]:
    agent_fixture = getattr(parsed, "agent_fixture", None)
    if agent_fixture and command in {
        "resume",
        "deliver",
        "accept-run",
        "publish-run",
        "requeue",
    }:
        return ["--agent-fixture", agent_fixture]
    return []


def _invoke_nested(command: str, identifier: str, *arguments: str) -> dict[str, object]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        from agent_run.cli import main

        exit_code = main([command, identifier, *arguments])
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{command} did not return a Delivery Run result")
    try:
        result: object = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise ValueError(f"{command} returned invalid Delivery Run JSON") from error
    if not isinstance(result, dict):
        raise ValueError(f"{command} returned invalid Delivery Run result")
    if exit_code != 0 and result.get("status") not in {
        "waiting_checks",
        "ready_for_human",
        "unsupported_scope_change",
        "abandonment_pending",
        "progress_exhausted",
        "execution_failed",
        "blocked",
        "waiting_merge",
        "requeue_required",
    }:
        raise ValueError(f"{command} failed without a recoverable Run state")
    return result


def _next_automatic_command(state: dict[str, Any]) -> str | None:
    status = str(state.get("status"))
    if status in {
        "active",
        "ticket_completed",
        "parent_delivery_pending",
        "waiting_merge",
    }:
        return "deliver"
    if status == "run_acceptance_pending":
        return "accept-run"
    publication = state.get("run_publication")
    if status == "run_publication_pending" or (
        status in {"publication_pending", "waiting_checks"}
        and isinstance(publication, dict)
        and publication.get("phase") in {
            "publication_pending",
            "waiting_checks",
            "ready_for_approval",
        }
    ):
        return "publish-run"
    if status in {"publication_pending", "waiting_checks"}:
        return "deliver"
    return None


def _is_lifecycle_action(command: str) -> bool:
    return command in {
        "deliver",
        "accept-run",
        "publish-run",
        "approve",
        "revise",
        "requeue",
    }


def _resume_is_ready(state: dict[str, object]) -> bool:
    """Whether `resume` has a current failed or Human Blocker Invocation."""

    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, dict) and invocation.get("status") == "failed":
        return True
    return _human_blocker_subject_count(state) == 1


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
    if status in {"unsupported_scope_change", "abandonment_pending"}:
        return False
    if command == "requeue":
        return status == "requeue_required"
    if status == "requeue_required":
        return False
    if command == "deliver":
        return True
    if command == "accept-run":
        return status in {"run_acceptance_pending", "run_publication_pending"}
    if command == "publish-run":
        publication = state.get("run_publication")
        return status == "run_publication_pending" or (
            isinstance(publication, dict)
            and status
            in {"publication_pending", "waiting_checks", "run_approval_pending"}
        )
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
    publication_phase = publication.get("phase") if isinstance(publication, dict) else None
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
        publication.get("publication_attempts") if isinstance(publication, dict) else None,
        publication.get("pr_number") if isinstance(publication, dict) else None,
    )


def _load_local_run(states: StateStore, run_id: str) -> dict[str, object]:
    state = states.load_run(run_id)
    if state is None:
        raise ValueError(f"unknown Delivery Run: {run_id}")
    return state
