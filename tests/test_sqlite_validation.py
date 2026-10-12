"""Successful business validation belongs to exact durable contents, not callers."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run import state_contract
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state import StateStore
from support.ticket_parallel import ticket_run


def test_caller_mutations_do_not_become_validated_durable_facts(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    first = states.load_current_run(run_id)
    first["parent"]["body"] = "caller mutation"
    first["policy_snapshot"] = None
    assert states.load_current_run(run_id)["policy_snapshot"] == state["policy_snapshot"]
    with states._repository.atomic():
        assert states.load_current_run(run_id)["parent"]["body"] == state["parent"]["body"]
        changed = states.load_current_run(run_id)
        changed["policy_snapshot"] = None
        states._repository.save(run_id, changed)
        with pytest.raises(ValueError):
            states.load_current_run(run_id)
        with pytest.raises(ValueError):
            states.load_current_run(run_id)
    with pytest.raises(ValueError):
        StateStore(states.root).load_current_run(run_id)


@pytest.mark.parametrize("location", ["instance", "class"])
def test_changed_validator_and_custom_loader_do_not_reuse_validation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, location: str,
) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    assert states.load_current_run(run_id) is not None
    original = state_contract.require_current_run_state

    def reject(current):
        original(current)
        raise ValueError("new contract")

    with monkeypatch.context() as patch:
        patch.setattr(state_contract, "require_current_run_state", reject)
        with pytest.raises(ValueError, match="new contract"):
            states.load_current_run(run_id)
    assert states.load_current_run(run_id) is not None
    load = states.load_run

    def changed_projection(identity):
        current = load(identity)
        current["policy_snapshot"] = None
        return current

    if location == "instance":
        monkeypatch.setattr(states, "load_run", changed_projection)
    else:
        monkeypatch.setattr(StateStore, "load_run", lambda self, identity: changed_projection(identity))
    with pytest.raises(ValueError):
        states.load_current_run(run_id)
    with pytest.raises(ValueError):
        states.load_current_run(run_id)


def test_rollbacks_and_other_store_changes_are_not_labelled_validated(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    assert states.load_current_run(run_id) is not None
    with pytest.raises(OSError, match="rollback"):
        with states._repository.atomic():
            current = states.load_current_run(run_id)
            current["status"] = "operator_stopped"
            states._repository.save(run_id, current)
            assert states.load_current_run(run_id)["status"] == "operator_stopped"
            raise OSError("rollback")
    assert states.load_current_run(run_id)["status"] == state["status"]
    changed = deepcopy(state)
    changed["policy_snapshot"] = None
    SQLiteStateRepository(states.root, max_bytes=4 * 1024 * 1024).save(run_id, changed)
    with pytest.raises(ValueError):
        states.load_current_run(run_id)
    with pytest.raises(ValueError):
        states.load_current_run(run_id)


def test_large_projection_changes_and_caller_mutations_remain_independent(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    assert states.load_current_run(run_id) is not None
    state["extension"] = {"body": "large exact evidence " * 30000}
    states.save_run(run_id, state)
    first = states.load_current_run(run_id)
    first["policy_snapshot"] = None
    assert states.load_current_run(run_id)["policy_snapshot"] == state["policy_snapshot"]
    damaged = states.load_current_run(run_id)
    damaged["policy_snapshot"] = None
    states._repository.save(run_id, damaged)
    with pytest.raises(ValueError):
        states.load_current_run(run_id)
