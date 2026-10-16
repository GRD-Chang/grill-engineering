"""明确 Timeline 查询与增量追加保留历史、容量、事务和原始证据。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from agent_run import state as state_module, sqlite_timeline
from agent_run.sqlite_state import SQLiteStateRepository


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, tzinfo=UTC)


def _event(number: int, **facts) -> dict:
    return {"at": f"2026-10-15T00:{number // 60:02}:{number % 60:02}+00:00",
            "kind": "run_status", "status": "active", "result": f"retained event {number}", **facts}


@pytest.mark.parametrize("change", ["empty", "append", "noop", "duplicate", "capacity", "continuation", "shift"])
def test_sparse_timeline_matches_complete_append_and_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    monkeypatch.setattr(state_module, "datetime", _Clock)
    repository = SQLiteStateRepository(tmp_path)
    count = 0 if change == "empty" else 255 if change == "capacity" else 256 if change in {"continuation", "shift"} else 10
    original = {"status": "active", "timeline": [_event(number) for number in range(count)],
                "agent_invocation_history": [{"work_subject": "ticket:3", "started_at": "historical", "status": "completed"}]}
    if change in {"continuation", "shift"}:
        original["timeline_at_capacity"] = True
        original["timeline_continuation"] = [_event(number, result=f"continuation {number}")
                                              for number in range(256 if change == "shift" else 5)]
        original["timeline_continuation"][0]["graph_change_summary"] = {"summary": "old removed evidence" * 100}
    if change == "duplicate":
        duplicate = deepcopy(original)
        duplicate["status"] = "blocked"
        state_module._append_timeline_event(duplicate, original)
        original["timeline"][-1] = duplicate["timeline"][-1]
    expected = deepcopy(original)
    if change != "noop":
        expected["status"] = "blocked"
    state_module._append_timeline_event(expected, original)
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2, thin_timeline=True)
        scopes = repository.work_timelines()
        assert "timeline" in scopes and "timeline" not in baseline
        current = deepcopy(baseline)
        current["status"] = expected["status"]
        updates = sqlite_timeline.append_events(current, baseline, scopes)
        repository.save_work("run-1", current, baseline, timeline_updates=updates)
    assert SQLiteStateRepository(tmp_path).load("run-1") == expected


def test_thin_timeline_does_not_allow_replacing_unread_events(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"timeline": [_event(0)]}
    repository.save("run-1", original)
    assert repository.load_work("run-1", 2, thin_timeline=True) == {}
    scope = repository.work_timelines()["timeline"]
    assert scope.count == 1 and scope.tail() == original["timeline"][-1]
    with pytest.raises(ValueError, match="unread Timeline|unloaded Timeline"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2, thin_timeline=True)
            repository.save_work("run-1", {**baseline, "timeline": []}, baseline)
    assert repository.load("run-1") == original


def test_timeline_scope_is_fresh_and_its_nested_tail_is_independent(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"timeline": [_event(0, graph_change_summary={"summary": "原始🙂\x00", "added_tickets": [2]})]}
    repository.save("run-1", original)
    assert repository.load_work("run-1", 2, thin_timeline=True) == {}
    scope = repository.work_timelines()["timeline"]
    tail = scope.tail()
    tail["graph_change_summary"]["added_tickets"].append(99)
    assert scope.tail() == original["timeline"][-1]
    added = _event(1, graph_change_summary={"summary": "新增🙂\x00", "added_tickets": [3]})
    SQLiteStateRepository(tmp_path).append_array("run-1", "timeline", [added])
    assert repository.load_work("run-1", 2, thin_timeline=True) == {}
    fresh = repository.work_timelines()["timeline"]
    assert fresh.count == 2 and fresh.tail() == added
    assert repository.load("run-1") == {"timeline": [*original["timeline"], added]}


@pytest.mark.parametrize("damage", ["body", "material_length", "missing_material", "event", "missing_event", "oversized_position"])
def test_thin_timeline_rechecks_current_event_and_material(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"timeline": [_event(0, graph_change_summary={"summary": "original evidence"})]})
    assert repository.load_work("run-1", 2, thin_timeline=True) == {}
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        if damage == "body":
            connection.execute("UPDATE work_evidence_materials SET body=replace(body,'original','Original')")
        elif damage == "material_length":
            connection.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1")
        elif damage == "missing_material":
            connection.execute("DELETE FROM work_evidence_materials")
        elif damage == "event":
            connection.execute("UPDATE run_timeline_events SET value_status='different'")
        elif damage == "missing_event":
            connection.execute("DELETE FROM run_timeline_events")
        else:
            connection.execute("UPDATE run_timeline_slots SET position=999999999")
    with pytest.raises(ValueError, match="material|evidence|Timeline"):
        repository.load_work("run-1", 2, thin_timeline=True)


def test_timeline_append_preserves_unread_historical_material(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"status": "active", "timeline": [_event(0, graph_change_summary={"summary": "old exact evidence"}), _event(1)]}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        identity, body = connection.execute("SELECT material_id,body FROM work_evidence_materials").fetchone()
        connection.execute("UPDATE work_evidence_materials SET body='broken' WHERE material_id=?", (identity,))
    with repository.atomic():
        baseline = repository.load_work("run-1", 2, thin_timeline=True)
        changed = deepcopy(baseline)
        changed["status"] = "blocked"
        updates = sqlite_timeline.append_events(changed, baseline, repository.work_timelines())
        repository.save_work("run-1", changed, baseline, timeline_updates=updates)
    with pytest.raises(ValueError, match="evidence|material"):
        repository.load("run-1")
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        assert connection.execute("SELECT body FROM work_evidence_materials WHERE material_id=?", (identity,)).fetchone() == ("broken",)
        connection.execute("UPDATE work_evidence_materials SET body=? WHERE material_id=?", (body, identity))
    expected = deepcopy(original)
    expected["status"] = "blocked"
    state_module._append_timeline_event(expected, original)
    # The event timestamp belongs to the actual append; all earlier facts are exact.
    saved = repository.load("run-1")
    expected["timeline"][-1]["at"] = saved["timeline"][-1]["at"]
    assert saved == expected


def test_changed_transaction_cannot_reuse_timeline_baseline(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"status": "active", "timeline": [_event(0)]}
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="current transaction baseline"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2, thin_timeline=True)
            repository._local.connection.execute("UPDATE runs SET status='blocked' WHERE run_id='run-1'")
            repository.save_work("run-1", baseline, baseline, timeline_updates={"timeline": _event(1)})
    assert repository.load("run-1") == original
