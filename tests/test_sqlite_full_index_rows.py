"""Full index checks retain actual SQLite types and complete references."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run import sqlite_objects


def _repository(path: Path) -> SQLiteStateRepository:
    return SQLiteStateRepository(path, max_bytes=4 * 1024 * 1024)


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_size_reuse_counts_restored_json_and_checks_current_materials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool,
) -> None:
    repository = _repository(tmp_path)
    original = {"timeline": [{"number": 1e99, "text": "中文"}]}
    repository.save("run-1", original)
    with repository.atomic():
        connection = repository._local.connection
        path = '["timeline",0]'
        old = json.loads(connection.execute(
            "SELECT value_json FROM run_objects WHERE object_path=?", (path,),
        ).fetchone()[0])
        text = connection.execute("SELECT value FROM run_materials WHERE material_id=?", (old,)).fetchone()[0]
        text = text.replace("1e+99", "1e99")
        identity = hashlib.sha256(text.encode()).hexdigest()
        connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, text, len(text.encode())))
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", (json.dumps(identity), path))
        connection.execute("UPDATE run_object_material_refs SET material_id=? WHERE material_id=?", (identity, old))
        connection.execute("UPDATE run_material_refs SET material_id=? WHERE material_id=?", (identity, old))
        connection.execute("DELETE FROM run_materials WHERE material_id=?", (old,))
    assert repository.load("run-1") == original
    repository._aggregate_rows = aggregate
    check_size = repository._check_size
    reused = []
    def observe_size(state, **kwargs):
        fields = kwargs.get("encoded_fields", {})
        if "timeline" in fields:
            reused.append(fields["timeline"])
            assert check_size(state, **kwargs) == check_size(state)
        return check_size(state, **kwargs)
    monkeypatch.setattr(repository, "_check_size", observe_size)
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == original
        view = repository._local.work_view
        # This field comes from the full read's canonical restored encoding,
        # while the current material intentionally has fewer numeric bytes.
        assert '1e+99' in repository._read_view.view.fields["timeline"]
        assert view.logical_bytes == repository._check_size(current)
    assert reused and all("1e+99" in text for text in reused)
    with repository.atomic():
        repository._local.connection.execute("DELETE FROM run_object_material_refs WHERE object_path=?", (path,))
    with pytest.raises(ValueError, match="material references"):
        repository.load_work("run-1", 2)


@pytest.mark.parametrize("aggregate", [True, False])
def test_complete_query_reuses_proved_closures_but_rechecks_actual_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool,
) -> None:
    repository = _repository(tmp_path)
    original = {"parent": {"body": "frozen requirement" * 100},
                "timeline": [{"summary": "evidence", "review_context": {"body": "review evidence" * 100}}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    restored_again = []
    references = sqlite_objects._references
    def observe(repository, kind, encoded, materials):
        if kind == "snapshot":
            restored_again.append(encoded)
        return references(repository, kind, encoded, materials)
    monkeypatch.setattr(sqlite_objects, "_references", observe)
    assert repository.load("run-1") == original
    assert repository.load("run-1") == original
    assert restored_again == []
    with repository.atomic():
        connection = repository._local.connection
        identity = connection.execute(
            "SELECT material_id FROM run_object_material_refs WHERE object_path='[\"parent\"]' "
            "AND material_id!=(SELECT json_extract(value_json,'$') FROM run_objects WHERE object_path='[\"parent\"]')",
        ).fetchone()[0]
        connection.execute("DELETE FROM run_object_material_refs WHERE run_id='run-1' AND material_id=?", (identity,))
    with pytest.raises(ValueError, match="material references"):
        repository.load("run-1")


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["history_blob", "refs_blob", "attempt_blob"])
def test_full_indexes_reject_blob_values_without_json_coercion(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = _repository(tmp_path)
    original = {"agent_invocation_history": [{"work_subject": "ticket:2", "started_at": "original"}],
                "ticket_jobs": {"2": {"semantic_attempt_history": [{"role": "reviewer", "status": "completed", "ordinal": 1}]}}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "history_blob":
            connection.execute("UPDATE run_history SET work_subject=?", (sqlite3.Binary(b"ticket:2"),))
        elif damage == "refs_blob":
            connection.execute("UPDATE run_object_material_refs SET material_id=CAST(material_id AS BLOB)")
        else:
            connection.execute("UPDATE run_job_attempts SET role=?", (sqlite3.Binary(b"reviewer"),))
        connection.commit()
    repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="history index|material references|Attempt index"):
        repository.load("run-1")


