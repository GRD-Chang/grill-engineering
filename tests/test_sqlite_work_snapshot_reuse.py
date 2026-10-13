"""Proved snapshot encodings still require current material and reference facts."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run import sqlite_objects
from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_snapshot_reuse_owns_independent_trees_and_run_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    body = {"body": "nested evidence" * 100, "number": 1e99}
    original = {
        "parent": body,
        "timeline": [{"review_context": body}, {"review_context": body}],
        "ticket_jobs": {"2": {"semantic_attempt_history": [
            {"role": "reviewer", "status": "completed", "ordinal": 1, "review_context": body},
        ]}},
    }
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    reused = []
    decode = sqlite_objects._decode

    def observe(*args, **kwargs):
        result = decode(*args, **kwargs)
        context = kwargs.get("context")
        if context is not None:
            reused.append(bool(context.snapshot_encodings))
        return result

    monkeypatch.setattr(sqlite_objects, "_decode", observe)
    current = repository.load_work("run-1", 2)
    assert current == original
    assert any(reused)
    current["timeline"][0]["review_context"]["body"] = "caller mutation"
    assert current["timeline"][1]["review_context"]["body"] == body["body"]
    assert repository.load_work("run-1", 2) == original
    with repository.atomic():
        owner = repository.load_work("run-1", 2)
        sibling = repository.load_work("run-1", 3)
        assert "semantic_attempt_history" in owner["ticket_jobs"]["2"]
        assert "semantic_attempt_history" not in sibling["ticket_jobs"]["2"]
    repository.save("run-2", original)
    assert repository.load("run-1") == original
    reused.clear()
    assert repository.load_work("run-2", 2) == original
    assert reused and not any(reused)


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["byte_length", "identity", "missing", "object_refs", "run_refs"])
def test_work_snapshot_reuse_rejects_current_material_damage(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    original = {"parent": {"body": "evidence" * 200},
                "timeline": [{"review_context": {"body": "evidence" * 200}}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "byte_length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "identity":
            # Preserve the byte count so SHA validation, rather than the length
            # guard, rejects the modified material before cached decoding.
            connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence')")
        elif damage == "missing":
            connection.execute("DELETE FROM run_materials WHERE material_id="
                               "(SELECT material_id FROM run_materials ORDER BY byte_length DESC LIMIT 1)")
        elif damage == "object_refs":
            # The sibling event retains the same materials. Presence alone
            # cannot stand in for this object's exact reference closure.
            connection.execute("DELETE FROM run_object_material_refs WHERE object_path='[\"parent\"]'")
        else:
            connection.execute("DELETE FROM run_material_refs WHERE run_id='run-1'")
        connection.commit()
    with pytest.raises(ValueError, match="material|persistence limit"):
        repository.load_work("run-1", 2)
