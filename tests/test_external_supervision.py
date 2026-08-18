from __future__ import annotations

import pytest

from agent_run.external_supervision import (
    CHECKS_BUDGET_SECONDS,
    ExternalSupervisor,
    ensure_supervision_window,
    is_github_convergence_error,
    public_supervision_snapshot,
    restore_supervision_wait,
    wait_for_github_convergence,
    wait_for_github_refresh,
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


@pytest.mark.parametrize(
    ("job_key", "job"),
    [
        ("active_ticket_job", {"pr_number": 11, "phase": "waiting_checks"}),
        ("parent_job", {"pr_number": 12, "phase": "waiting_checks"}),
        ("run_publication", {"pr_number": 13, "phase": "waiting_checks"}),
    ],
)
def test_observed_wait_window_survives_a_new_supervisor_until_explicit_resume(
    job_key: str, job: dict[str, object]
) -> None:
    now = [0.0]
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "waiting_checks",
        job_key: job,
    }
    first = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )

    window = first.observe(state)

    assert window is not None
    assert window["started_at"] == 0
    assert window["deadline"] == CHECKS_BUDGET_SECONDS

    now[0] = CHECKS_BUDGET_SECONDS
    restarted = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    assert not restarted.before_retry(state)
    assert state["supervision_wait"]["deadline"] == CHECKS_BUDGET_SECONDS  # type: ignore[index]

    restore_supervision_wait(state)
    assert restarted.observe(state)["started_at"] == CHECKS_BUDGET_SECONDS  # type: ignore[index]


@pytest.mark.parametrize("job_key", ["parent_job", "run_publication"])
def test_changed_parent_or_final_pr_facts_open_a_new_window(job_key: str) -> None:
    now = [0.0]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "waiting_checks",
        job_key: {
            "pr_number": 11,
            "phase": "waiting_checks",
            "head_sha": "head-a",
            "base_sha": "base-a",
        },
    }

    first = supervisor.observe(state)
    assert first is not None
    now[0] = 120
    changed = state[job_key]
    assert isinstance(changed, dict)
    changed["head_sha"] = "head-b"

    replacement = supervisor.observe(state)

    assert replacement is not None
    assert replacement["identity"] != first["identity"]
    assert replacement["started_at"] == 120


@pytest.mark.parametrize(
    ("job_key", "changed_path"),
    [
        ("parent_job", ("pr_number",)),
        ("parent_job", ("parent_branch",)),
        ("parent_job", ("head_sha",)),
        ("parent_job", ("base_sha",)),
        ("parent_job", ("acceptance_record", "effective_revision")),
        ("run_publication", ("pr_number",)),
        ("run_publication", ("head_sha",)),
        ("run_publication", ("base_sha",)),
        ("run_publication", ("record", "run_head_sha")),
        ("run_publication", ("record", "default_head_sha")),
        ("run_publication", ("record", "parent_revision")),
    ],
)
def test_changed_parent_or_final_review_boundary_opens_a_new_window(
    job_key: str, changed_path: tuple[str, ...]
) -> None:
    now = [0.0]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    state: dict[str, object] = {
        "run_id": "run-1",
        "run_branch": "agent-run/run-1/run",
        "status": "waiting_checks",
        job_key: {
            "pr_number": 11,
            "phase": "waiting_checks",
            "parent_branch": "agent-run/run-1/parent",
            "head_sha": "head-a",
            "base_sha": "base-a",
            "acceptance_record": {"effective_revision": "parent-revision-a"},
            "record": {
                "run_head_sha": "run-head-a",
                "default_head_sha": "default-head-a",
                "parent_revision": "parent-revision-a",
            },
        },
    }

    first = supervisor.observe(state)
    assert first is not None
    now[0] = 120
    boundary: object = state[job_key]
    for key in changed_path[:-1]:
        assert isinstance(boundary, dict)
        boundary = boundary[key]
    assert isinstance(boundary, dict)
    boundary[changed_path[-1]] = "changed"

    replacement = supervisor.observe(state)

    assert replacement is not None
    assert replacement["identity"] != first["identity"]
    assert replacement["started_at"] == 120


@pytest.mark.parametrize(
    ("job_key", "changed_path", "replacement_value"),
    [
        ("parent_job", ("base", "branch"), "release"),
        ("parent_job", ("base", "sha"), "base-b"),
        ("run_publication", ("run_branch",), "agent-run/run-1/replacement"),
        ("run_publication", ("base", "branch"), "release"),
        ("run_publication", ("base", "sha"), "base-b"),
        (
            "run_publication",
            ("run_publication", "record", "ticket_completion_records"),
            [{"ticket_number": 3, "integrated_sha": "changed"}],
        ),
    ],
)
def test_changed_root_or_completion_review_facts_open_a_new_window(
    job_key: str, changed_path: tuple[str, ...], replacement_value: object
) -> None:
    now = [0.0]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda _seconds: None, poll_interval_seconds=60
    )
    state: dict[str, object] = {
        "run_id": "run-1",
        "run_branch": "agent-run/run-1/run",
        "base": {"branch": "main", "sha": "base-a"},
        "status": "waiting_checks",
        job_key: {
            "pr_number": 11,
            "phase": "waiting_checks",
            "record": {
                "ticket_completion_records": [
                    {"ticket_number": 3, "integrated_sha": "integrated-a"}
                ]
            },
        },
    }

    first = supervisor.observe(state)
    assert first is not None
    now[0] = 120
    boundary: object = state
    for key in changed_path[:-1]:
        assert isinstance(boundary, dict)
        boundary = boundary[key]
    assert isinstance(boundary, dict)
    boundary[changed_path[-1]] = replacement_value

    replacement = supervisor.observe(state)

    assert replacement is not None
    assert replacement["identity"] != first["identity"]
    assert replacement["started_at"] == 120


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


def test_timed_out_snapshot_reports_no_remaining_time_from_persisted_state() -> None:
    state: dict[str, object] = {
        "status": "supervision_timeout",
        "supervision_wait": {
            "deadline": CHECKS_BUDGET_SECONDS,
            "elapsed_seconds": CHECKS_BUDGET_SECONDS,
            "budget_seconds": CHECKS_BUDGET_SECONDS,
        },
    }

    snapshot = public_supervision_snapshot(state, now=204)

    assert snapshot is not None
    assert snapshot["remaining_seconds"] == 0


def test_direct_wait_persists_a_window_before_foreground_supervision() -> None:
    state: dict[str, object] = {
        "status": "waiting_checks",
        "parent": {"number": 1},
        "parent_job": {"pr_number": 1, "base_sha": "base-a"},
        "base": {"sha": "base-a"},
    }

    window = ensure_supervision_window(state, now=lambda: 100.0)
    snapshot = public_supervision_snapshot(state, now=101.0)

    assert window is not None
    assert snapshot is not None
    assert snapshot["kind"] == "required_checks"
    assert snapshot["started_at"] == 100.0
    assert snapshot["deadline"] == 100.0 + CHECKS_BUDGET_SECONDS


def test_refresh_wait_persists_a_window_before_requeue_or_resume_returns() -> None:
    state: dict[str, object] = {
        "parent": {"number": 1},
        "base": {"sha": "base-a"},
    }

    wait_for_github_refresh(
        state,
        code="github_timeout",
        message="authority refresh unavailable",
        waiting_for="GitHub authority refresh",
    )
    snapshot = public_supervision_snapshot(state)

    assert state["github_refresh_pending"] is True
    assert snapshot is not None
    assert snapshot["kind"] == "github_convergence"
    assert snapshot["timeout_resume_action"] == "agent-run run 1"


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
