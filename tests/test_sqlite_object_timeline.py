"""真实对象提交只读 Timeline 末项，完整历史、通知及失败边界保持有效。"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent_run import executor as executor_module, sqlite_timeline, state as state_module
from agent_run.executor import DeliveryExecutor
from agent_run.object_projection import ObjectProjection
from agent_run.object_state import current_projection_facts, object_store, save_owned
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state import ConcurrentRunStateError, StateStore
from agent_run.state_observation import CurrentRunObserver
from support.state import write_run_fixture
from support.ticket_parallel import ticket_run


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, tzinfo=UTC)


def _event(number: int, **facts) -> dict:
    return {"at": f"2026-10-15T00:{number // 60:02}:{number % 60:02}+00:00",
            "kind": "run_status", "status": "active", "result": f"retained event {number}", **facts}


def _run(git_repo: Path, *, count: int = 10, continuation: int | None = None):
    state, states, _git, _fixture = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    state = states.load_current_run(run_id)
    assert state is not None
    state["timeline"] = [_event(number) for number in range(count)]
    if count:
        state["timeline"][0]["graph_change_summary"] = {"summary": "原始证据🙂\x00", "added_tickets": [2]}
    if continuation is not None:
        state["timeline_at_capacity"] = True
        state["timeline_continuation"] = [_event(number, result=f"continuation {number}") for number in range(continuation)]
        if continuation:
            state["timeline_continuation"][0]["graph_change_summary"] = {"summary": "released evidence"}
    write_run_fixture((states.root, run_id), state)
    return state, states


def _old_reads(states: StateStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    with closing(sqlite3.connect(states._repository.path)) as connection:
        identities = {row[0] for row in connection.execute("SELECT event_id FROM run_timeline_events")}
    read = sqlite_timeline._read
    seen = []

    def counted(connection, run_id, event_id):
        if event_id in identities:
            seen.append(event_id)
        return read(connection, run_id, event_id)

    monkeypatch.setattr(sqlite_timeline, "_read", counted)
    return seen


def _timeline_material(connection: sqlite3.Connection, run_id: str) -> tuple[str, str]:
    row = connection.execute(
        "SELECT m.material_id,m.body FROM run_timeline_slots s "
        "JOIN work_evidence e ON e.run_id=s.run_id "
        "AND e.owner_id='run:'||s.run_id||':timeline:'||s.event_id "
        "JOIN work_evidence_materials m ON m.material_id=e.material_id "
        "WHERE s.run_id=? AND s.stream='timeline' AND s.position=0 AND e.kind='graph_change_summary'",
        (run_id,),
    ).fetchone()
    assert row is not None
    return row


@pytest.mark.parametrize("change", ["empty", "append", "noop", "duplicate", "capacity", "continuation", "shift"])
def test_object_timeline_matches_complete_append_with_bounded_old_reads(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    monkeypatch.setattr(state_module, "datetime", _Clock)
    count = 0 if change == "empty" else 255 if change == "capacity" else 256 if change in {"continuation", "shift"} else 10
    continuation = 256 if change == "shift" else 5 if change == "continuation" else None
    original, states = _run(git_repo, count=count, continuation=continuation)
    run_id = original["run_id"]
    if change == "duplicate":
        duplicate = deepcopy(original)
        duplicate["status"] = "blocked"
        state_module._append_timeline_event(duplicate, original)
        original["timeline"][-1] = duplicate["timeline"][-1]
        write_run_fixture((states.root, run_id), original)
    expected = deepcopy(original)
    if change != "noop":
        expected["status"] = "blocked"
    state_module._append_timeline_event(expected, original)

    with monkeypatch.context() as bounded:
        seen = _old_reads(states, bounded)
        adapter = object_store(states)
        current = adapter.load_current_run(run_id)
        assert isinstance(current, ObjectProjection) and "timeline" not in current
        current["status"] = expected["status"]
        adapter.save_run(run_id, current)
        assert len(seen) <= 6, "ordinary object save expanded retained Timeline events"
        assert "timeline" not in current and "timeline_continuation" not in current
        assert current.timelines["timeline"].count == len(expected["timeline"])
        assert current_projection_facts(current) == current_projection_facts(expected)
    assert StateStore(states.root).load_current_run(run_id) == expected


def test_object_scope_survives_copy_deepcopy_and_readback_update(git_repo: Path) -> None:
    original, states = _run(git_repo, count=1)
    run_id = original["run_id"]
    adapter = object_store(states)
    for copying in (lambda value: value.copy(), deepcopy, _updated):
        loaded = adapter.load_current_run(run_id)
        assert isinstance(loaded, ObjectProjection)
        current = copying(loaded)
        assert current.timelines == loaded.timelines and "timeline" not in current
        current.timelines["timeline"].latest["graph_change_summary"]["added_tickets"].append(99)
        assert loaded.timelines["timeline"].tail()["graph_change_summary"]["added_tickets"] == [2]
        current["updated_at"] = "2026-10-15T00:00:01+00:00"
        adapter.save_run(run_id, current)
        assert current.timelines["timeline"].tail() == original["timeline"][-1]
    assert StateStore(states.root).load_current_run(run_id)["timeline"] == original["timeline"]


def _updated(value: ObjectProjection) -> ObjectProjection:
    updated = ObjectProjection()
    updated.update(value)
    return updated


def test_unread_timeline_cannot_be_replaced_but_owned_complete_input_remains_supported(git_repo: Path) -> None:
    original, states = _run(git_repo)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    assert current is not None
    current["timeline"] = []
    with pytest.raises(ValueError, match="unread Timeline"):
        adapter.save_run(run_id, current)
    current.pop("timeline")
    with pytest.raises(ValueError, match="fresh read"):
        adapter.save_run(run_id, current)
    current = adapter.load_current_run(run_id)
    with pytest.raises(ValueError, match="unread Timeline"):
        save_owned(states, run_id, current, fields={"timeline"})
    assert StateStore(states.root).load_current_run(run_id) == original
    replacement = [_event(100, result="explicit complete Timeline")]
    save_owned(states, run_id, {"timeline": replacement}, fields={"timeline"})
    assert StateStore(states.root).load_current_run(run_id) == {**original, "timeline": replacement}
    oversized = [_event(101, graph_change_summary={"summary": "x" * (4 * 1024 * 1024)})]
    with pytest.raises(ValueError, match="evidence exceeds|byte limit"):
        save_owned(states, run_id, {"timeline": oversized}, fields={"timeline"})
    assert StateStore(states.root).load_current_run(run_id) == {**original, "timeline": replacement}


def test_owned_current_commit_appends_without_replacing_unread_streams(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, states = _run(git_repo, count=128)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    current["status"] = "blocked"
    with monkeypatch.context() as bounded:
        seen = _old_reads(states, bounded)
        save_owned(states, run_id, current, fields={"status"})
        assert len(seen) <= 3
    assert current.timelines["timeline"].count == 129 and "timeline" not in current
    current["updated_at"] = "2026-10-15T00:00:01+00:00"
    adapter.save_run(run_id, current)
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved["timeline"][:128] == original["timeline"]
    assert len(saved["timeline"]) == 129 and saved["status"] == "blocked"


@pytest.mark.parametrize("observer", ["disabled", "current", "custom"])
def test_old_material_stays_unread_and_observer_failure_cannot_rollback_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, observer: str,
) -> None:
    monkeypatch.setattr(state_module, "datetime", _Clock)
    original, states = _run(git_repo)
    run_id = original["run_id"]
    with closing(sqlite3.connect(states._repository.path)) as connection, connection:
        identity, body = _timeline_material(connection, run_id)
        connection.execute("UPDATE work_evidence_materials SET body='broken' WHERE material_id=?", (identity,))
    observations = []

    def observe(state):
        assert not states._repository.in_transaction and states._lock_depth == 0
        observations.append(state)

    states.run_saved_observer = (CurrentRunObserver(observe) if observer == "current"
                                 else observe if observer == "custom" else None)
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    current["status"] = "blocked"
    adapter.save_run(run_id, current)
    assert len(observations) == (1 if observer == "current" else 0)
    assert ("Run observer failed after durable commit" in caplog.text) == (observer == "custom")
    with pytest.raises(ValueError, match="evidence|material"):
        StateStore(states.root).load_current_run(run_id)
    with closing(sqlite3.connect(states._repository.path)) as connection, connection:
        assert connection.execute("SELECT body FROM work_evidence_materials WHERE material_id=?", (identity,)).fetchone() == ("broken",)
        connection.execute("UPDATE work_evidence_materials SET body=? WHERE material_id=?", (body, identity))
    expected = {**deepcopy(original), "status": "blocked"}
    state_module._append_timeline_event(expected, original)
    assert StateStore(states.root).load_current_run(run_id) == expected


@pytest.mark.parametrize("damage", ["body", "material_length", "missing_material", "event", "missing_event", "oversized_position"])
def test_object_save_rechecks_the_actual_current_tail_and_rejects_external_damage(
    git_repo: Path, damage: str,
) -> None:
    original, states = _run(git_repo, count=1)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    with closing(sqlite3.connect(states._repository.path)) as connection, connection:
        identity, body = _timeline_material(connection, run_id)
        if damage == "body":
            connection.execute("UPDATE work_evidence_materials SET body=replace(body,'原始','改动') WHERE material_id=?", (identity,))
        elif damage == "material_length":
            connection.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1 WHERE material_id=?", (identity,))
        elif damage == "missing_material":
            connection.execute("DELETE FROM work_evidence_materials WHERE material_id=?", (identity,))
        elif damage == "event":
            connection.execute("UPDATE run_timeline_events SET value_status='different'")
        elif damage == "missing_event":
            connection.execute("DELETE FROM run_timeline_events")
        else:
            connection.execute("UPDATE run_timeline_slots SET position=999999999")
        damaged = tuple(connection.iterdump())
    current["status"] = "blocked"
    with pytest.raises(ValueError, match="material|evidence|Timeline"):
        adapter.save_run(run_id, current)
    with pytest.raises(ValueError, match="material|evidence|Timeline"):
        adapter.load_current_run(run_id)
    with closing(sqlite3.connect(states._repository.path)) as connection:
        assert tuple(connection.iterdump()) == damaged


def test_object_save_keeps_concurrent_suffix_and_refreshes_its_scope(git_repo: Path) -> None:
    original, states = _run(git_repo)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    concurrent = SQLiteStateRepository(states.root)
    extra = _event(99, result="another transaction")
    concurrent.append_array(run_id, "timeline", [extra])
    current["updated_at"] = "2026-10-15T00:00:01+00:00"
    adapter.save_run(run_id, current)
    assert current.timelines["timeline"].count == 11
    assert current.timelines["timeline"].tail() == extra
    assert StateStore(states.root).load_current_run(run_id)["timeline"] == [*original["timeline"], extra]
    with adapter.require_unchanged_run(run_id, current):
        concurrent.append_array(run_id, "timeline", [_event(100)])
        with pytest.raises(ConcurrentRunStateError, match="before Controller returned"):
            adapter.load_unchanged_run(run_id)


def test_timeline_append_rejects_sql_changes_since_its_transaction_read(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"status": "active", "timeline": [_event(0)]}
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="current transaction"):
        with repository.atomic():
            current, _ = repository.load_objects("run-1", thin_timeline=True)
            repository._local.connection.execute("UPDATE runs SET status='blocked' WHERE run_id='run-1'")
            repository.save_objects("run-1", current, fields=set(), timeline_updates={"timeline": _event(1)})
    assert repository.load("run-1") == original


@pytest.mark.parametrize("failure", ["record_limit", "outer_rollback"])
def test_object_timeline_rollback_preserves_storage_and_invalidates_the_carrier(git_repo: Path, failure: str) -> None:
    original, states = _run(git_repo)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    observed = []
    states.run_saved_observer = observed.append
    current["status"] = "blocked"
    if failure == "record_limit":
        current["phase"] = "x" * (256 * 1024)
        with pytest.raises(ValueError, match="record exceeds|byte limit"):
            adapter.save_run(run_id, current)
    else:
        def interrupted():
            adapter.save_run(run_id, current)
            raise OSError("outer transaction interrupted")

        with pytest.raises(OSError, match="outer transaction interrupted"):
            states._commit_run(interrupted)
    assert observed == []
    assert StateStore(states.root).load_current_run(run_id) == original
    with pytest.raises(ValueError, match="fresh read"):
        adapter.save_run(run_id, current)
    fresh = adapter.load_current_run(run_id)
    fresh["status"] = "blocked"
    adapter.save_run(run_id, fresh)
    assert len(observed) == 1
    assert observed[0]["timeline"] == StateStore(states.root).load_current_run(run_id)["timeline"]


@pytest.mark.parametrize("mode", ["disabled", "notifications", "feedback"])
def test_executor_uses_current_observation_without_expanding_the_timeline(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    original, states = _run(git_repo, count=128)
    run_id = original["run_id"]
    observed = []
    closed = []

    def record_observation(state):
        assert not states._repository.in_transaction
        observed.append(state)

    class Notifications:
        enabled = mode == "notifications"

        def __init__(self, *args, **kwargs):
            pass

        observe = staticmethod(record_observation)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(executor_module, "Notifications", Notifications)
    feedback = (SimpleNamespace(observe=record_observation, project=lambda state: state,
                                attach=lambda *args: None, failure=lambda *args, **kwargs: None)
                if mode == "feedback" else None)

    def factory(actual, binding):
        adapter = object_store(actual)

        def advance(state):
            assert isinstance(state, ObjectProjection) and "timeline" not in state
            for status in ("blocked", "active"):
                state["status"] = status
                adapter.save_run(run_id, state)
            return state

        return SimpleNamespace(advance=advance)

    with monkeypatch.context() as bounded:
        seen = _old_reads(states, bounded)
        connections = []
        connect = sqlite3.connect

        def capture(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection

        bounded.setattr(sqlite3, "connect", capture)
        executor = DeliveryExecutor(states=states, driver_factory=factory, resume_feedback=feedback)
        assert executor.execute(run_id)["run_id"] == run_id
        assert len(seen) <= 8, "Executor expanded a retained Timeline for ordinary saves"
    assert closed == [True] and states.run_saved_observer is None
    assert bool(observed) == (mode != "disabled")
    assert all(len(state.get("timeline", [])) <= 1 for state in observed)
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved["timeline"][:128] == original["timeline"]
    assert [event["status"] for event in saved["timeline"][-2:]] == ["blocked", "active"]


@pytest.mark.parametrize("outer", ["admission", "transaction"])
def test_custom_observations_keep_each_commits_current_facts_and_timeline_together(
    git_repo: Path, outer: str,
) -> None:
    original, states = _run(git_repo)
    run_id = original["run_id"]
    adapter = object_store(states)
    observed, expected = [], []

    def observe(current):
        assert not states._repository.in_transaction and states._lock_depth == 0
        observed.append(current)

    states.run_saved_observer = observe

    def save_twice():
        for status in ("blocked", "active"):
            current = adapter.load_current_run(run_id)
            current["status"] = status
            adapter.save_run(run_id, current)
            expected.append(states.load_current_run(run_id))
            assert observed == []

    if outer == "admission":
        with states.commit_admission():
            save_twice()
    else:
        states._commit_run(save_twice)
    assert len(observed) == 2
    for current, saved in zip(observed, expected, strict=True):
        assert current["status"] == saved["status"]
        assert current["timeline"] == saved["timeline"]
    assert observed[0]["timeline"] != observed[1]["timeline"]


@pytest.mark.parametrize("rolled_back", [False, True])
def test_custom_observation_preparation_cannot_mask_a_lost_sqlite_transaction(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rolled_back: bool,
) -> None:
    original, states = _run(git_repo)
    run_id = original["run_id"]
    adapter = object_store(states)
    current = adapter.load_current_run(run_id)
    current["status"] = "blocked"
    observed = []
    states.run_saved_observer = observed.append

    def failed_query(*args, **kwargs):
        if rolled_back:
            states._repository._local.connection.rollback()
        raise OSError("observation query failed")

    with monkeypatch.context() as failing:
        failing.setattr(states._repository, "load_fields", failed_query)
        if rolled_back:
            with pytest.raises(OSError, match="observation query failed"):
                adapter.save_run(run_id, current)
        else:
            adapter.save_run(run_id, current)
    saved = states.load_current_run(run_id)
    assert saved["status"] == ("active" if rolled_back else "blocked")
    assert observed == []
    assert ("Run observer failed after durable commit" in caplog.text) is not rolled_back
