"""原始材料传输保留 UTF-8 原文、有界聚合和真实 SQL 类型。"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("body", ["", "\x00中文🙂\n\"\\" * 100, "a\x00b\x00" * 200, "escaped \\u0000 literal " * 100])
def test_work_material_transfer_preserves_exact_text_and_independent_values(
    tmp_path: Path, aggregate: bool, body: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    original = {"parent": {"body": body}, "timeline": [{"body": body}, {"body": body}]}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load("run-1") == original
    current = repository.load_work("run-1", 2)
    assert current == original
    current["timeline"][0]["body"] = "caller mutation"
    assert current["timeline"][1]["body"] == body
    assert repository.load_work("run-1", 2) == original


def test_work_material_transfer_hashes_noncanonical_snapshot_bytes(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    original = {"parent": {"number": 1e100, "body": "Unicode 中文🙂"}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        old_identity, raw = connection.execute("SELECT material_id,value FROM run_materials").fetchone()
        assert "1e+100" in raw
        rewritten = raw.replace("1e+100", "1e+0100")
        identity = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, rewritten, len(rewritten.encode("utf-8"))))
        connection.execute("UPDATE run_objects SET value_json=? WHERE value_json=?", (encode_json(identity), encode_json(old_identity)))
        connection.execute("UPDATE run_object_material_refs SET material_id=? WHERE material_id=?", (identity, old_identity))
        connection.execute("UPDATE run_material_refs SET material_id=? WHERE material_id=?", (identity, old_identity))
        connection.execute("DELETE FROM run_materials WHERE material_id=?", (old_identity,))
        connection.commit()
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection:
        assert connection.execute("SELECT value FROM run_materials").fetchone()[0] == rewritten


@pytest.mark.parametrize("damage", ["same_length_content", "value_blob", "length_real", "missing_run_link", "invalid_utf8"])
@pytest.mark.parametrize("warm_complete", [False, True])
def test_work_material_transfer_rejects_current_damage(tmp_path: Path, damage: str, warm_complete: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    original = {"parent": {"body": "evidence 中文🙂"}}
    repository.save("run-1", original)
    if warm_complete:
        assert repository.load("run-1") == original
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "same_length_content":
            connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence')")
        elif damage == "value_blob":
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        elif damage == "length_real":
            # INTEGER affinity preserves a fractional value as REAL.
            connection.execute("UPDATE run_materials SET byte_length=byte_length+0.5")
        elif damage == "missing_run_link":
            connection.execute("DELETE FROM run_material_refs")
        else:
            old_identity = connection.execute("SELECT material_id FROM run_materials").fetchone()[0]
            identity = hashlib.sha256(b"\x80").hexdigest()
            connection.execute("UPDATE run_materials SET material_id=?,value=CAST(X'80' AS TEXT),byte_length=1", (identity,))
            connection.execute("UPDATE run_objects SET value_json=? WHERE value_json=?", (encode_json(identity), encode_json(old_identity)))
            connection.execute("UPDATE run_object_material_refs SET material_id=? WHERE material_id=?", (identity, old_identity))
            connection.execute("UPDATE run_material_refs SET material_id=? WHERE material_id=?", (identity, old_identity))
        connection.commit()
    message = "UTF-8" if damage == "invalid_utf8" else "material"
    with pytest.raises(ValueError, match=message):
        repository.load_work("run-1", 2)


def test_work_material_body_aggregation_is_skipped_above_physical_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=2 * 1024 * 1024)
    original = {"parent": {"body": "中文🙂\x00" * 70000}}
    repository.save("run-1", original)
    connect = sqlite3.connect
    aggregated = []

    class BodyAggregate:
        def __init__(self):
            aggregated.append(True)
            raise AssertionError("oversized material body was aggregated")

        def step(self, value, separator):
            pass

        def finalize(self):
            return ""

    def guarded_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.create_aggregate("group_concat", 2, BodyAggregate)
        return connection

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    assert repository.load_work("run-1", 2) == original
    assert aggregated == []
