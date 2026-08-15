"""Recovery helpers for GitHub reads around a durable Requeue transition."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from agent_run.external_supervision import (
    is_github_convergence_error,
    wait_for_github_refresh,
)
from agent_run.graph import state_from_graph
from agent_run.github import GitHubReadError
from agent_run.models import DeliveryGraph
from agent_run.scope_changes import reconcile_structure


class DeliveryGraphReader(Protocol):
    def delivery_graph(self, parent_number: int) -> DeliveryGraph: ...


def wait_for_recoverable_github_read(
    state: dict[str, Any],
    error: GitHubReadError,
    *,
    waiting_for: str,
    now: Callable[[], str],
) -> dict[str, Any]:
    """Persist a wait for an unknown GitHub read, or re-raise a contradiction."""

    if not is_github_convergence_error(error.code):
        raise error
    wait_for_github_refresh(
        state,
        code=error.code,
        message=error.message,
        waiting_for=waiting_for,
    )
    state["updated_at"] = now()
    return state


def refresh_requeue_transition_facts(
    state: dict[str, Any],
    parent_number: int,
    github: DeliveryGraphReader,
    *,
    now: Callable[[], str],
) -> dict[str, Any]:
    """Refresh graph facts before retrying a durable old-PR retirement."""

    try:
        graph = github.delivery_graph(parent_number)
        projected = state_from_graph(state, graph)
        refreshed = reconcile_structure(state, projected)
        if refreshed.get("status") == "unsupported_scope_change":
            return refreshed
        state["parent"] = _mapping(refreshed, "parent")
        state["ticket_graph"] = _mapping(refreshed, "ticket_graph")
        if state.pop("github_refresh_pending", None):
            state.update(
                {
                    "status": "requeue_required",
                    "terminal_kind": "requeue_required",
                    "diagnostics": [],
                }
            )
        state["updated_at"] = now()
        return state
    except GitHubReadError as error:
        failed = dict(state)
        if is_github_convergence_error(error.code):
            return wait_for_recoverable_github_read(
                failed,
                error,
                waiting_for="GitHub requeue transition refresh",
                now=now,
            )
        failed.update(
            {
                "status": "blocked",
                "terminal_kind": "waiting_human",
                "diagnostics": [{"code": error.code, "message": error.message}],
                "updated_at": now(),
            }
        )
        return failed


def _mapping(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value
