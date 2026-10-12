"""Consistent SQL row sources with optional native JSON aggregation."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

_MATERIAL_ROWS = (
    "SELECT m.material_id, m.value, m.byte_length FROM run_materials m "
    "JOIN run_material_refs r USING(material_id) WHERE r.run_id = :run_id ORDER BY r.material_id"
)
_OBJECT_ROWS = (
    "SELECT object_path, kind, value_json FROM run_objects WHERE run_id = :run_id ORDER BY object_path"
)
_SIZES = (
    "SELECT EXISTS(SELECT 1 FROM runs WHERE run_id = :run_id) AS found, "
    "(SELECT COALESCE(SUM(LENGTH(CAST(m.value AS BLOB))), 0) FROM run_materials m "
    "JOIN run_material_refs r USING(material_id) WHERE r.run_id = :run_id) AS materials, "
    "(SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB)) + LENGTH(CAST(value_json AS BLOB))), 0) "
    "FROM run_objects WHERE run_id = :run_id) AS objects"
)

_DOCUMENT = (
    "WITH sizes AS (" + _SIZES + ") SELECT found, materials, objects, "
    "(SELECT json_group_array(json_array(type, name, tbl_name, sql)) FROM "
    "(SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name)), "
    "(SELECT json_group_array(json_array(key, value)) FROM schema_meta), "
    "CASE WHEN found AND materials + objects <= :aggregate_bytes THEN "
    "(SELECT json_group_array(json_array(material_id, value, byte_length)) FROM ("
    + _MATERIAL_ROWS + ")) END, "
    "CASE WHEN found AND materials + objects <= :aggregate_bytes THEN "
    "(SELECT json_group_array(json_array(object_path, kind, value_json)) FROM ("
    + _OBJECT_ROWS + ")) END FROM sizes"
)


@dataclass(frozen=True)
class RowSource:
    materials: str | tuple[tuple[str, str, int], ...]
    objects: str | tuple[tuple[str, str, str], ...]

    def material_rows(self) -> tuple[tuple[str, str, int], ...]:
        if isinstance(self.materials, str):
            return tuple(tuple(row) for row in json.loads(self.materials))
        return self.materials

    def object_rows(self) -> tuple[tuple[str, str, str], ...]:
        if isinstance(self.objects, str):
            return tuple(tuple(row) for row in json.loads(self.objects))
        return self.objects


class ReadConnection(sqlite3.Connection):
    schema_facts: tuple[tuple[tuple[str, str, str, str | None], ...], dict[str, str]] | None = None
    run_rows: tuple[RowSource | None, int, int, bool] | None = None


def read_document(
    connection: ReadConnection, run_id: str, *, max_bytes: int, aggregate_bytes: int,
    tables: Mapping[str, str],
) -> bool:
    """Capture a complete small Run and canonical schema in one SQL snapshot.

    Noncanonical (but potentially compatible) DDL and large Runs use the
    existing explicit read transaction and structural PRAGMA validation.
    """
    try:
        found, materials, objects, ddl, metadata, material_json, object_json = connection.execute(
            _DOCUMENT, {"run_id": run_id, "aggregate_bytes": min(aggregate_bytes, 8 * max_bytes)},
        ).fetchone()
    except sqlite3.OperationalError as error:
        if any(reason in str(error) for reason in ("no such function", "no such table", "no such column")):
            return False
        raise
    definitions = tuple(tuple(row) for row in json.loads(ddl))
    actual = {name: sql for kind, name, _, sql in definitions if kind == "table" and not name.startswith("sqlite_")}
    if actual != tables or found and material_json is None:
        return False
    connection.schema_facts = definitions, dict(json.loads(metadata))
    connection.run_rows = (RowSource(material_json, object_json) if found else None, materials, objects, True)
    return True


def read_schema(
    connection: sqlite3.Connection,
) -> tuple[tuple[tuple[str, str, str, str | None], ...], dict[str, str] | None]:
    """Read all actual DDL and protocol rows without per-row GIL handoffs."""
    if isinstance(connection, ReadConnection) and connection.schema_facts is not None:
        return connection.schema_facts
    definitions = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    try:
        encoded, metadata = connection.execute(
            "SELECT json_group_array(json_array(type, name, tbl_name, sql)), "
            "(SELECT json_group_array(json_array(key, value)) FROM schema_meta) FROM ("
            + definitions + ")"
        ).fetchone()
        return tuple(tuple(row) for row in json.loads(encoded)), dict(json.loads(metadata))
    except sqlite3.OperationalError as error:
        if "no such function" not in str(error) and "no such table: schema_meta" not in str(error):
            raise
    # Empty/invalid databases and builds without JSON functions retain the
    # original structural checks before looking up schema_meta.
    return tuple(connection.execute(definitions)), None


def read_material_ids(connection: sqlite3.Connection, run_id: str, *, aggregate: bool) -> set[str]:
    query = "SELECT material_id FROM run_material_refs WHERE run_id = ?"
    if aggregate:
        try:
            encoded = connection.execute(
                "SELECT json_group_array(material_id) FROM (" + query + ")", (run_id,),
            ).fetchone()[0]
            return set(json.loads(encoded))
        except sqlite3.OperationalError as error:
            if "no such function" not in str(error):
                raise
    return {row[0] for row in connection.execute(query, (run_id,))}


def read_row_source(connection: sqlite3.Connection, run_id: str, *, aggregate: bool) -> RowSource:
    # Actual ordered values, including types/lengths, remain the cache key.
    # Aggregation avoids releasing/reacquiring the Python GIL for every row in
    # concurrent hot reads. It neither persists JSON nor skips database facts.
    params = {"run_id": run_id}
    if aggregate:
        try:
            material_json = connection.execute(
                "SELECT json_group_array(json_array(material_id, value, byte_length)) FROM ("
                + _MATERIAL_ROWS + ")", params,
            ).fetchone()[0]
            object_json = connection.execute(
                "SELECT json_group_array(json_array(object_path, kind, value_json)) FROM ("
                + _OBJECT_ROWS + ")", params,
            ).fetchone()[0]
            return RowSource(material_json, object_json)
        except sqlite3.OperationalError as error:
            if "no such function" not in str(error):
                raise
            # JSON support can be omitted from a valid stdlib SQLite build.
    return RowSource(tuple(connection.execute(_MATERIAL_ROWS, params)),
                     tuple(connection.execute(_OBJECT_ROWS, params)))


def read_run_rows(
    connection: sqlite3.Connection, run_id: str, *, max_bytes: int,
    aggregate_bytes: int, aggregate: bool,
) -> tuple[RowSource | None, int, int, bool]:
    """Read identity, bounds and small sources with one SQLite handoff.

    CASE checks size before aggregating any content. Large sources use raw
    rows; missing JSON support retains the same snapshot and size guard.
    """
    if isinstance(connection, ReadConnection) and connection.run_rows is not None:
        return connection.run_rows
    params = {"run_id": run_id, "aggregate_bytes": min(aggregate_bytes, 8 * max_bytes)}
    source = None
    if aggregate:
        try:
            found, materials, objects, material_json, object_json = connection.execute(
                "WITH sizes AS (" + _SIZES + ") SELECT found, materials, objects, "
                "CASE WHEN found AND materials + objects <= :aggregate_bytes THEN "
                "(SELECT json_group_array(json_array(material_id, value, byte_length)) FROM ("
                + _MATERIAL_ROWS + ")) END, "
                "CASE WHEN found AND materials + objects <= :aggregate_bytes THEN "
                "(SELECT json_group_array(json_array(object_path, kind, value_json)) FROM ("
                + _OBJECT_ROWS + ")) END FROM sizes", params,
            ).fetchone()
            if material_json is not None:
                source = RowSource(material_json, object_json)
        except sqlite3.OperationalError as error:
            if "no such function" not in str(error):
                raise
            aggregate = False
    if not aggregate:
        found, materials, objects = connection.execute(_SIZES, params).fetchone()
    if not found:
        return None, 0, 0, aggregate
    if materials + objects > 8 * max_bytes:
        raise ValueError(f"Run state exceeds {max_bytes} byte persistence limit")
    if source is None:
        source = read_row_source(connection, run_id, aggregate=False)
    return source, materials, objects, aggregate
