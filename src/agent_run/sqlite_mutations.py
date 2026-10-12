"""Bounded row patches inside the repository's existing write transaction."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from itertools import islice

from agent_run.sqlite_rows import read_material_ids

Row = tuple[str | int, ...]


def _batch(
    connection: sqlite3.Connection, rows: Iterable[Row], *, sql: str, native_sql: str, native: bool,
) -> bool:
    iterator = iter(rows)
    while batch := tuple(islice(iterator, 256)):
        # Estimate before encoding: JSON escaping can grow text by at most six
        # times. Large bodies retain executemany without a large transfer copy.
        size = sum(len(value.encode("utf-8")) if isinstance(value, str) else 24
                   for row in batch for value in row)
        # Older JSON1 json_extract truncates decoded strings at NUL. Bind
        # these batches directly to preserve exact bytes on every build;
        # this does not disable the native path for later safe batches.
        if native and size <= 64 * 1024 and not any(
            isinstance(value, str) and "\x00" in value for row in batch for value in row
        ):
            try:
                connection.execute(native_sql, (json.dumps(batch, ensure_ascii=False, separators=(",", ":")),))
                continue
            except sqlite3.OperationalError as error:
                if ("no such function" not in str(error)
                        and "no such table: json_each" not in str(error)):
                    raise
                # Preparation failed before any row changed. Do not retry
                # constraint errors or any already executed statement.
                native = False
        connection.executemany(sql, batch)
    return native


def patch_run(
    connection: sqlite3.Connection, run_id: str, *, objects: Mapping[str, tuple[str, str]],
    previous: Mapping[str, tuple[str, str]], materials: Mapping[str, str],
    known_materials: Mapping[str, str], native: bool,
) -> bool:
    native = _batch(
        connection, ((run_id, path) for path in previous.keys() - objects.keys()),
        sql="DELETE FROM run_objects WHERE run_id = ? AND object_path = ?",
        native_sql="DELETE FROM run_objects WHERE (run_id, object_path) IN "
                   "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?))",
        native=native,
    )
    update = "ON CONFLICT(run_id, object_path) DO UPDATE SET kind=excluded.kind, value_json=excluded.value_json"
    native = _batch(
        connection, ((run_id, path, *value) for path, value in objects.items() if previous.get(path) != value),
        sql="INSERT INTO run_objects VALUES (?, ?, ?, ?) " + update,
        native_sql="INSERT INTO run_objects SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
                   "json_extract(value, '$[2]'), json_extract(value, '$[3]') FROM json_each(?) WHERE true " + update,
        native=native,
    )
    native = _batch(
        connection, ((identity, value, len(value.encode("utf-8"))) for identity, value in materials.items()
                     if identity not in known_materials),
        sql="INSERT OR IGNORE INTO run_materials VALUES (?, ?, ?)",
        native_sql="INSERT OR IGNORE INTO run_materials SELECT json_extract(value, '$[0]'), "
                   "json_extract(value, '$[1]'), json_extract(value, '$[2]') FROM json_each(?)",
        native=native,
    )
    references = read_material_ids(connection, run_id, aggregate=native)
    removed = references - materials.keys()
    native = _batch(
        connection, ((run_id, identity) for identity in removed),
        sql="DELETE FROM run_material_refs WHERE run_id = ? AND material_id = ?",
        native_sql="DELETE FROM run_material_refs WHERE (run_id, material_id) IN "
                   "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?))",
        native=native,
    )
    native = _batch(
        connection, ((run_id, identity) for identity in materials.keys() - references),
        sql="INSERT INTO run_material_refs VALUES (?, ?)",
        native_sql="INSERT INTO run_material_refs SELECT json_extract(value, '$[0]'), "
                   "json_extract(value, '$[1]') FROM json_each(?)",
        native=native,
    )
    return _batch(
        connection, ((identity, identity) for identity in removed),
        sql="DELETE FROM run_materials WHERE material_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM run_material_refs WHERE material_id = ?)",
        native_sql="DELETE FROM run_materials WHERE material_id IN "
                   "(SELECT json_extract(value, '$[0]') FROM json_each(?)) AND NOT EXISTS "
                   "(SELECT 1 FROM run_material_refs r WHERE r.material_id = run_materials.material_id)",
        native=native,
    )
