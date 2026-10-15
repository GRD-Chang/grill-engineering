"""Read and patch named collection children without assembling their siblings."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

from agent_run.sqlite_encoding import encode_json, object_path
from agent_run.sqlite_integrity import (KEYED_FIELDS, SOURCE_DIGEST_FIELDS, history_item_digest, field_size_proof, source_object_digest)

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository


def _decode_paths(
    connection: sqlite3.Connection, run_id: str, paths: set[tuple[str | int, ...]],
    rows: Mapping[str, tuple[str, str]], *, material_values: dict[str, str] | None = None,
) -> dict[tuple[str | int, ...], Any]:
    from agent_run.sqlite_objects import _decode

    names = {path: f"item-{index}" for index, path in enumerate(sorted(paths))}
    roots = {encode_json(path): path for path in paths}
    two_level = all(len(path) == 2 for path in paths)
    single = next(iter(paths)) if not two_level else ()
    present: set[tuple[str | int, ...]] = set()
    referenced: set[tuple[str | int, ...]] = set()
    relative = {}
    for raw, value in rows.items():
        parsed = object_path(raw)
        path = parsed[:2] if two_level else single
        if path not in names or parsed[:len(path)] != path:
            raise ValueError("Invalid SQLite collection child path")
        referenced.add(path)
        if raw in roots:
            present.add(path)
        location = encode_json((names[path],) + parsed[len(path):])
        if location in relative:
            raise ValueError("Duplicate SQLite collection child path")
        relative[location] = value
    if referenced - present:
        raise ValueError("Missing SQLite collection child")
    decoded = _decode(connection, relative, run_id=run_id, material_values=material_values) if relative else {}
    return {path: decoded[names[path]] for path in present}


def read_path(
    connection: sqlite3.Connection, run_id: str, path: tuple[str | int, ...], *,
    material_values: dict[str, str] | None = None,
) -> Any:
    from agent_run.sqlite_objects import _rows
    rows = _rows(connection, run_id, encode_json(path))
    if not rows:
        raise KeyError(path)
    return _decode_paths(connection, run_id, {path}, rows, material_values=material_values)[path]


def load_paths(
    connection: sqlite3.Connection, run_id: str, paths: set[tuple[str | int, ...]], *,
    material_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    material_values = {} if material_values is None else material_values
    if paths and all(len(path) == 2 and all(isinstance(part, str) for part in path) for path in paths):
        from agent_run.sqlite_objects import _rows_for_paths
        rows = _rows_for_paths(connection, run_id, [encode_json(path) for path in sorted(paths)])
        for path, value in _decode_paths(connection, run_id, paths, rows, material_values=material_values).items():
            result.setdefault(str(path[0]), {})[str(path[1])] = value
        return result
    for path in paths:
        if not path or any(not isinstance(part, str) for part in path):
            raise ValueError("Collection path projection requires mapping ancestors")
        try:
            value = read_path(connection, run_id, path, material_values=material_values)
        except KeyError:
            continue
        target = result
        for part in path[:-1]:
            target = target.setdefault(str(part), {})
        target[str(path[-1])] = value
    return result


def prepare_collections(
    repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
    state: Mapping[str, Any], keyed: Mapping[str, set[str]], append: Mapping[str, list[Any]],
    objects: dict[str, tuple[str, str]], materials: dict[str, str], previous: dict[str, tuple[str, str]],
) -> dict[str, tuple[int, str, int]]:
    from agent_run.sqlite_objects import _rows
    summaries = {}
    read_materials: dict[str, str] = {}
    for field in keyed.keys() | append.keys():
        path = encode_json((field,))
        source = connection.execute("SELECT kind,value_json FROM run_objects WHERE run_id=? AND object_path=?", (run_id, path)).fetchone()
        summary = connection.execute("SELECT logical_bytes,content_digest,child_count FROM run_object_index WHERE run_id=? AND object_path=?",
                                     (run_id, path)).fetchone()
        size, digest, count = (summary[0], int(summary[1], 16), summary[2]) if summary else (len(encode_json(field).encode()) + 4, 0, 0)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid SQLite collection child count")
        if source is not None:
            if source[0] != ("map" if field in keyed else "list"):
                raise ValueError("Invalid SQLite collection object kind")
            previous[path] = source
        changes: list[tuple[str | int, Any, bool]]
        if field in keyed:
            if field not in KEYED_FIELDS:
                raise ValueError("Unsupported keyed SQLite collection")
            incoming = state.get(field, {})
            if not isinstance(incoming, Mapping):
                raise ValueError("Keyed SQLite collection must be a mapping")
            changes = [(key, incoming.get(key), key in incoming) for key in sorted(keyed[field])]
        else:
            changes = [(count + index, item, True) for index, item in enumerate(append[field])]
        for key, item, present in changes:
            child_path = (field, key)
            child_rows = _rows(connection, run_id, encode_json(child_path)) if field in keyed else {}
            if child_rows:
                old = _decode_paths(connection, run_id, {child_path}, child_rows,
                                    material_values=read_materials)[child_path]
                previous.update(child_rows)
                body = len(encode_json(old).encode()) + (len(encode_json(key).encode()) + 1 if isinstance(key, str) else 0)
                size -= body + bool(count > 1)
                digest -= (sum(source_object_digest(location, value) for location, value in child_rows.items())
                           if field in SOURCE_DIGEST_FIELDS else history_item_digest(key, old))
                count -= 1
            if not present:
                continue
            shaped: dict[str, Any] = {field: {key: item}} if isinstance(key, str) else {field: [item]}
            encoded_objects, encoded_materials = repository._objects(shaped)
            origin = (field, key if isinstance(key, str) else 0)
            for location, value in encoded_objects.items():
                parts = object_path(location)
                if parts[:2] == origin:
                    objects[encode_json(child_path + parts[2:])] = value
            materials.update(encoded_materials)
            body = len(encode_json(item).encode()) + (len(encode_json(key).encode()) + 1 if isinstance(key, str) else 0)
            size += body + bool(count)
            digest += (sum(source_object_digest(location, value) for location, value in objects.items()
                           if object_path(location)[:2] == child_path)
                       if field in SOURCE_DIGEST_FIELDS else history_item_digest(key, item))
            count += 1
        objects[path] = ("map", "null") if field in keyed else ("list", str(count))
        if field in SOURCE_DIGEST_FIELDS and source is None:
            digest += source_object_digest(path, objects[path])
        summaries[field] = (size, f"{digest % (1 << 256):064x}", count)
    return summaries


def save_summaries(
    connection: sqlite3.Connection, run_id: str, summaries: Mapping[str, tuple[int, str, int]],
    objects: Mapping[str, tuple[str, str]],
) -> None:
    for field, (size, digest, count) in summaries.items():
        path = encode_json((field,))
        connection.execute("INSERT INTO run_object_index VALUES (?,?,NULL,NULL,?,?,?,?,NULL) "
                           "ON CONFLICT(run_id,object_path) DO UPDATE SET logical_bytes=excluded.logical_bytes,"
                           "content_digest=excluded.content_digest,size_proof=excluded.size_proof,child_count=excluded.child_count",
                           (run_id, path, size, digest, field_size_proof(path, size, digest, objects[path], count), count))
