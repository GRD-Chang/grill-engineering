"""Full index checks retain actual SQLite types and complete references."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run import sqlite_job_history, sqlite_work_objects as sqlite_objects
from support.work_records import attempt, invocation


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
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == original
        assert repository.current_work_baseline("run-1") == original
        assert repository._check_size(current) == repository._check_size(original)
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
@pytest.mark.parametrize("damage", ["history_blob", "refs_blob", "attempt_blob",
                                     "history_json_blob", "refs_json_blob", "attempt_json_blob"])
def test_full_indexes_reject_blob_values_without_json_coercion(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = _repository(tmp_path)
    original = {"agent_invocation_history": [invocation(started="original", context={"body": "immutable evidence"})],
                "ticket_jobs": {"2": {"semantic_attempt_history": [attempt()]}}}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    assert repository._read_view.view.index_facts is not None
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage in {"history_blob", "history_json_blob"}:
            value = b'"ticket:2"' if damage.endswith("json_blob") else b"ticket:2"
            connection.execute("UPDATE work_invocations SET work_subject=?", (sqlite3.Binary(value),))
        elif damage in {"refs_blob", "refs_json_blob"}:
            if damage.endswith("json_blob"):
                connection.execute("UPDATE work_evidence SET material_id=CAST(json_quote(material_id) AS BLOB) WHERE material_id IS NOT NULL")
            else:
                connection.execute("UPDATE work_evidence SET material_id=CAST(material_id AS BLOB) WHERE material_id IS NOT NULL")
        else:
            value = b'"reviewer"' if damage.endswith("json_blob") else b"reviewer"
            connection.execute("UPDATE work_attempts SET role=?", (sqlite3.Binary(value),))
        connection.commit()
    repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="Invocation|evidence|execution record|Attempt"):
        repository.load("run-1")


@pytest.mark.parametrize("aggregate", [True, False])
def test_identical_complete_source_reuses_expected_metadata_and_still_reads_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool,
) -> None:
    repository = _repository(tmp_path)
    repository._aggregate_rows = aggregate
    current = invocation(started="original", context={"body": "evidence" * 100})
    original = {"ticket_agent_invocations": {"2": deepcopy(current)},
                "agent_invocation_history": [current],
                "ticket_jobs": {"2": {"semantic_attempt_history": [attempt()],
                                      "human_blocker_history": [{"body": "retained evidence" * 100}]}}}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    current_archive_reads = []
    from agent_run import sqlite_change_jobs
    actual_history = sqlite_change_jobs._load_history
    def read_history(*args, **kwargs):
        current_archive_reads.append(True)
        return actual_history(*args, **kwargs)
    monkeypatch.setattr(sqlite_change_jobs, "_load_history", read_history)
    first = repository.load("run-1")
    assert first == original
    first["ticket_jobs"]["2"]["human_blocker_history"][0]["body"] = "caller mutation"
    assert repository.load("run-1") == original
    assert len(current_archive_reads) >= 2


@pytest.mark.parametrize("aggregate", [True, False])
def test_complete_query_reuses_only_unchanged_source_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool,
) -> None:
    from agent_run import sqlite_state

    repository = _repository(tmp_path)
    original = {"status": "active", "ticket_graph": {"tickets": {"2": {"number": 123456, "title": "retained title"}}},
                "parent": {"body": "frozen requirement" * 100}, "timeline": [{"event": "retained event"}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    writer = _repository(tmp_path)
    changed = {**original, "status": "paused"}
    writer.save_objects("run-1", changed, fields={"status"})
    decode_scalar = sqlite_state.scalar_value

    def decode(encoded):
        assert encoded not in {'123456', '"retained title"'}, "unchanged field was assembled again"
        return decode_scalar(encoded)

    monkeypatch.setattr(sqlite_state, "scalar_value", decode)
    current = repository.load("run-1")
    assert current == changed
    current["ticket_graph"]["tickets"]["2"]["title"] = "caller mutation"
    assert repository.load("run-1") == changed
    # An external source-node change invalidates the field even when metadata
    # such as updated_at did not change, and the returned tree stays isolated.
    monkeypatch.setattr(sqlite_state, "scalar_value", decode_scalar)
    changed["ticket_graph"] = {"tickets": {"2": {"number": 123457, "title": "new title"}}}
    writer.save_objects("run-1", changed, fields={"ticket_graph"})
    assert repository.load("run-1") == changed


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["content", "blob"])
def test_unchanged_complete_field_still_checks_actual_material_bytes(tmp_path: Path, aggregate: bool, damage: str) -> None:
    repository = _repository(tmp_path)
    original = {"status": "active", "parent": {"body": "frozen requirement" * 100}}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_objects SET value_json='\"paused\"' WHERE object_path='[\"status\"]'")
        if damage == "blob":
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        else:
            connection.execute("UPDATE run_materials SET value=replace(value,'frozen','Frozen')")
        connection.commit()
    with pytest.raises(ValueError, match="material identity|JSON cannot hold BLOB"):
        repository.load("run-1")
