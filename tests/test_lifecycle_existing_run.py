"""Existing-Run handoff uses durable requests, never inherited in-memory state."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.executor_host import BoundExecutorHost, ExecutorSpec
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_lifecycle import RunLifecycle, prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey
from support.seeding import write_fixture

from support.state import snapshot_run


def _accepted(git_repo: Path, tmp_path: Path, kind: str, *, target: str | None = None, payload_damage: str | None = None):
    fixture = write_fixture(git_repo / "github.json", issues={})
    states = StateStore(tmp_path / "state")
    state, _ = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), states).start(1)
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(states.root)
    payload = {"parent": 1, "run_id": target or state["run_id"]}
    if payload_damage == "missing_id":
        del payload["run_id"]
    elif payload_damage == "empty_id":
        payload["run_id"] = ""
    elif payload_damage == "invalid_id":
        payload["run_id"] = "../outside-run"
    claim = control.claim_action(task, kind=kind, payload=payload)
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
    return states, state, task, control, claim, reservation


def _executor(states, task, control, claim, reservation, select, execute):
    return RunLifecycle(
        states=StateStore(states.root), control=TaskControlStore(control.root),
        host=BoundExecutorHost(action_id=claim.action_id, generation=reservation.generation),
        task=task, preflight=lambda: pytest.fail("executor must reload durable target"),
        select_run=select, initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ),
        execute=execute,
    )


@pytest.mark.parametrize("kind", ["resume", "approve", "revise", "requeue"])
@pytest.mark.parametrize("window", ["unbound", "bound", "applied"])
def test_existing_action_reentry_applies_intent_exactly_once(
    git_repo: Path, tmp_path: Path, kind: str, window: str,
) -> None:
    states, initial, task, control, claim, reservation = _accepted(git_repo, tmp_path, kind)
    run_id = initial["run_id"]
    record = control.load(task)
    assert record["run_id"] is None
    assert record["action"]["run_id"] is None
    assert record["executor"]["run_id"] is None
    if window != "unbound":
        control.bind_run(task, claim.action_id, run_id, generation=reservation.generation)
    if window == "applied":
        initial["test_applied_intents"] = [kind]
        prepare_action_application_receipt(initial, claim.action)
        states.save_run(run_id, initial)
    selected = []
    executed = []

    def select(action):
        selected.append(kind)
        record = control.load(task)
        assert record["action"]["run_id"] == run_id
        state = StateStore(states.root).load_current_run(run_id)
        state.setdefault("test_applied_intents", []).append(kind)
        prepare_action_application_receipt(state, action)
        StateStore(states.root).save_run(run_id, state)
        return state, True

    def execute(actual_run):
        executed.append(actual_run)
        return StateStore(states.root).load_current_run(actual_run)

    lifecycle = _executor(states, task, control, claim, reservation, select, execute)
    final, _, _ = lifecycle.execute_claimed(action_id=claim.action_id, generation=reservation.generation)
    assert final["test_applied_intents"] == [kind]
    assert selected == ([] if window == "applied" else [kind])
    # approve requires real approval authority; a receipt alone must not dispatch.
    assert executed == ([] if kind == "approve" else [run_id])
    record = control.load(task)
    assert record["action"]["application_observed"] is True
    assert record["action"]["run_id"] == record["executor"]["run_id"] == run_id
    receipt = StateStore(states.root).load_current_run(run_id)["action_application_receipt"]
    assert receipt["action_id"] == claim.action_id
    assert receipt["executor_generation"] == reservation.generation


@pytest.mark.parametrize("damage", ["missing", "missing_id", "empty_id", "invalid_id", "repository", "parent", "generation", "action", "binding", "unreadable"])
def test_existing_action_reentry_rejects_wrong_target_before_business(
    git_repo: Path, tmp_path: Path, damage: str,
) -> None:
    states, initial, task, control, claim, reservation = _accepted(
        git_repo, tmp_path, "resume", target="missing-run" if damage == "missing" else None, payload_damage=damage,
    )
    run_id = initial["run_id"]
    if damage == "repository":
        initial["repository"] = "another/repository"
    if damage == "parent":
        initial["parent"]["number"] = 999
    if damage in {"repository", "parent"}:
        states.save_run(run_id, initial)
    if damage == "binding":
        control.bind_run(task, claim.action_id, "other-run", generation=reservation.generation)
    if damage == "unreadable":
        (states.root / "state.sqlite3").write_bytes(b"broken SQLite database")
    before = (states.root / "state.sqlite3").read_bytes()
    lifecycle = _executor(
        states, task, control, claim, reservation,
        lambda _: pytest.fail("must not apply intent"),
        lambda _: pytest.fail("must not execute business"),
    )
    with pytest.raises((ActionReconciliationError, ValueError)):
        lifecycle.execute_claimed(
            action_id="stale-action" if damage == "action" else claim.action_id,
            generation=reservation.generation + (damage == "generation"),
        )
    assert (states.root / "state.sqlite3").read_bytes() == before


@pytest.mark.parametrize("kind", ["resume", "approve", "revise", "requeue"])
def test_binding_does_not_bypass_command_business_rejection(
    git_repo: Path, tmp_path: Path, kind: str,
) -> None:
    states, initial, task, control, claim, reservation = _accepted(git_repo, tmp_path, kind)
    run_id = initial["run_id"]
    before = snapshot_run((states.root, str(run_id)))

    def reject(_action):
        raise ValueError("current command boundary no longer permits intent")

    lifecycle = _executor(
        states, task, control, claim, reservation, reject,
        lambda _: pytest.fail("rejected command must not dispatch"),
    )
    with pytest.raises(ValueError, match="current command boundary"):
        lifecycle.execute_claimed(action_id=claim.action_id, generation=reservation.generation)
    assert snapshot_run((states.root, str(run_id))) == before
    record = control.load(task)
    assert record["action"]["run_id"] == run_id
    assert record["action"]["application_observed"] is False
    assert record["action"]["status"] == "failed"
