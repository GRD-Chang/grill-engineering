"""Bounded row patches inside the repository's existing write transaction."""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any



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


def create_run_objects(
    connection: sqlite3.Connection, run_id: str, *, objects: Mapping[str, tuple[str, str]],
    materials: Mapping[str, str], native: bool,
) -> bool:
    """Insert a new Run's objects and references in its creation transaction."""
    native = _batch(
        connection, ((run_id, path, *value) for path, value in objects.items()),
        sql="INSERT INTO run_objects VALUES (?, ?, ?, ?)",
        native_sql="INSERT INTO run_objects SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
                   "json_extract(value, '$[2]'), json_extract(value, '$[3]') FROM json_each(?)",
        native=native,
    )
    native = _batch(
        connection, ((identity, value, len(value.encode("utf-8"))) for identity, value in materials.items()),
        sql="INSERT OR IGNORE INTO run_materials VALUES (?, ?, ?)",
        native_sql="INSERT OR IGNORE INTO run_materials SELECT json_extract(value, '$[0]'), "
                   "json_extract(value, '$[1]'), json_extract(value, '$[2]') FROM json_each(?)",
        native=native,
    )
    return _batch(
        connection, ((run_id, identity) for identity in materials),
        sql="INSERT INTO run_material_refs(run_id, material_id) VALUES (?, ?)",
        native_sql="INSERT INTO run_material_refs(run_id, material_id) SELECT json_extract(value, '$[0]'), "
                   "json_extract(value, '$[1]') FROM json_each(?)",
        native=native,
    )


def _same(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list | tuple):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, float) and left == right == 0:
        return math.copysign(1, left) == math.copysign(1, right)
    return bool(left == right)
