"""Bind program-side credentials from local Run facts before remote access."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from agent_run.git import GitRepository
from agent_run.github import GhGitHubReader
from agent_run.github_identity import (
    identity_overrides, resolve_runner_identity, validate_identity_snapshot,
)
from agent_run.messages import error_message
from agent_run.state import StateStore
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from agent_run.task_control import TaskControlError, TaskControlStore, TaskKey
from agent_run.user_defaults import UserDefaultsStore


def frozen_runner_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    if "runner_github_identity" not in state:
        raise IncompatibleRunStateError(error_message("identity.run_missing"))
    identity = validate_identity_snapshot(state["runner_github_identity"])
    if identity["source"] not in {"host", "pat"}:
        raise IncompatibleRunStateError(error_message("identity.run_missing"))
    return identity


def bind_runner_identity(
    parsed: argparse.Namespace, states: StateStore, git: GitRepository,
    github: Any, initial: dict[str, Any] | None,
) -> None:
    """Choose from local Run/Action facts before making any remote request."""
    current = None
    pending = None
    if parsed.command == "run":
        repository = parsed.repo or github.repository_hint()
        if not repository:
            raise TaskControlError("Cannot identify the selected repository")
        task = TaskKey(git.root, repository, parsed.parent)
        unfinished = states.find_unfinished_runs(task.repository, task.parent_number)
        if len(unfinished) > 1:
            raise TaskControlError("Multiple unfinished Delivery Runs exist for this Parent Issue")
        current = unfinished[0] if unfinished else None
        if current is not None:
            require_current_run_state(current)
        if current is None:
            try:
                record = TaskControlStore(states.root).load(task)
            except TaskControlError:
                record = None
            action = record.get("action") if isinstance(record, dict) else None
            if isinstance(action, dict) and action.get("status") in {"accepted", "applying"}:
                payload = action.get("payload")
                bound_run_id = action.get("run_id")
                if bound_run_id is None and isinstance(payload, dict):
                    bound_run_id = payload.get("run_id")
                if isinstance(bound_run_id, str):
                    # A completed Run can still have an applying approval.
                    # Its mutation payload is not a new Run configuration.
                    current = states.load_current_run(bound_run_id)
                    if current is None:
                        raise TaskControlError("Lifecycle Action bound Run is missing")
                elif action.get("kind") == "run":
                    pending = payload
                else:
                    raise TaskControlError("Lifecycle Action is missing its Run binding")
    elif isinstance(getattr(parsed, "run_id", None), str):
        current = states.load_current_run(parsed.run_id)
    if current is not None:
        identity = frozen_runner_identity(current)
    elif isinstance(pending, dict):
        identity = frozen_runner_identity(pending)
    else:
        identity = initial or resolve_runner_identity(
            UserDefaultsStore().load().get("github"), identity_overrides(parsed),
        )
    if parsed.command == "run" and (current is not None or pending is not None):
        selected = resolve_runner_identity(
            {"runner_source": identity["source"],
             **({"pat": identity["pat"]} if "pat" in identity else {})},
            identity_overrides(parsed),
        )
        if selected != identity:
            raise TaskControlError(error_message("identity.run_fixed"))
    git.runner_identity = identity
    if isinstance(github, GhGitHubReader):
        github.runner_identity = identity

