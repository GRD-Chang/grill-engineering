"""Complete row transport preserves actual material bytes and SQL types."""
from contextlib import closing, contextmanager
from collections.abc import Iterator
import hashlib
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_rows import (
    MaterialRows, ReadConnection, RowSource, read_document, read_row_source, read_run_rows,
)
from agent_run.sqlite_state import SQLiteStateRepository


@contextmanager
def _database() -> Iterator[tuple[ReadConnection, dict[str, str]]]:
    with closing(sqlite3.connect(":memory:", factory=ReadConnection)) as connection:
        for sql in (
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY)",
            "CREATE TABLE run_materials (material_id TEXT PRIMARY KEY, value TEXT, byte_length INTEGER)",
            "CREATE TABLE run_material_refs (run_id TEXT, material_id TEXT)",
            "CREATE TABLE run_objects (run_id TEXT, object_path TEXT, kind TEXT, value_json TEXT)",
            "CREATE TABLE schema_meta (key TEXT, value TEXT)",
        ):
            connection.execute(sql)
        connection.execute("INSERT INTO runs VALUES ('run-1')")
        tables = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        yield connection, tables


def _insert(connection: sqlite3.Connection, body: str) -> str:
    identity = hashlib.sha256(body.encode()).hexdigest()
    connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, body, len(body.encode())))
    connection.execute("INSERT INTO run_material_refs VALUES ('run-1',?)", (identity,))
    return identity


def _source(connection: ReadConnection, tables: dict[str, str], mode: str) -> RowSource:
    if mode == "document":
        assert read_document(connection, "run-1", max_bytes=65536, aggregate_bytes=65536, tables=tables)
    if mode in {"document", "run_rows"}:
        source, _, _, supported = read_run_rows(
            connection, "run-1", max_bytes=65536, aggregate_bytes=65536, aggregate=True,
        )
        assert supported and source is not None
        return source
    return read_row_source(connection, "run-1", aggregate=True)


@pytest.mark.parametrize("mode", ["document", "run_rows", "row_source"])
def test_raw_material_transport_preserves_unicode_nul_and_empty(mode: str) -> None:
    with _database() as (connection, tables):
        bodies = ("", "中文🌊\x00after nul\n\"quoted\"\\" * 100, "another material")
        for body in bodies:
            _insert(connection, body)
        source = _source(connection, tables, mode)
        assert isinstance(source.materials, MaterialRows)
        expected = read_row_source(connection, "run-1", aggregate=False)
        assert source.material_rows() == expected.material_rows()
        assert source.materials.data == b"".join(body.encode() for _, body, _ in expected.material_rows())
        assert "after nul" not in source.materials.metadata
        assert source.object_rows() == expected.object_rows()


@pytest.mark.parametrize("mode", ["document", "run_rows", "row_source"])
def test_raw_material_transport_handles_no_materials(mode: str) -> None:
    with _database() as (connection, tables):
        source = _source(connection, tables, mode)
        assert source.materials == MaterialRows(b"", "[]")
        assert source.material_rows() == ()


@pytest.mark.parametrize("mode", ["document", "run_rows", "row_source"])
@pytest.mark.parametrize("damage", ["body_blob", "identity_blob", "length_blob", "length_real", "length_text", "wrong_length"])
def test_raw_material_transport_rejects_actual_sql_types_and_lengths(mode: str, damage: str) -> None:
    with _database() as (connection, tables):
        identity = _insert(connection, "material body")
        if damage == "identity_blob":
            connection.execute("UPDATE run_materials SET material_id=CAST(material_id AS BLOB)")
            connection.execute("UPDATE run_material_refs SET material_id=CAST(material_id AS BLOB)")
        else:
            column, value = {
                "body_blob": ("value", sqlite3.Binary(b"material body")),
                "length_blob": ("byte_length", sqlite3.Binary(b"13")),
                "length_real": ("byte_length", 13.5),
                "length_text": ("byte_length", "invalid"),
                "wrong_length": ("byte_length", 14),
            }[damage]
            connection.execute(f"UPDATE run_materials SET {column}=? WHERE material_id=?", (value, identity))
        source = _source(connection, tables, mode)
        with pytest.raises(ValueError, match="material identity: type or byte length"):
            source.material_rows()


def test_raw_material_transport_rejects_invalid_utf8_text() -> None:
    with _database() as (connection, tables):
        _insert(connection, "original")
        connection.execute("UPDATE run_materials SET value=CAST(X'FF' AS TEXT), byte_length=1")
        source = _source(connection, tables, "document")
        with pytest.raises(ValueError, match="UTF-8"):
            source.material_rows()


@pytest.mark.parametrize("damage", ["sql_type", "declared_length"])
def test_material_source_equality_includes_actual_sql_metadata(damage: str) -> None:
    with _database() as (connection, tables):
        _insert(connection, "same bytes")
        original = read_row_source(connection, "run-1", aggregate=True)
        assert original == read_row_source(connection, "run-1", aggregate=True)
        if damage == "sql_type":
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        else:
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        changed = read_row_source(connection, "run-1", aggregate=True)
        assert isinstance(original.materials, MaterialRows)
        assert isinstance(changed.materials, MaterialRows)
        assert original.materials.data == changed.materials.data
        assert original.materials.metadata != changed.materials.metadata
        assert original != changed
        with pytest.raises(ValueError, match="material identity"):
            changed.material_rows()


def test_cached_complete_query_rechecks_same_length_material_bytes(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    body = "unchanged evidence " * 100
    original = {"parent": {"body": body}}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    assert repository.load("run-1") == original
    cached = repository._read_view.source
    assert isinstance(cached.materials, MaterialRows)
    identity = hashlib.sha256(body.encode()).hexdigest()
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_materials SET value=? WHERE material_id=?", ("X" + body[1:], identity))
        connection.commit()
        changed = read_row_source(connection, "run-1", aggregate=True)
        assert isinstance(changed.materials, MaterialRows)
        assert changed.materials.metadata == cached.materials.metadata
        assert changed.materials.data != cached.materials.data
        assert changed != cached
    with pytest.raises(ValueError, match="material identity"):
        repository.load("run-1")


def test_document_and_run_bounds_precede_raw_body_aggregation() -> None:
    with _database() as (connection, tables):
        _insert(connection, "oversized material" * 100)
        reached = []

        class UnexpectedAggregate:
            def step(self, *args):
                reached.append(True)
                raise AssertionError("oversized material reached aggregation")

            def finalize(self):
                return None

        connection.create_aggregate("group_concat", 2, UnexpectedAggregate)
        assert not read_document(connection, "run-1", max_bytes=65536, aggregate_bytes=1, tables=tables)
        assert connection.run_rows is None
        source, _, _, supported = read_run_rows(
            connection, "run-1", max_bytes=65536, aggregate_bytes=1, aggregate=True,
        )
        assert supported and source == read_row_source(connection, "run-1", aggregate=False)
        with pytest.raises(ValueError, match="persistence limit"):
            read_run_rows(connection, "run-1", max_bytes=1, aggregate_bytes=65536, aggregate=True)
        assert reached == []


def test_raw_document_missing_json_keeps_original_fallback() -> None:
    with _database() as (connection, tables):
        _insert(connection, "原始材料\x00" * 100)

        class WithoutJson:
            def execute(self, sql, args=()):
                if "json_group_array" in sql:
                    raise sqlite3.OperationalError("no such function: json_group_array")
                return connection.execute(sql, args)

        without_json = WithoutJson()
        assert not read_document(without_json, "run-1", max_bytes=65536, aggregate_bytes=65536, tables=tables)
        source, _, _, supported = read_run_rows(
            without_json, "run-1", max_bytes=65536, aggregate_bytes=65536, aggregate=True,
        )
        assert not supported
        assert source == read_row_source(connection, "run-1", aggregate=False)
