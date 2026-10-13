"""Sparse Timeline appends preserve canonical sizes, old evidence and full queries."""
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run import state as state_module, sqlite_timeline
from agent_run.sqlite_state import SQLiteStateRepository


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("change", ["empty", "append", "noop", "duplicate", "capacity", "continuation", "shift"])
def test_sparse_timeline_matches_complete_append_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool, change: str,
) -> None:
    monkeypatch.setattr(state_module, "datetime", _Clock)
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    count = 0 if change == "empty" else 255 if change == "capacity" else 256 if change in {"continuation", "shift"} else 10
    original = {"status": "active", "timeline": [{"summary": f"old {i}"} for i in range(count)],
                "agent_invocation_history": [{"work_subject": "ticket:3", "started_at": "historical"}]}
    if change in {"continuation", "shift"}:
        original["timeline_at_capacity"] = True
        original["timeline_continuation"] = [{"summary": f"continuation {i}"} for i in range(256 if change == "shift" else 5)]
        original["timeline_continuation"][0]["review_context"] = {"body": "old removed evidence" * 100}
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
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    with repository.atomic():
        baseline = repository.load_work("run-1", 2, thin_timeline=True)
        scopes = repository.work_timelines()
        assert "timeline" in scopes and "timeline" not in baseline
        current = deepcopy(baseline)
        current["status"] = expected["status"]
        updates = sqlite_timeline.append_events(current, baseline, scopes)
        repository.save_work("run-1", current, baseline, timeline_updates=updates)
        complete = repository.load_work("run-1", 2)
        assert complete["timeline"] == expected["timeline"]
        assert complete.get("timeline_continuation") == expected.get("timeline_continuation")
        assert repository.work_timelines() == {}
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == expected
    with closing(sqlite3.connect(repository.path)) as connection:
        materials = {row[0] for row in connection.execute("SELECT material_id FROM run_materials")}
        references = {row[0] for row in connection.execute("SELECT material_id FROM run_object_material_refs")}
        assert materials == references
        size = connection.execute("SELECT logical_bytes FROM run_sizes").fetchone()[0]
        assert size == repository._check_size(expected)
        assert connection.execute("SELECT value_json FROM run_objects WHERE object_path='[\"timeline\"]'").fetchone() == (str(len(expected["timeline"])),)
        assert connection.execute("SELECT value_json FROM run_objects WHERE object_path='[\"agent_invocation_history\"]'").fetchone() == ("1",)
        assert connection.execute("SELECT COUNT(*) FROM run_history").fetchone() == (1,)


@pytest.mark.parametrize("aggregate", [True, False])
def test_thin_timeline_requires_matching_complete_proof_and_absent_field(
    tmp_path: Path, aggregate: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"timeline": [{"summary": "old"}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    # No complete view exists yet: the request returns the ordinary full field.
    assert repository.load_work("run-1", 2, thin_timeline=True) == original
    assert repository.work_timelines() == {}
    assert repository.load("run-1") == original
    with repository.atomic():
        baseline = repository.load_work("run-1", 2, thin_timeline=True)
        scope = repository.work_timelines()["timeline"]
        tail = scope.tail()
        tail["summary"] = "caller mutation"
        assert scope.tail() == original["timeline"][-1]
    with pytest.raises(ValueError, match="unloaded Timeline"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2, thin_timeline=True)
            repository.save_work("run-1", {**baseline, "timeline": []}, baseline)
    changed = {"timeline": [{"summary": "actual changed object"}]}
    repository.save("run-1", changed)
    assert repository.load_work("run-1", 2, thin_timeline=True) == changed
    assert repository.work_timelines() == {}
    repository.save("run-2", changed)
    assert repository.load("run-1") == changed
    assert repository.load_work("run-2", 2, thin_timeline=True) == changed
    assert repository.work_timelines() == {}


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["identity", "bytes", "missing", "refs", "root", "child"])
def test_thin_timeline_retains_current_damage_checks(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"timeline": [{"summary": "evidence"}, {"summary": "evidence"}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "identity":
            connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence')")
        elif damage == "bytes":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "missing":
            connection.execute("DELETE FROM run_materials")
        elif damage == "refs":
            connection.execute("DELETE FROM run_object_material_refs WHERE object_path='[\"timeline\",0]'")
        elif damage == "root":
            connection.execute("UPDATE run_objects SET value_json='3' WHERE object_path='[\"timeline\"]'")
        else:
            connection.execute("DELETE FROM run_objects WHERE object_path='[\"timeline\",0]'")
        connection.commit()
    with pytest.raises(ValueError, match="material|list item"):
        repository.load_work("run-1", 2, thin_timeline=True)


@pytest.mark.parametrize("aggregate", [True, False])
def test_thin_timeline_counts_canonical_bytes_and_rejects_changed_transaction(
    tmp_path: Path, aggregate: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"timeline": [{"number": 1e99}]}
    repository.save("run-1", original)
    with repository.atomic():
        connection = repository._local.connection
        old, text = connection.execute("SELECT material_id,value FROM run_materials").fetchone()
        text = text.replace("1e+99", "1e99")
        identity = hashlib.sha256(text.encode()).hexdigest()
        connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, text, len(text.encode())))
        connection.execute("UPDATE run_objects SET value_json=? WHERE kind='snapshot'", (json.dumps(identity),))
        for table in ("run_material_refs", "run_object_material_refs"):
            connection.execute(f"UPDATE {table} SET material_id=? WHERE material_id=?", (identity, old))
        connection.execute("DELETE FROM run_materials WHERE material_id=?", (old,))
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    with pytest.raises(ValueError, match="current transaction baseline"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2, thin_timeline=True)
            scope = repository.work_timelines()["timeline"]
            assert "1e+99" in scope.encoded
            assert repository._check_size(baseline, unloaded_fields={"timeline": scope.encoded}) == repository._check_size(original)
            repository._local.connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
            repository.save_work("run-1", baseline, baseline, timeline_updates={"timeline": {"number": 2}})
    assert repository.load("run-1") == original
    repository.max_bytes = repository._check_size(original) - 1
    with pytest.raises(ValueError, match="persistence limit|size metadata"):
        repository.load_work("run-1", 2, thin_timeline=True)


@pytest.mark.parametrize("aggregate", [True, False])
def test_timeline_descendant_override_keeps_complete_fallback(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"timeline": [{"summary": "old"}]})
    overridden = {"timeline": [{"summary": "actual descendant"}]}
    with repository.atomic():
        connection = repository._local.connection
        connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)",
                           ("run-1", '["timeline",0,"summary"]', "scalar", json.dumps("actual descendant")))
        connection.execute("UPDATE run_sizes SET logical_bytes=?", (repository._check_size(overridden),))
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == overridden
    assert repository.load_work("run-1", 2, thin_timeline=True) == overridden
    assert repository.work_timelines() == {}
