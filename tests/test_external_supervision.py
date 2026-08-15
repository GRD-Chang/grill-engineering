from __future__ import annotations

from agent_run.external_supervision import (
    CHECKS_BUDGET_SECONDS,
    ExternalSupervisor,
    restore_supervision_wait,
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
