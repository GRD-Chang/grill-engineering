from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.graph import state_from_graph
from agent_run.state import StateStore
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from agent_run.ticket_queue import next_ticket
from agent_run.review_budget import new_budget
from support.seeding import write_fixture


def _state(repo: Path) -> dict[str, Any]:
    issues = {
        str(number): {
            "number": number,
            "title": f"Ticket {number}",
            "body": f"Deliver ticket {number}.",
            "state": "OPEN",
            "labels": ["ready-for-agent"],
            "blocked_by": [],
        }
        for number in (2, 3)
    }
    fixture = write_fixture(repo / "github.json", issues=issues)
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(repo), StateStore(repo / ".agent-run")
    )
    state, _ = controller.start(1)
    return state


def _two_jobs(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    first = state["ticket_jobs"]["2"]
    first.update(
        {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": new_budget(),
            "review_budget_history": [],
            "ci_recovery": new_ci_recovery_budget(),
        }
    )
    second = deepcopy(first)
    second["ticket_number"] = 3
    state["ticket_jobs"]["3"] = second
    state["active_ticket_job"] = second
    return first, second


def test_worker_limit_controls_new_ticket_admission_during_repair(git_repo: Path) -> None:
    state = _state(git_repo)
    first = state["ticket_jobs"]["2"]
    first["phase"] = "repairing"
    state["active_ticket_job"] = first
    state["ticket_jobs"]["3"] = {"ticket_number": 3}
    state["frontier"] = [2, 3]
    state["policy_snapshot"]["ticket_development_workers"] = 1

    assert next_ticket(state, state["frontier"]) == 2
    graph = FixtureGitHubReader(git_repo / "github.json").delivery_graph(1)
    assert state_from_graph(state, graph)["active_ticket_job"]["ticket_number"] == 2

    state["policy_snapshot"]["ticket_development_workers"] = 2
    assert next_ticket(state, state["frontier"]) == 3
    assert state_from_graph(state, graph)["active_ticket_job"]["ticket_number"] == 3


@pytest.mark.parametrize("protocol", [None, False, 0, 1.0, "1"])
def test_multi_ticket_protocol_rejects_old_or_malformed_run(
    git_repo: Path, protocol: object
) -> None:
    state = _state(git_repo)
    if protocol is None:
        state.pop("multi_ticket_protocol")
    else:
        state["multi_ticket_protocol"] = protocol

    with pytest.raises(IncompatibleRunStateError, match="Multi Ticket protocol"):
        require_current_run_state(state)


def test_old_protocol_is_rejected_on_resume_before_delivery(git_repo: Path) -> None:
    state = _state(git_repo)
    state.pop("multi_ticket_protocol")
    states = StateStore(git_repo / ".agent-run")
    states.save_run(str(state["run_id"]), state)
    fixture = git_repo / "github.json"
    controller = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), states)

    with pytest.raises(IncompatibleRunStateError, match="Multi Ticket protocol"):
        controller.resume(str(state["run_id"]))


@pytest.mark.parametrize("field", ["ticket_integration_queue", "ticket_integration_holder"])
def test_missing_queue_or_holder_is_rejected(git_repo: Path, field: str) -> None:
    state = _state(git_repo)
    state.pop(field)

    with pytest.raises(IncompatibleRunStateError, match="Ticket integration queue"):
        require_current_run_state(state)


@pytest.mark.parametrize("holder", [False, 0, 2.0, "2"])
def test_invalid_holder_is_rejected(git_repo: Path, holder: object) -> None:
    state = _state(git_repo)
    state["ticket_integration_holder"] = holder

    with pytest.raises(IncompatibleRunStateError, match="Ticket integration holder"):
        require_current_run_state(state)


def test_two_run_wide_operator_gates_are_rejected(git_repo: Path) -> None:
    state = _state(git_repo)
    state["active_ticket_job"] = None
    for key in ("run_acceptance", "run_publication"):
        state[key] = {
            "phase": "ready_for_human",
            "blocked_reason": "reviewer_requires_human",
            "human_blockers": ["Maintainer input is required."],
        }

    with pytest.raises(IncompatibleRunStateError, match="multiple current Run-wide"):
        require_current_run_state(state)


def test_queue_and_holder_allow_a_to_wait_while_b_develops(git_repo: Path) -> None:
    state = _state(git_repo)
    first, second = _two_jobs(state)
    first["phase"] = "accepted"
    second["phase"] = "developing"
    state["ticket_integration_holder"] = 2

    require_current_run_state(state)


@pytest.mark.parametrize(
    ("queue", "message"),
    [
        ([{"ticket_number": 2, "ready_at": "2026-09-24T00:00:00"}], "ready_at"),
        ([{"ticket_number": True, "ready_at": "2026-09-24T00:00:00+00:00"}], "entry"),
        ([{"ticket_number": 2, "ready_at": "2026-09-24T00:00:00+00:00", "extra": 1}], "entry"),
        ([{"ticket_number": 2, "ready_at": "2026-09-24T00:00:00+00:00"}] * 2, "duplicate"),
        (
            [
                {"ticket_number": 3, "ready_at": "2026-09-24T00:00:00+00:00"},
                {"ticket_number": 2, "ready_at": "2026-09-24T00:00:00+00:00"},
            ],
            "unordered",
        ),
    ],
)
def test_queue_rejects_invalid_entry_or_order(
    git_repo: Path, queue: list[dict[str, Any]], message: str
) -> None:
    state = _state(git_repo)
    first, second = _two_jobs(state)
    first["phase"] = second["phase"] = "ready_for_integration"
    state["ticket_integration_queue"] = queue

    with pytest.raises(IncompatibleRunStateError, match=message):
        require_current_run_state(state)


def test_queue_uses_instant_then_ticket_number_for_order(git_repo: Path) -> None:
    state = _state(git_repo)
    first, second = _two_jobs(state)
    first["phase"] = second["phase"] = "ready_for_integration"
    state["ticket_integration_queue"] = [
        {"ticket_number": 2, "ready_at": "2026-09-24T08:00:00+08:00"},
        {"ticket_number": 3, "ready_at": "2026-09-24T00:00:00+00:00"},
    ]

    require_current_run_state(state)


@pytest.mark.parametrize("mutation", ["unqueued", "wrong_phase", "holder_queued", "second_seat", "active_mismatch"])
def test_queue_rejects_job_or_seat_mismatch(git_repo: Path, mutation: str) -> None:
    state = _state(git_repo)
    first, second = _two_jobs(state)
    first["phase"] = "accepted"
    second["phase"] = "ready_for_integration"
    state["ticket_integration_holder"] = 2
    state["ticket_integration_queue"] = [
        {"ticket_number": 3, "ready_at": "2026-09-24T00:00:00+00:00"}
    ]
    if mutation == "unqueued":
        state["ticket_integration_queue"] = []
    elif mutation == "wrong_phase":
        second["phase"] = "developing"
    elif mutation == "holder_queued":
        first["phase"] = "ready_for_integration"
        state["ticket_integration_queue"].insert(
            0, {"ticket_number": 2, "ready_at": "2026-09-23T00:00:00+00:00"}
        )
    elif mutation == "second_seat":
        second["phase"] = "publishing"
        state["ticket_integration_queue"] = []
    else:
        state["active_ticket_job"] = {**second, "phase": "developing"}

    with pytest.raises(IncompatibleRunStateError, match="integration|unqueued|active Ticket gate mirror"):
        require_current_run_state(state)
