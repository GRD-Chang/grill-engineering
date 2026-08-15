from __future__ import annotations

import pytest

from agent_run.external_supervision import (
    CHECKS_BUDGET_SECONDS,
    ExternalSupervisor,
    is_github_convergence_error,
    restore_supervision_wait,
    wait_for_github_convergence,
    waiting_boundary,
)


def test_pending_required_checks_wait_with_a_bounded_fake_clock() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=advance, poll_interval_seconds=60
    )
    state: dict[str, object] = {"status": "waiting_checks"}

    assert waiting_boundary(state) is not None
    assert supervisor.before_retry(state)
    assert sleeps == [60]

    now[0] = CHECKS_BUDGET_SECONDS
    assert not supervisor.before_retry(state)
    assert state["status"] == "supervision_timeout"
    wait = state["supervision_wait"]  # type: ignore[index]
    assert wait["resume_status"] == "waiting_checks"  # type: ignore[index]
    diagnostic = state["diagnostics"][0]  # type: ignore[index]
    assert diagnostic["waiting_for"] == "GitHub 外部状态 的 GitHub Required Checks"  # type: ignore[index]
    assert diagnostic["budget_seconds"] == CHECKS_BUDGET_SECONDS  # type: ignore[index]


def test_only_external_wait_states_are_supervised() -> None:
    assert waiting_boundary({"status": "active"}) is None
    assert waiting_boundary({"status": "execution_failed"}) is None


def test_each_waiting_object_gets_its_own_budget_window() -> None:
    now = [0.0]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    first: dict[str, object] = {
        "status": "waiting_checks",
        "active_ticket_job": {"pr_number": 11},
    }
    second: dict[str, object] = {
        "status": "waiting_checks",
        "active_ticket_job": {"pr_number": 12},
    }

    assert supervisor.before_retry(first)
    now[0] = CHECKS_BUDGET_SECONDS
    assert supervisor.before_retry(second)


def test_restore_supervision_wait_resets_the_persisted_window() -> None:
    state: dict[str, object] = {
        "status": "supervision_timeout",
        "terminal_kind": "supervision_timeout",
        "diagnostics": [{"code": "supervision_timeout"}],
        "supervision_wait": {"resume_status": "waiting_external"},
    }

    restore_supervision_wait(state)

    assert state["status"] == "waiting_external"
    assert state["terminal_kind"] == "waiting_external"
    assert state["diagnostics"] == []
    assert "supervision_wait" not in state


def test_supervision_timeout_preserves_bounded_last_read_error() -> None:
    now = [0.0]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    state: dict[str, object] = {}
    wait_for_github_convergence(
        state,
        code="github_read_failed",
        message="authorization: Bearer ghp_secret " + "x" * 9_000,
        waiting_for="GitHub repository binding",
    )

    assert supervisor.before_retry(state)
    now[0] = 10 * 60
    assert not supervisor.before_retry(state)

    wait = state["supervision_wait"]  # type: ignore[index]
    assert "last_error" not in wait  # type: ignore[operator]
    diagnostic = state["diagnostics"][0]  # type: ignore[index]
    error = diagnostic["last_error"]  # type: ignore[index]
    assert error["code"] == "github_read_failed"  # type: ignore[index]
    assert "ghp_secret" not in error["message"]  # type: ignore[index]
    assert len(error["message"].encode("utf-8")) <= 8 * 1024  # type: ignore[index]


@pytest.mark.parametrize(
    "code",
    [
        "github_read_failed",
        "github_invalid_response",
        "missing_pull_request",
        "ticket_close_ownership_pending",
    ],
)
def test_unproven_github_read_failures_remain_reconcilable(code: str) -> None:
    assert is_github_convergence_error(code)


@pytest.mark.parametrize(
    "code",
    [
        "ambiguous_run_pr",
        "invalid_parent",
        "stale_run_pr",
    ],
)
def test_proven_github_state_contradictions_do_not_enter_supervision(code: str) -> None:
    assert not is_github_convergence_error(code)
