"""投影材料批次读取保留真实字节、Run 链接和独立容器。"""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_objects import load_projection
from agent_run.sqlite_state import SQLiteStateRepository


def _state(body: str = "中文🙂\x00\\\n" * 100) -> dict[str, Any]:
    evidence = {"body": body, "nested": [{"count": 1}, {"count": 2}]}
    return {"agent_invocation_history": [], "parent": evidence, "ticket": deepcopy(evidence), "phase": "active",
            "timeline": [{"body": body}, {"body": body}],
            "ticket_jobs": {"2": {"ticket_number": 2, "review_context": evidence}}}


@pytest.mark.parametrize("missing_json", [False, True])
def test_projection_prefetch_preserves_text_aliases_and_independent_reads(
    tmp_path: Path, missing_json: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    original = _state()
    repository.save("run-1", original)

    class NoJSON(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if missing_json and sql.startswith("SELECT json_group_array(json_array(m.material_id,"):
                raise sqlite3.OperationalError("no such function: json_group_array")
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(repository.path, factory=NoJSON)) as connection:
        connection.execute("BEGIN")
        loaded = load_projection(connection, "run-1", max_bytes=repository.max_bytes)
        assert loaded is not None
        state, _ = loaded
        assert state == original
        state["parent"]["nested"][0]["count"] = 100
        state["timeline"][0]["body"] = "caller change"
        assert state["ticket"] == original["ticket"]
        assert state["timeline"][1] == original["timeline"][1]
        assert repository.load_objects("run-1")[0] == original


@pytest.mark.parametrize("damage", ["body", "declared_length", "blob", "real_length", "missing_link", "nested_body"])
def test_projection_prefetch_revalidates_actual_materials(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state("evidence " * 150)
    repository.save("run-1", original)
    assert repository.load_objects("run-1")[0] == original
    with closing(sqlite3.connect(repository.path)) as connection:
        parent_id = connection.execute(
            "SELECT value_json FROM run_objects WHERE object_path=?", (encode_json(("parent",)),),
        ).fetchone()[0]
        if damage in {"body", "nested_body"}:
            if damage == "body":
                connection.execute("UPDATE run_materials SET value=replace(value,'nested','Nested') WHERE material_id=?",
                                   (json.loads(parent_id),))
            else:
                connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence') WHERE value LIKE 'evidence %'")
        elif damage == "declared_length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "blob":
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        elif damage == "real_length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+0.5")
        else:
            connection.execute("DELETE FROM run_material_refs WHERE run_id='run-1'")
        connection.commit()
    with pytest.raises(ValueError, match="material"):
        repository.load_objects("run-1")


def test_projection_prefetch_never_aggregates_oversized_bodies(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    evidence = {"rows": [{"label": "中文🙂\x00" * 20} for _ in range(3500)]}
    original = {"parent": evidence, "ticket": deepcopy(evidence), "agent_invocation_history": []}
    repository.save("run-1", original)

    class GuardBodies(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("SELECT json_group_array(json_array(m.material_id,"):
                raise AssertionError("oversized direct material bodies were aggregated")
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(repository.path, factory=GuardBodies)) as connection:
        connection.execute("BEGIN")
        loaded = load_projection(connection, "run-1", max_bytes=repository.max_bytes)
        assert loaded is not None and loaded[0] == original


def test_projection_prefetch_does_not_borrow_another_runs_material_link(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    repository.save("run-2", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("DELETE FROM run_material_refs WHERE run_id='run-1'")
        connection.commit()
    with pytest.raises(ValueError, match="Missing SQLite Run material"):
        repository.load_objects("run-1")
    assert repository.load_objects("run-2")[0] == original


def test_projection_prefetch_keeps_each_material_batch_bounded(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = {"timeline": [{"event": str(index), "items": [index]} for index in range(300)],
                "agent_invocation_history": []}
    repository.save("run-1", original)
    batches = []

    class GuardBatch(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("SELECT json_group_array(json_array(m.material_id,"):
                batches.append(len(parameters) - 1)
                assert len(parameters) <= 257
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(repository.path, factory=GuardBatch)) as connection:
        connection.execute("BEGIN")
        loaded = load_projection(connection, "run-1", max_bytes=repository.max_bytes)
        assert loaded is not None and loaded[0] == original
    assert len(batches) == 2


def test_projection_prefetch_does_not_expand_unselected_materials(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = {"parent": {"body": "selected evidence"}, "ticket": {"body": "unselected evidence"}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_materials SET value=replace(value,'unselected','Unselected')")
        connection.commit()
    assert repository.load_fields("run-1", {"parent"}) == {"parent": original["parent"]}
    with pytest.raises(ValueError, match="material"):
        repository.load_fields("run-1", {"ticket"})
