"""Shared Ticket work capacity and durable FIFO ownership boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.delivery_policy import DeliveryPolicy, DeliveryPolicyError
from agent_run.state import StateStore
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_work import TicketWorkAdmission, generation
from support.ticket_parallel import ticket_run


def _queue(states: StateStore, run_id: str) -> list[dict[str, Any]]:
    # A fresh reader also verifies the persisted contract after every mutation.
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved is not None
    return saved["ticket_work_queue"]


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_mixed_roles_share_capacity_and_fifo(git_repo: Path, workers: int) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3, 4, 5), workers=workers)
    run_id = state["run_id"]
    admission = TicketWorkAdmission(states, run_id)
    admission.seed([2, 3, 4, 5])
    roles = ("development", "reviewer", "publication", "integration_conflict")
    # A later role cannot take a free slot ahead of the oldest ready work.
    assert admission.acquire(5, generation(state, 5), roles[3]) is None
    tokens = [admission.acquire(n, generation(state, n), role)
              for n, role in zip((2, 3, 4, 5), roles)]
    assert all(tokens[:workers])
    assert tokens[workers:] == [None] * (4 - workers)
    queue = _queue(states, run_id)
    assert [item["role"] for item in queue] == list(roles)
    assert [item["lease_id"] for item in queue] == tokens
    admission.seed([5, 4, 3, 2, 2])
    assert _queue(states, run_id) == queue

    assert tokens[0] is not None
    admission.release(tokens[0])
    # Refill immediately while the other admitted jobs still hold their leases.
    next_number = 2 + workers
    token = admission.acquire(next_number, generation(state, next_number), roles[workers])
    assert token is not None
    queue = _queue(states, run_id)
    assert sum(item["lease_id"] is not None for item in queue) == workers
    assert [item["ticket_number"] for item in queue] == [3, 4, 5]


def test_exact_lease_release_cannot_cancel_replacement_work(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3), workers=1)
    admission = TicketWorkAdmission(states, state["run_id"])
    first = admission.acquire(2, generation(state, 2), "development")
    assert first is not None
    with pytest.raises(TaskControlBusyError):
        admission.acquire(2, generation(state, 2), "reviewer")
    admission.release(first)
    second = admission.acquire(2, generation(state, 2), "reviewer")
    assert second is not None and second != first
    admission.release(first)
    admission.release("unknown-lease")
    assert _queue(states, state["run_id"])[0]["lease_id"] == second
    assert admission.acquire(3, generation(state, 3), "publication") is None
    admission.release(second)
    assert admission.acquire(3, generation(state, 3), "publication") is not None


def test_stale_generation_cannot_enter_queue(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3))
    admission = TicketWorkAdmission(states, state["run_id"])
    with pytest.raises(TaskControlBusyError):
        admission.acquire(2, generation(state, 2) + 1, "development")
    assert _queue(states, state["run_id"]) == []


def test_pause_cancels_waiter_without_releasing_running_work(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3, 4), workers=1)
    run_id = state["run_id"]
    admission = TicketWorkAdmission(states, run_id)
    admission.seed([3, 2, 4])
    token = admission.acquire(3, generation(state, 3), "reviewer")
    assert token is not None
    assert admission.acquire(2, generation(state, 2), "development") is None
    paused = states.load_current_run(run_id)
    assert paused is not None
    paused["ticket_pause_requests"]["2"] = "2026-09-30T00:00:00+00:00"
    states.save_run(run_id, paused)
    assert [item["ticket_number"] for item in _queue(states, run_id)] == [3, 4]
    admission.seed([2, 4])
    assert admission.acquire(2, generation(state, 2), "development") is None
    assert [item["ticket_number"] for item in _queue(states, run_id)] == [3, 4]
    admission.release(token)
    assert admission.acquire(4, generation(state, 4), "publication") is not None


@pytest.mark.parametrize("status", ["operator_stopped", "abandoned"])
def test_run_gate_blocks_admission_and_preserves_only_resumable_waiters(
    git_repo: Path, status: str,
) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3), workers=1)
    run_id = state["run_id"]
    admission = TicketWorkAdmission(states, run_id)
    admission.seed([2, 3])
    token = admission.acquire(2, generation(state, 2), "development")
    assert token is not None
    stopped = states.load_current_run(run_id)
    assert stopped is not None
    stopped["status"] = status
    states.save_run(run_id, stopped)
    remaining = [None] if status == "operator_stopped" else []
    assert [item["lease_id"] for item in _queue(states, run_id)] == [token, *remaining]
    with pytest.raises(TaskControlBusyError):
        admission.acquire(3, generation(state, 3), "reviewer")
    admission.release(token)
    assert [item["lease_id"] for item in _queue(states, run_id)] == remaining


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_capacity_requires_positive_integer(invalid: Any) -> None:
    assert DeliveryPolicy().ticket_development_workers == 3
    with pytest.raises(DeliveryPolicyError):
        DeliveryPolicy(ticket_development_workers=invalid)


@pytest.mark.parametrize("corruption", [
    "missing", "not_list", "duplicate_ticket", "duplicate_lease", "stale_generation",
    "unknown_role", "unknown_ticket", "over_capacity", "invalid_capacity",
])
def test_invalid_persisted_queue_is_rejected(git_repo: Path, corruption: str) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3), workers=1)
    entry = {"ticket_number": 2, "generation": generation(state, 2),
             "role": "development", "lease_id": "lease-2"}
    state["ticket_work_queue"] = [entry]
    if corruption == "missing":
        del state["ticket_work_queue"]
    elif corruption == "not_list":
        state["ticket_work_queue"] = {}
    elif corruption == "duplicate_ticket":
        state["ticket_work_queue"].append({**entry, "lease_id": None})
    elif corruption in {"duplicate_lease", "over_capacity"}:
        state["ticket_work_queue"].append({
            **entry, "ticket_number": 3, "generation": generation(state, 3),
            "lease_id": "lease-2" if corruption == "duplicate_lease" else "lease-3",
        })
    elif corruption == "stale_generation":
        entry["generation"] += 1
    elif corruption == "unknown_role":
        entry["role"] = "supervision"
    elif corruption == "unknown_ticket":
        entry["ticket_number"] = 999
    elif corruption == "invalid_capacity":
        state["policy_snapshot"]["ticket_development_workers"] = True
    path = states.runs_directory / f"{state['run_id']}.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(IncompatibleRunStateError):
        StateStore(states.root).load_current_run(state["run_id"])


@pytest.mark.parametrize("role", ["development", "reviewer", "publication", "integration_conflict"])
def test_stop_before_process_launch_uses_exact_lease_and_keeps_slot_until_release(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    from agent_run.ticket_pause import TicketStopRequested, request_ticket_pause
    from agent_run.worker_control import worker_control
    from agent_run.worker_sandbox import run_worker_process

    state, states, _, _ = ticket_run(git_repo, (2, 3), workers=1)
    run_id = state["run_id"]
    admission = TicketWorkAdmission(states, run_id)
    lease = admission.acquire(2, generation(state, 2), role)
    assert lease is not None
    request_ticket_pause(states, run_id, 2)
    original_queue = _queue(states, run_id)
    request_ticket_pause(states, run_id, 2)
    assert _queue(states, run_id) == original_queue

    def unexpected_process(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Stop accepted before launch must prevent the Worker from starting")

    monkeypatch.setattr("agent_run.worker_sandbox.subprocess.Popen", unexpected_process)
    binding = worker_control.set(lambda: admission.check_control(lease))
    try:
        with pytest.raises(TicketStopRequested):
            run_worker_process(["unused"], cwd=git_repo, prompt="", environment={}, timeout=1)
    finally:
        worker_control.reset(binding)
    assert _queue(states, run_id)[0]["lease_id"] == lease
    assert admission.acquire(3, generation(state, 3), "development") is None
    admission.release(lease)
    with pytest.raises(TaskControlBusyError):
        admission.check_control(lease)
    assert admission.acquire(3, generation(state, 3), "development") is not None
