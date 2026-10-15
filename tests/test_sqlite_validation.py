"""Successful business validation belongs to exact durable contents, not callers."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from agent_run import state_contract
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state import StateStore
from agent_run.object_state import save_owned
from support.ticket_parallel import ticket_run


@pytest.mark.parametrize("reference", ["history", "active", "ticket"])
def test_missing_invocation_identity_is_not_silently_omitted(tmp_path: Path, reference: str) -> None:
    invocation = {"work_subject": "ticket:2", "started_at": "original", "status": "completed"}
    field = {"history": "agent_invocation_history", "active": "active_agent_invocation", "ticket": "ticket_agent_invocations"}[reference]
    value = [invocation] if reference == "history" else {"2": invocation} if reference == "ticket" else invocation
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", {field: value})
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM work_invocations WHERE run_id='run-1'")
        connection.commit()
    with pytest.raises(ValueError, match="Invocation"):
        repository.load("run-1")
    if reference != "history":
        with pytest.raises(ValueError, match="Invocation"):
            repository.load_work("run-1", 2)
    else:
        # An unreferenced historical row is outside this bounded work query.
        assert repository.load_work("run-1", 2)["agent_invocation_history"] == []


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
        states._repository.save_objects(run_id, changed, fields={"policy_snapshot"})
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
            states._repository.save_objects(run_id, current, fields={"status"})
            assert states.load_current_run(run_id)["status"] == "operator_stopped"
            raise OSError("rollback")
    assert states.load_current_run(run_id)["status"] == state["status"]
    changed = deepcopy(state)
    changed["policy_snapshot"] = None
    SQLiteStateRepository(states.root, max_bytes=4 * 1024 * 1024).save_objects(run_id, changed, fields={"policy_snapshot"})
    with pytest.raises(ValueError):
        states.load_current_run(run_id)
    with pytest.raises(ValueError):
        states.load_current_run(run_id)


def test_large_projection_changes_and_caller_mutations_remain_independent(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    assert states.load_current_run(run_id) is not None
    state["extension"] = {"body": "large exact evidence " * 30000}
    save_owned(states, run_id, state, fields={"extension"})
    first = states.load_current_run(run_id)
    first["policy_snapshot"] = None
    assert states.load_current_run(run_id)["policy_snapshot"] == state["policy_snapshot"]
    damaged = states.load_current_run(run_id)
    damaged["policy_snapshot"] = None
    states._repository.save_objects(run_id, damaged, fields={"policy_snapshot"})
    with pytest.raises(ValueError):
        states.load_current_run(run_id)
