"""Indexed work projections and object-scoped material ownership."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import copy_json_tree, encode_json, object_path, scalar_value
from agent_run import sqlite_timeline
from agent_run.sqlite_work_indexes import IndexFacts, check_facts
from agent_run.sqlite_work_fields import reuse_fields

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository

ObjectPath = tuple[str | int, ...]
ParsedRow = tuple[str, ObjectPath, str, str]



def _references(repository: SQLiteStateRepository, kind: str, encoded: str,
                materials: Mapping[str, str]) -> set[str]:
    if kind not in {"material", "snapshot"}:
        return set()
    identity = scalar_value(encoded)
    if not isinstance(identity, str) or identity not in materials:
        raise ValueError("Missing SQLite Run material")
    references = {identity}
    if kind == "snapshot":
        repository._restore_snapshot(json.loads(materials[identity]), materials, references)
    return references


def index_run(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
              state: Mapping[str, Any], objects: Mapping[str, tuple[str, str]],
              materials: Mapping[str, str]) -> None:
    """Build sparse indexes only at the explicitly complete dictionary boundary."""
    connection.execute("INSERT OR REPLACE INTO run_sizes VALUES (?, ?, 0)",
                       (run_id, repository._check_size(state)))
    connection.executemany("INSERT INTO run_object_material_refs VALUES (?, ?, ?)",
                          ((run_id, path, identity) for path, (kind, encoded) in objects.items()
                           for identity in _references(repository, kind, encoded, materials)))


def check_indexes(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                  state: Mapping[str, Any], logical_bytes: int,
                  objects: Mapping[str, tuple[str, str]], materials: Mapping[str, str], *,
                  snapshot_refs: Mapping[str, frozenset[str]] | None = None,
                  snapshot_encodings: Mapping[tuple[str | int, ...], str] | None = None,
                  expected: IndexFacts | None = None) -> IndexFacts:
    """A complete query validates sparse metadata against complete stored facts."""
    if expected is None:
        expected = IndexFacts(
            run_id, (logical_bytes, 0),
            frozenset((path, identity) for path, (kind, encoded) in objects.items()
                      # The complete source restoration proved these closures.
                      for identity in (snapshot_refs[path] if kind == "snapshot" and snapshot_refs is not None and path in snapshot_refs
                                       else _references(repository, kind, encoded, materials))),
        )
    check_facts(connection, run_id, expected, max_bytes=repository.max_bytes)
    return expected


def load_base_work(
    repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str, *, thin_timeline: bool,
) -> tuple[dict[str, Any], dict[str, sqlite_timeline.TimelineScope], int] | None:
    """Read only remaining Run facts, retaining their current Timeline proof."""
    from agent_run.sqlite_integrity import require_field_sizes
    from agent_run.sqlite_objects import _require_root

    if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        return None
    _require_root(connection, run_id)
    require_field_sizes(connection, run_id, max_bytes=repository.max_bytes)
    byte_count = connection.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+LENGTH(CAST(value_json AS BLOB))),0) "
        "FROM run_objects WHERE run_id=?", (run_id,),
    ).fetchone()[0]
    if type(byte_count) is not int or byte_count > 8 * repository.max_bytes:
        raise ValueError("SQLite Run objects exceed persistence limit")
    rows = _sql_rows(repository, connection,
                     "SELECT object_path,kind,value_json FROM run_objects WHERE run_id=?", (run_id,),
                     ("object_path", "kind", "value_json"), byte_count=byte_count)
    context = DecodeContext()
    state, _ = _decode(repository, connection, run_id, rows, context=context, thin_timeline=thin_timeline)
    size = repository._check_size(state, unloaded_fields={key: scope.encoded for key, scope in context.timelines.items()})
    recorded = connection.execute("SELECT logical_bytes,canonical_count FROM run_sizes WHERE run_id=?", (run_id,)).fetchone()
    if recorded != (size, 0):
        raise ValueError("Invalid SQLite Run size metadata")
    return state, context.timelines, size


@dataclass
class DecodeContext:
    """Validated materials owned by one consistent work-read snapshot."""
    materials: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, Any] = field(default_factory=dict)
    snapshot_refs: dict[str, frozenset[str]] = field(default_factory=dict)
    snapshot_encodings: dict[str, str] = field(default_factory=dict)
    restored_encodings: dict[str, str] = field(default_factory=dict)
    verified_objects: dict[str, tuple[str, str]] = field(default_factory=dict)
    object_refs: dict[str, set[str]] = field(default_factory=dict)
    byte_length: int = 0
    timelines: dict[str, sqlite_timeline.TimelineScope] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    paths: dict[str, ObjectPath] = field(default_factory=dict)


def _parsed_rows(rows: Iterable[tuple[str, str, str]], context: DecodeContext) -> list[ParsedRow]:
    """Parse each exact source or synthetic path once in this read only."""
    result = []
    paths = context.paths
    for raw, kind, encoded in rows:
        path = paths.get(raw)
        if path is None:
            path = object_path(raw)
            paths[raw] = path
        result.append((raw, path, kind, encoded))
    return result


def _overridden(rows: list[ParsedRow]) -> set[ObjectPath]:
    return {path[:length] for _, path, _, _ in rows for length in range(1, len(path))}


def _sql_rows(repository: SQLiteStateRepository, connection: sqlite3.Connection,
              query: str, parameters: tuple[Any, ...], columns: tuple[str, ...], *, byte_count: int,
              ) -> list[tuple[Any, ...]]:
    if repository._aggregate_rows and byte_count <= 512 * 1024:
        try:
            encoded = connection.execute(
                "SELECT json_group_array(json_array(" + ",".join(columns) + ")) FROM (" + query + ")",
                parameters,
            ).fetchone()[0]
            return [tuple(row) for row in json.loads(encoded)]
        except sqlite3.OperationalError as error:
            if "no such function" not in str(error):
                raise
            repository._aggregate_rows = False
    return list(connection.execute(query, parameters))


def _prefetch_materials(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                        rows: Iterable[tuple[str, str, str]], context: DecodeContext) -> None:
    """Read only selected paths' dependency metadata, then their bounded bodies."""
    paths = iter(path for path, kind, _ in rows if kind in {"material", "snapshot"} and path not in context.object_refs)
    while batch := tuple(islice(paths, 256)):
        marks = ",".join("?" for _ in batch)
        predicate = "r.run_id = ? AND r.object_path IN (" + marks + ")"
        parameters = (run_id, *batch)
        byte_count = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB)) + LENGTH(material_id) + 80), 0) "
            "FROM run_object_material_refs r WHERE " + predicate, parameters,
        ).fetchone()[0]
        if byte_count > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
        dependencies = _sql_rows(repository, connection,
            "SELECT r.object_path, r.material_id, m.byte_length, LENGTH(CAST(m.value AS BLOB)) AS actual_length, "
            "rr.material_id AS linked FROM run_object_material_refs r "
            "LEFT JOIN run_materials m ON m.material_id = r.material_id "
            "LEFT JOIN run_material_refs rr ON rr.run_id = r.run_id AND rr.material_id = r.material_id WHERE " + predicate,
            parameters, ("object_path", "material_id", "byte_length", "actual_length", "linked"), byte_count=byte_count)
        for path in batch:
            context.object_refs[path] = set()
        needed: dict[str, int] = {}
        for path, identity, length, actual, linked in dependencies:
            if linked != identity or type(length) is not int or length != actual or not 0 <= length <= 8 * repository.max_bytes:
                raise ValueError("Invalid SQLite Run material byte length or reference")
            context.object_refs[path].add(identity)
            if identity not in context.materials:
                needed[identity] = length
        if context.byte_length + sum(needed.values()) > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
        identities = iter(needed)
        while materials := tuple(islice(identities, 256)):
            material_rows = _sql_rows(repository, connection,
                "SELECT material_id, value, byte_length FROM run_materials WHERE material_id IN ("
                + ",".join("?" for _ in materials) + ")", materials, ("material_id", "value", "byte_length"),
                byte_count=sum(needed[identity] + 80 for identity in materials))
            if len(material_rows) != len(materials):
                raise ValueError("Missing SQLite Run material")
            for identity, value, length in material_rows:
                if not isinstance(identity, str) or not isinstance(value, str):
                    raise ValueError("Invalid SQLite Run material type")
                raw = value.encode("utf-8")
                if len(raw) != length or hashlib.sha256(raw).hexdigest() != identity:
                    raise ValueError("Invalid SQLite Run material identity")
                context.materials[identity] = value
                context.byte_length += length


def _decode(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
            rows: list[tuple[str, str, str]], *, context: DecodeContext | None = None,
            thin_timeline: bool = False,
            parsed_rows: list[ParsedRow] | None = None,
            ) -> tuple[dict[str, Any], dict[str, str]]:
    shared = context is not None
    context = context or DecodeContext()
    parsed = parsed_rows if parsed_rows is not None else _parsed_rows(rows, context)
    _prefetch_materials(repository, connection, run_id, rows, context)
    materials = context.materials
    with repository._read_view_lock:
        cached = repository._read_view
    prior = cached.view if cached is not None and cached.view.run_id == run_id else None
    if prior is not None:
        # These bounded encodings came from a proved complete read. Reuse only
        # after the current snapshot object and every material agree again.
        for cached_path, _, kind, encoded in parsed:
            if kind != "snapshot" or prior.objects.get(cached_path) != (kind, encoded):
                continue
            references = prior.snapshot_refs.get(cached_path)
            plain = prior.snapshots.get(cached_path)
            if references is not None and plain is not None and references <= materials.keys():
                identity = scalar_value(encoded)
                context.snapshot_encodings[identity] = plain
                context.snapshot_refs[identity] = references
    visiting: set[str] = set()

    def material(identity: str) -> None:
        if identity in materials:
            return
        bounds = connection.execute("SELECT byte_length, LENGTH(CAST(value AS BLOB)) FROM run_materials WHERE material_id = ?", (identity,)).fetchone()
        if bounds is None or type(bounds[0]) is not int or bounds[0] != bounds[1] or not 0 <= bounds[0] <= 8 * repository.max_bytes:
            raise ValueError("Invalid SQLite Run material byte length")
        row = connection.execute("SELECT value, byte_length FROM run_materials WHERE material_id = ?", (identity,)).fetchone()
        linked = connection.execute("SELECT 1 FROM run_material_refs WHERE run_id = ? AND material_id = ?",
                                    (run_id, identity)).fetchone()
        if row is None or linked is None:
            raise ValueError("Missing SQLite Run material")
        value, length = row
        if not isinstance(value, str):
            raise ValueError("Invalid SQLite Run material type")
        raw = value.encode("utf-8")
        if len(raw) != length or hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError("Invalid SQLite Run material identity")
        materials[identity] = value
        context.byte_length += length
        if context.byte_length > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")

    def restore(identity: str) -> Any:
        if identity in context.snapshots:
            return context.snapshots[identity]
        if identity in context.snapshot_encodings:
            context.snapshots[identity] = json.loads(context.snapshot_encodings[identity])
            return context.snapshots[identity]
        if identity in visiting:
            raise ValueError("Invalid SQLite Run cyclic material")
        material(identity)
        visiting.add(identity)
        dependencies = {identity}
        restored = snapshot(json.loads(materials[identity]), dependencies)
        visiting.remove(identity)
        context.snapshots[identity] = restored
        context.snapshot_refs[identity] = frozenset(dependencies)
        context.restored_encodings[identity] = encode_json(restored)
        return restored

    def snapshot(value: Any, dependencies: set[str]) -> Any:
        if not isinstance(value, list):
            return value
        if len(value) != 2:
            raise ValueError("Invalid SQLite Run snapshot")
        kind, content = value
        if kind == "map" and isinstance(content, dict):
            return {key: snapshot(item, dependencies) for key, item in content.items()}
        if kind == "list" and isinstance(content, list):
            return [snapshot(item, dependencies) for item in content]
        if kind == "material" and isinstance(content, str):
            material(content)
            dependencies.add(content)
            return materials[content]
        if kind == "snapshot" and isinstance(content, str):
            restored = restore(content)
            dependencies.update(context.snapshot_refs[content])
            return restored
        raise ValueError("Missing or invalid SQLite Run snapshot material")

    complete_fields = reuse_fields(prior, parsed, context)
    context.fields.update(complete_fields)
    scopes = sqlite_timeline.prove(parsed, context, restore,
                                   sources=prior.timeline_sources if prior is not None else None)
    if thin_timeline:
        context.timelines = scopes
    else:
        # The same current object/material proof also permits one complete
        # canonical field copy, avoiding per-event restoration and assembly.
        context.fields.update((key, scope.encoded) for key, scope in scopes.items())
    restored_fields = {key: json.loads(encoded) for key, encoded in complete_fields.items()}
    if not thin_timeline:
        restored_fields.update((key, json.loads(scope.encoded)) for key, scope in scopes.items())
    for timeline_path, path, kind, encoded in parsed:
        if path and path[0] in scopes and kind == "snapshot":
            context.verified_objects[timeline_path] = kind, encoded
    skipped = scopes.keys() | complete_fields.keys()
    decoded_rows = [row for row in parsed if not row[1] or row[1][0] not in skipped] if skipped else parsed

    nodes: dict[tuple[str | int, ...], Any] = {}
    lengths: dict[tuple[str | int, ...], int] = {}
    for encoded_path, path, kind, encoded in decoded_rows:
        value = scalar_value(encoded)
        if kind in {"material", "snapshot"} and isinstance(value, str):
            material(value)
            if kind == "material":
                value = materials[value]
            elif value in context.snapshot_encodings:
                # Each node owns a new tree. Descendant overrides must never
                # mutate the context's separately restored immutable baseline.
                value = json.loads(context.snapshot_encodings[value])
            else:
                restore(value)
                value = json.loads(context.restored_encodings[value])
        elif kind == "map" and value is None:
            value = {}
        elif kind == "list" and type(value) is int and 0 <= value <= repository.max_bytes:
            lengths[path] = value
            value = []
        elif kind != "scalar" or isinstance(value, dict | list):
            raise ValueError("Invalid SQLite Run object value")
        if path in nodes:
            raise ValueError("Duplicate SQLite Run object path")
        nodes[path] = value
    child_counts = dict.fromkeys(lengths, 0)
    for path in nodes:
        parent_path = path[:-1]
        if path and parent_path in lengths:
            index = path[-1]
            if type(index) is not int or not 0 <= index < lengths[parent_path]:
                raise ValueError("Invalid SQLite Run list item")
            child_counts[parent_path] += 1
    for path, length in lengths.items():
        if child_counts[path] != length:
            raise ValueError("Missing SQLite Run list item")
        nodes[path] = [None] * length
    root = nodes.get(())
    if not isinstance(root, dict):
        raise ValueError("Missing SQLite Run object root")
    root.update(restored_fields)
    for path in sorted(nodes, key=len):
        if not path:
            continue
        parent, key = nodes.get(path[:-1]), path[-1]
        if isinstance(parent, dict) and isinstance(key, str):
            parent[key] = nodes[path]
        elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent):
            parent[key] = nodes[path]
        else:
            raise ValueError("Invalid SQLite Run object parent")
    if not shared:
        repository._check_size(root)
    for encoded_path, path, kind, encoded in parsed:
        if kind not in {"material", "snapshot"}:
            continue
        if context.verified_objects.get(encoded_path) == (kind, encoded):
            continue
        actual = context.object_refs[encoded_path]
        identity = scalar_value(encoded)
        expected = {identity} if kind == "material" else set(context.snapshot_refs[identity])
        if actual != expected:
            raise ValueError("Invalid SQLite Run object material references")
        context.verified_objects[encoded_path] = (kind, encoded)
    return root, materials



def _object_rows(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                 paths: list[str]) -> list[tuple[str, str, str]]:
    rows = []
    iterator = iter(paths)
    while batch := tuple(islice(iterator, 256)):
        predicate = "run_id = ? AND object_path IN (" + ",".join("?" for _ in batch) + ")"
        parameters = (run_id, *batch)
        byte_count = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB)) + LENGTH(CAST(value_json AS BLOB))), 0) FROM run_objects WHERE " + predicate, parameters).fetchone()[0]
        if byte_count > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
        rows.extend(_sql_rows(repository, connection,
            "SELECT object_path, kind, value_json FROM run_objects WHERE " + predicate, parameters,
            ("object_path", "kind", "value_json"), byte_count=byte_count))
    return rows
