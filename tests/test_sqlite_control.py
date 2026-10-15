"""Control relations preserve identity and join the caller's SQL transaction."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run import sqlite_control as control


IDENTITY = {"workspace": "/fixture/repository", "repository": "example/project", "parent": 7}
ROOT = "/fixture/state"
STAMP = "2026-10-15T10:00:00+00:00"


def action(action_id: str = "action-1", kind: str = "run", generation: int = 1) -> dict[str, Any]:
    payload = {"message": "保留冻结的意图", "kind": kind}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"action_id": action_id, "kind": kind, "payload": payload,
            "payload_digest": hashlib.sha256(encoded.encode()).hexdigest(), "status": "accepted", "run_id": None,
            "executor_generation": generation, "application_observed": False, "submitted_at": STAMP,
            "completed_at": None, "result_status": None, "failure": None}


def record() -> dict[str, Any]:
    return {"protocol": 2, "task": dict(IDENTITY), "run_id": None, "run_state_dir": None,
            "next_generation": 2, "action": action(), "action_history": [], "updated_at": STAMP,
            "executor": {"action_id": "action-1", "generation": 1, "status": "starting", "run_id": None,
                         "binding_token": "executor-binding", "pid": 400, "process_start_token": "executor-start",
                         "handshake_at": None, "started_at": STAMP, "exited_at": None, "failure": None,
                         "workers": [{"pid": 401, "process_start_token": "worker-one", "started_at": STAMP},
                                     {"pid": 402, "process_start_token": "worker-two", "started_at": STAMP}]}}


def receipt() -> dict[str, Any]:
    current = action()
    return {"protocol": 2, "action_id": current["action_id"], "kind": current["kind"],
            "payload_digest": current["payload_digest"], "executor_generation": 1,
            "run_id": "run-1", "applied_at": STAMP}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(tmp_path / "control.sqlite3", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        connection.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY,repository TEXT,parent_number INTEGER,budget INTEGER)")
        connection.execute("INSERT INTO runs VALUES ('run-1','example/project',7,1)")
        for statement in control.SCHEMA:
            connection.execute(statement)
        connection.commit()
        yield connection
    finally:
        connection.close()


def test_relational_control_reopens_with_shared_target_and_history_identity(
    database: sqlite3.Connection, tmp_path: Path,
) -> None:
    original = record()
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
    assert control.load(database, "task-1", IDENTITY) == original
    original["action_history"] = [{"run_id": None, "action": original["action"], "executor": original["executor"]}]
    original["action"] = {**action("stop-1", "stop", 2), "target_executor": original["executor"]}
    original["executor"] = None
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
    with closing(sqlite3.connect((tmp_path / "control.sqlite3").as_uri() + "?mode=ro", uri=True)) as reopened:
        view = control.load(reopened, "task-1", IDENTITY)
    assert view == original
    assert view is not None
    assert view["action"]["target_executor"] is view["action_history"][0]["executor"]
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    conflicting = deepcopy(view)
    conflicting["action_history"][0]["executor"] = deepcopy(conflicting["action"]["target_executor"])
    conflicting["action_history"][0]["executor"]["failure"] = "contradictory snapshot"
    writes = database.total_changes
    with pytest.raises(ValueError, match="contradictory"):
        control.save(database, "task-1", conflicting)
    assert database.total_changes == writes


def test_action_payload_is_immutable_and_unknown_core_fields_are_rejected(database: sqlite3.Connection) -> None:
    original = record()
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
    changed = deepcopy(original)
    changed["action"]["payload"]["message"] = "a different intent"
    changed["action"]["payload_digest"] = hashlib.sha256(json.dumps(
        changed["action"]["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    writes = database.total_changes
    with pytest.raises(ValueError, match="immutable"):
        control.save(database, "task-1", changed)
    for subject in (original, original["action"], original["executor"], original["executor"]["workers"][0]):
        subject["unexpected_core_fact"] = True
        with pytest.raises(ValueError, match="Unsupported Control"):
            control.save(database, "task-1", original)
        del subject["unexpected_core_fact"]
    original["action"]["receipt_only_reconciliation"] = True
    with pytest.raises(ValueError, match="Unsupported Control"):
        control.save(database, "task-1", original)
    assert database.total_changes == writes


def test_exact_worker_closeout_after_stop_does_not_replace_current_ownership(database: sqlite3.Connection) -> None:
    stopped = record()
    old_executor = stopped["executor"]
    stopped["action_history"] = [{"run_id": None, "action": stopped["action"], "executor": old_executor}]
    stopped["action"] = {**action("stop-1", "stop", 2), "target_executor": old_executor}
    stopped["executor"] = None
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", stopped)
    writes = database.total_changes
    with pytest.raises(ValueError, match="binding"):
        control.finish_worker(database, "task-1", "action-1", 1, 401, "foreign-start")
    assert database.total_changes == writes
    with database:
        database.execute("BEGIN")
        remaining = control.finish_worker(database, "task-1", "action-1", 1, 401, "worker-one")
        assert [worker["pid"] for worker in remaining["workers"]] == [402]
        remaining["status"] = "exited"
        remaining["exited_at"] = STAMP
        control.save_executor(database, "task-1", remaining)
    view = control.load(database, "task-1", IDENTITY)
    assert view is not None
    assert view["executor"] is None and view["action"]["action_id"] == "stop-1"
    assert view["action"]["target_executor"] is view["action_history"][0]["executor"]
    assert view["action"]["target_executor"]["status"] == "exited"
    assert [worker["pid"] for worker in view["action"]["target_executor"]["workers"]] == [402]
    assert control.finish_worker(database, "task-1", "action-1", 1, 401, "worker-one") == remaining


def test_application_joins_business_transaction_and_completed_receipt_is_idempotent(database: sqlite3.Connection) -> None:
    original = record()
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
    with pytest.raises(OSError, match="interrupted"):
        with database:
            database.execute("BEGIN")
            database.execute("UPDATE runs SET budget=2 WHERE run_id='run-1'")
            control.observe_application(database, receipt(), ROOT)
            assert database.in_transaction
            raise OSError("interrupted before commit")
    assert database.execute("SELECT budget FROM runs").fetchone() == (1,)
    assert control.load(database, "task-1", IDENTITY) == original
    with database:
        database.execute("BEGIN")
        database.execute("UPDATE runs SET budget=2 WHERE run_id='run-1'")
        control.observe_application(database, receipt(), ROOT)
    applied = control.load(database, "task-1", IDENTITY)
    assert applied is not None
    assert applied["run_id"] == applied["action"]["run_id"] == applied["executor"]["run_id"] == "run-1"
    assert applied["action"]["application_observed"] is True
    assert applied["action"]["status"] == "applying"
    applied["action"].update(status="completed", completed_at=STAMP)
    applied["executor"].update(status="exited", exited_at=STAMP)
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", applied)
    writes = database.total_changes
    control.observe_application(database, receipt(), ROOT)
    assert database.total_changes == writes
    for wrong in ({**receipt(), "executor_generation": 2}, {**receipt(), "action_id": "unknown"},
                  {**receipt(), "payload_digest": "foreign"}, {**receipt(), "protocol": 2.0},
                  {**receipt(), "executor_generation": True}, {**receipt(), "target_executor": None}):
        with pytest.raises(ValueError, match="Control application"):
            control.observe_application(database, wrong, ROOT)
    with pytest.raises(ValueError, match="ownership"):
        control.observe_application(database, receipt(), "/other/state")
    assert database.total_changes == writes


def test_history_pruning_keeps_referenced_target_and_other_parent_facts(database: sqlite3.Connection) -> None:
    current, other = record(), record()
    other["task"]["parent"] = 8
    other["action"]["action_id"] = "other-parent-action"
    other["executor"]["action_id"] = "other-parent-action"
    with database:
        database.execute("BEGIN")
        control.save(database, "other-task", other)
        control.save(database, "task-1", current)
        control.observe_application(database, receipt(), ROOT)
    current = control.load(database, "task-1", IDENTITY)
    assert current is not None
    first_executor = deepcopy(current["executor"])
    for generation in range(2, 8):
        old_executor = current["executor"]
        current["action_history"].append({"run_id": current["run_id"], "state_dir": ROOT,
                                          "action": current["action"], "executor": old_executor})
        current["action_history"] = current["action_history"][-4:]
        selected = action(f"action-{generation}", "stop" if generation == 2 else "resume", generation)
        selected["run_id"] = "run-1"
        if generation == 2:
            selected["target_executor"] = old_executor
            current["executor"] = None
        else:
            current["executor"] = {**deepcopy(first_executor), "action_id": selected["action_id"], "generation": generation}
        current["action"] = selected
        current["next_generation"] = generation + 1
        with database:
            database.execute("BEGIN")
            control.save(database, "task-1", current)
        if generation == 2:
            writes = database.total_changes
            with pytest.raises(ValueError, match="ownership"):
                control.observe_application(database, receipt(), ROOT)
            assert database.total_changes == writes
        if generation == 6:
            assert database.execute("SELECT 1 FROM control_actions WHERE task_id='task-1' AND action_id='action-1'").fetchone() is None
            assert control.find_executor(database, "task-1", "action-1", 1) == first_executor
        current = control.load(database, "task-1", IDENTITY)
        assert current is not None
        assert control.load(database, "other-task", other["task"]) == other
    assert control.find_executor(database, "task-1", "action-1", 1) is None
    assert len(current["action_history"]) == 4
    assert database.execute("SELECT COUNT(*) FROM control_payloads WHERE task_id='task-1'").fetchone() == (5,)
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("table,column", [
    ("control_tasks", "updated_at"), ("control_actions", "failure"),
    ("control_executors", "failure"), ("control_workers", "process_start_token"),
    ("control_action_history", "state_dir"), ("control_payloads", "value"),
])
def test_corrupt_control_text_is_rejected_before_python_decoding(
    database: sqlite3.Connection, table: str, column: str,
) -> None:
    original = record()
    original["action_history"] = [{"run_id": None, "action": original["action"], "executor": original["executor"]}]
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
        database.execute(f"UPDATE {table} SET {column}=CAST(zeroblob(?) AS TEXT) WHERE task_id=?",
                         (control.MAX_CONTROL_BYTES + 1, "task-1"))

    def decode(raw: bytes) -> str:
        assert len(raw) <= control.MAX_CONTROL_BYTES, "Oversized SQL text was fetched before its size proof"
        return raw.decode("utf-8")

    database.text_factory = decode
    writes = database.total_changes
    with database:
        database.execute("BEGIN")
        with pytest.raises(ValueError, match="size|byte limit"):
            control.load(database, "task-1", IDENTITY)
    assert database.total_changes == writes


def test_control_view_rejects_combined_fields_over_its_limit(database: sqlite3.Connection) -> None:
    original = record()
    with database:
        database.execute("BEGIN")
        control.save(database, "task-1", original)
        # Each row fits; the current view must still obey the whole-record bound.
        database.execute("UPDATE control_actions SET failure=? WHERE task_id=?", ("a" * (128 * 1024), "task-1"))
        database.execute("UPDATE control_executors SET failure=? WHERE task_id=?", ("b" * (128 * 1024), "task-1"))
    writes = database.total_changes
    with pytest.raises(ValueError, match="byte limit"):
        control.load(database, "task-1", IDENTITY)
    assert database.total_changes == writes
