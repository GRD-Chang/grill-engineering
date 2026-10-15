"""Object mutations retain old evidence without rewriting its materials."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run import sqlite_mutations


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("retained", [30, 200])
def test_timeline_append_only_mutates_changed_objects_materials(
    tmp_path: Path, aggregate: bool, retained: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    original = {
        "ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"}},
        "timeline": [{"summary": f"saved event {index}", "evidence": "saved evidence " * 100}
                     for index in range(retained)],
    }
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    writes: list[str] = []
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        connection = repository._local.connection
        prior_event_materials = {
            json.loads(value) for path, value in connection.execute(
                "SELECT object_path,value_json FROM run_objects WHERE run_id='run-1' AND kind='snapshot'"
            ) if json.loads(path)[:1] == ["timeline"]
        }
        updated = deepcopy(baseline)
        updated["ticket_jobs"]["2"]["phase"] = "candidate"
        updated["timeline"].append({"summary": "new event", "evidence": "saved evidence " * 100})
        timeline_inputs = []
        timeline_encodings = []
        encoded_bytes = []
        real_objects = repository._objects
        real_encode = sqlite_mutations.encode_json
        real_dumps = json.dumps
        def objects(state, **kwargs):
            if "timeline" in state:
                timeline_inputs.append(deepcopy(state["timeline"]))
            return real_objects(state, **kwargs)
        def encode(value):
            if value is baseline["timeline"] or value is updated["timeline"]:
                timeline_encodings.append(value)
            return real_encode(value)
        def dumps(*args, **kwargs):
            result = real_dumps(*args, **kwargs)
            encoded_bytes.append(len(result.encode("utf-8")))
            return result
        monkeypatch.setattr(repository, "_objects", objects)
        monkeypatch.setattr(sqlite_mutations, "encode_json", encode)
        monkeypatch.setattr(json, "dumps", dumps)
        connection.set_trace_callback(writes.append)
        try:
            repository.save_work("run-1", updated, baseline)
        finally:
            connection.set_trace_callback(None)
    material_inserts = "\n".join(sql for sql in writes if sql.startswith("INSERT OR IGNORE INTO run_materials"))
    assert prior_event_materials
    assert timeline_inputs == [[updated["timeline"][-1]]]
    assert timeline_encodings == []
    assert sum(encoded_bytes) < 16 * len(real_dumps(updated["timeline"][-1]).encode("utf-8"))
    assert not any(identity in material_inserts for identity in prior_event_materials)
    assert SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1") == updated


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("field", ["timeline", "timeline_continuation"])
@pytest.mark.parametrize("change", ["empty_append", "append", "remove", "clear", "replace", "capacity_shift", "shape_change"])
def test_timeline_position_changes_keep_exact_size_references_and_reopen(
    tmp_path: Path, aggregate: bool, field: str, change: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    count = 256 if change == "capacity_shift" else 0 if change == "empty_append" else 3
    original = {field: [{"summary": f"event {index}", "review_context": {"body": f"evidence {index}" * 120}}
                        for index in range(count)]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        updated = deepcopy(baseline)
        events = updated[field]
        fresh = {"summary": "new", "review_context": {"body": "new evidence" * 120}}
        if change in {"empty_append", "append"}:
            events.append(fresh)
        elif change == "remove":
            events.pop()
        elif change == "clear":
            events.clear()
        elif change == "replace":
            events[1] = fresh
        elif change == "capacity_shift":
            events.pop(0)
            events.append(fresh)
        else:
            events[1] = ["scalar", {"nested": "changed" * 200}]
        repository.save_work("run-1", updated, baseline)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == updated
    with reopened.atomic():
        connection = reopened._local.connection
        materials = {row[0] for row in connection.execute("SELECT material_id FROM run_materials")}
        references = {row[0] for row in connection.execute("SELECT material_id FROM run_object_material_refs")}
        assert materials == references


@pytest.mark.parametrize("aggregate", [True, False])
def test_timeline_patch_requires_unchanged_transaction_objects(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"timeline": [{"summary": "old"}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="current transaction baseline"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            assert baseline is not None
            updated = deepcopy(baseline)
            updated["timeline"].append({"summary": "new"})
            repository._local.connection.execute(
                "DELETE FROM run_objects WHERE run_id='run-1' AND object_path='[\"timeline\",0]'",
            )
            repository.save_work("run-1", updated, baseline)
    assert repository.load("run-1") == original


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_rejects_oversized_material_before_restoring_its_body(
    tmp_path: Path, aggregate: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"parent": {"body": "frozen requirement"}})
    with repository.atomic():
        repository._local.connection.execute(
            "UPDATE run_materials SET value=?,byte_length=?", ("damaged" * 10000, 70000),
        )
    repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="byte length|persistence limit"):
        repository.load_work("run-1", 2)
