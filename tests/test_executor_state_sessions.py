"""Executor 只复用准备 SQL，退出时关闭准确状态工厂的连接。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.executor import DeliveryExecutor
from agent_run.object_state import object_store
from agent_run.state import SimulatedProcessCrash, StateStore
from support.ticket_parallel import ticket_run


@pytest.mark.parametrize("exit_kind", ["success", "factory", "advance", "cancel", "crash"])
def test_executor_retains_prepared_sql_and_closes_selected_store_on_every_exit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, exit_kind: str,
) -> None:
    initial, selected, _git, _fixture = ticket_run(git_repo, (2,))
    run_id = initial["run_id"]
    unused = StateStore(git_repo / ".unused-state")
    created: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        created.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    binding = ("action-1", 2, run_id)
    selections = []

    def select(actual_binding):
        selections.append(actual_binding)
        return selected

    def driver_factory(actual_store, actual_binding):
        assert actual_store is selected and actual_binding == binding
        assert selected.load_observation_run(run_id) is not None
        reader = created[-1]
        # A prepared-statement scope must remain usable between transactions.
        assert reader.execute("PRAGMA query_only").fetchone() == (1,)
        assert not reader.in_transaction
        if exit_kind == "factory":
            raise RuntimeError("driver construction failed")
        prepared = []

        def authorize(action, argument1, argument2, database, source):
            if action == sqlite3.SQLITE_FUNCTION and argument2 == "upper":
                prepared.append(argument2)
            return sqlite3.SQLITE_OK

        reader.set_authorizer(authorize)
        assert reader.execute("SELECT upper(?)", ("run-1",)).fetchone() == ("RUN-1",)

        def advance(state):
            assert selected.load_observation_run(run_id) is not None
            assert reader.execute("SELECT upper(?)", ("run-2",)).fetchone() == ("RUN-2",)
            assert prepared == ["upper"]
            scoped = object_store(selected)
            current = scoped.load_current_run(run_id)
            assert current is not None
            scoped.save_run(run_id, current)
            assert not reader.in_transaction
            assert not created[-1].in_transaction
            if exit_kind == "advance":
                raise RuntimeError("driver advance failed")
            if exit_kind == "cancel":
                raise KeyboardInterrupt("execution cancelled")
            if exit_kind == "crash":
                raise SimulatedProcessCrash("simulated crash")
            return state

        return SimpleNamespace(advance=advance)

    executor = DeliveryExecutor(
        states=unused, state_store_factory=select, driver_factory=driver_factory,
    )
    if exit_kind == "success":
        assert executor.execute(run_id, action_id="action-1", generation=2)["run_id"] == run_id
    else:
        errors = {"factory": RuntimeError, "advance": RuntimeError,
                  "cancel": KeyboardInterrupt, "crash": SimulatedProcessCrash}
        with pytest.raises(errors[exit_kind]):
            executor.execute(run_id, action_id="action-1", generation=2)
    assert selections == [binding]
    assert selected.run_saved_observer is None
    assert created
    for connection in created:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert StateStore(selected.root).load_current_run(run_id) is not None
