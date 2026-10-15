"""真实提交边界：生命周期控制与业务只能同时成为新事实。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_run.object_state import object_store, save_owned
from agent_run.executor_host import _process_start_token
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.sqlite_runtime import require_wal_runtime
from agent_run.state import StateStore
from agent_run.state_commits import lifecycle_commits
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.task_control import (
    ActionBusyError, ActionReconciliationError, TaskControlBusyError, TaskControlStore, TaskKey,
)


def _reserved(root: Path):
    control = TaskControlStore(root)
    task = TaskKey(root.parent, "example/project", 1)
    claim = control.claim_action(task, kind="resume", payload={"run_id": "run-1"})
    assert claim.action is not None and claim.action_id is not None
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    control.mark_handshake(task, action_id=claim.action_id, generation=generation,
                           pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    return control, task, claim.action, generation


def test_task_precedes_run_and_has_only_sql_authority(tmp_path: Path) -> None:
    control, task, action, generation = _reserved(tmp_path / "state")
    assert StateStore(control.root).list_run_ids() == []
    reopened = TaskControlStore(control.root).load(task)
    assert reopened["action"]["action_id"] == action["action_id"]
    assert reopened["executor"]["generation"] == generation
    assert not list(control.directory.glob("*.json"))
    with closing(sqlite3.connect(control.root / "state.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM control_tasks").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_other_parent_write_contention_does_not_queue_or_change_action(tmp_path: Path) -> None:
    control, task, action, _ = _reserved(tmp_path)
    other = TaskKey(task.workspace, task.repository, 2)
    with control._repository.atomic():
        with ThreadPoolExecutor(max_workers=1) as executor:
            # A different Parent shares the real database writer, not the
            # lifecycle Action slot or a business FIFO entry.
            with pytest.raises(TaskControlBusyError):
                executor.submit(control.claim_action, other, kind="run", payload={}).result(timeout=3)
            attached = executor.submit(
                control.claim_action, task, kind="resume", payload={"run_id": "run-1"},
            ).result(timeout=3)
            assert attached.attached and attached.action_id == action["action_id"]
            with pytest.raises(ActionBusyError):
                executor.submit(control.claim_action, task, kind="approve", payload={}).result(timeout=3)
    assert control.load(other) is None
    claimed = control.claim_action(other, kind="run", payload={})
    assert claimed.attached is False
    assert control.load(task)["action"]["action_id"] == action["action_id"]


@pytest.mark.parametrize("creation", [True, False])
@pytest.mark.parametrize("committed", [False, True])
def test_process_exit_commits_control_and_business_together(
    tmp_path: Path, creation: bool, committed: bool,
) -> None:
    root = tmp_path / "state"
    control, task, action, _ = _reserved(root)
    old = {"run_id": "run-1", "repository": "example/project", "parent": {"number": 1},
           "job": {"phase": "developing"}, "budget": 1, "invocations": ["running"]}
    if not creation:
        StateStore(root).save_run("run-1", old)
    before = deepcopy(control.load(task))
    new = {**old, "job": {"phase": "candidate"}, "budget": 2, "invocations": ["completed"]}
    prepare_action_application_receipt(new, action)
    script = """
import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from agent_run.state import StateStore
from agent_run.object_state import save_owned
root, creation, committed, encoded = sys.argv[1:5]
connect = sqlite3.connect
class CutConnection(sqlite3.Connection):
    def commit(self):
        if committed == "yes":
            super().commit()
        os._exit(71)
def open_connection(*args, **kwargs):
    kwargs.setdefault("factory", CutConnection)
    return connect(*args, **kwargs)
sqlite3.connect = open_connection
states = StateStore(Path(root))
state = json.loads(encoded)
if creation == "yes":
    states.save_run("run-1", state)
else:
    save_owned(states, "run-1", state, fields={"job", "budget", "invocations", "action_application_receipt"})
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(root), "yes" if creation else "no",
         "yes" if committed else "no", json.dumps(new),
         str(Path(__file__).parents[1] / "src")],
        capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 71, child.stderr
    reopened = TaskControlStore(root).load(task)
    saved = StateStore(root).load_run("run-1")
    if committed:
        assert saved == {**new, "timeline": saved["timeline"]}
        assert reopened["run_id"] == reopened["action"]["run_id"] == reopened["executor"]["run_id"] == "run-1"
        assert reopened["action"]["application_observed"] is True
        assert reopened["action"]["status"] == "applying"
    else:
        assert reopened == before
        assert saved == (None if creation else old)
    assert not list(control.directory.glob("*.json"))


def test_invalid_application_rolls_back_business_and_control(tmp_path: Path) -> None:
    control, task, action, _ = _reserved(tmp_path)
    before = control.load(task)
    state = {"run_id": "run-1", "repository": "example/project", "parent": {"number": 1}, "budget": 2}
    prepare_action_application_receipt(state, action)
    state["action_application_receipt"]["executor_generation"] += 1
    with pytest.raises(ValueError, match="ownership"):
        StateStore(tmp_path).save_run("run-1", state)
    assert StateStore(tmp_path).load_run("run-1") is None
    assert control.load(task) == before


def test_outer_control_rollback_invalidates_provisional_projection_and_notifications(git_repo: Path) -> None:
    from support.ticket_parallel import ticket_run
    state, store, git, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    control = TaskControlStore(store.root)
    task = TaskKey(git.root, state["repository"], 1)
    claim = control.claim_action(task, kind="run", payload={})
    action = claim.action
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id).generation
    save_owned(store, run_id, {"counter": 1}, fields={"counter"})
    objects = object_store(store)
    projection = objects.load_run(run_id)
    projection["counter"] = 2
    notifications = []
    store.run_saved_observer = notifications.append
    with pytest.raises(OSError, match="outer commit"):
        with lifecycle_commits(store.root), store.commit_admission(), control._executor_current_transaction(
            task, action_id=action["action_id"], generation=generation, run_id=run_id,
        ):
            objects.save_run(run_id, projection)
            raise OSError("outer commit failed")
    assert store.load_run(run_id)["counter"] == 1
    assert notifications == []
    with pytest.raises(ValueError, match="fresh read"):
        objects.save_run(run_id, projection)
    refreshed = objects.load_run(run_id)
    refreshed["counter"] = 2
    objects.save_run(run_id, refreshed)
    assert store.load_run(run_id)["counter"] == 2


def test_lifecycle_commit_does_not_wait_for_business_fifo(tmp_path: Path) -> None:
    control, task, action, generation = _reserved(tmp_path)
    store = StateStore(tmp_path)
    store.save_run("run-1", {"run_id": "run-1", "repository": "example/project",
                             "parent": {"number": 1}, "status": "active"})
    # A business caller owns the FIFO seat but holds no SQLite transaction.
    def stop() -> None:
        claim = control.claim_control_action(task, kind="stop", payload={}, run_id="run-1", state_dir=tmp_path)
        reserved = control.begin_executor(task, action_id=claim.action_id, run_id="run-1")
        stop_store = StateStore(tmp_path)
        stop_store._set_write_guard(None, transaction=partial(
            control._executor_current_transaction, task, action_id=claim.action_id,
            generation=reserved.generation, run_id="run-1",
        ))
        with lifecycle_commits(tmp_path):
            stopped = stop_store.load_run("run-1")
            stopped["status"] = "operator_stopped"
            prepare_action_application_receipt(stopped, claim.action)
            save_owned(stop_store, "run-1", stopped, fields={"status", "action_application_receipt"})
    control.bind_run(task, action["action_id"], "run-1", generation=generation)
    control.record_application(task, action_id=action["action_id"], generation=generation,
                               run_id="run-1", payload_digest=action["payload_digest"])
    control.complete_action(task, action_id=action["action_id"], generation=generation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.commit_admission():
            executor.submit(stop).result(timeout=3)
    assert store.load_run("run-1")["status"] == "operator_stopped"
    with pytest.raises(ActionReconciliationError):
        control.assert_executor_current(task, action_id=action["action_id"], generation=generation)


@pytest.mark.parametrize("version,supported", [
    ((3, 51, 2), False), ((3, 45, 1), False), ((3, 51, 3), True),
    ((3, 50, 7), True), ((3, 50, 6), False), ((3, 44, 6), True), ((3, 52, 0), True),
])
def test_wal_runtime_requires_verified_reset_fix(monkeypatch, tmp_path, version, supported) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    if supported:
        require_wal_runtime()
    else:
        with pytest.raises(IncompatibleRunStateError, match="WAL-reset"):
            StateStore(tmp_path / "unsupported").save_run("run", {})
        assert not (tmp_path / "unsupported").exists()


def test_legacy_control_rejected_and_preserved(tmp_path: Path) -> None:
    control = TaskControlStore(tmp_path)
    task = TaskKey(tmp_path, "example/project", 1)
    control.directory.mkdir()
    legacy = control.path_for(task)
    legacy.write_text('{"protocol":1}', encoding="utf-8")
    before = legacy.read_bytes()
    with pytest.raises(IncompatibleRunStateError, match="legacy Task Control"):
        control.claim_action(task, kind="run", payload={})
    assert legacy.read_bytes() == before
    assert not (tmp_path / "state.sqlite3").exists()
