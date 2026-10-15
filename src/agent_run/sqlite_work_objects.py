"""Indexed work projections and object-scoped material ownership."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import copy_json_tree, encode_json, object_path, scalar_value, snapshot_fields
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS
from agent_run.sqlite_work_query import MaterialBundle, read_work
from agent_run import sqlite_job_history, sqlite_timeline
from agent_run.sqlite_related_attempts import archive_rows, read_related, decode_related
from agent_run.sqlite_work_indexes import IndexFacts, check_facts
from agent_run.sqlite_work_fields import reuse_fields

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository

_HISTORY = "agent_invocation_history"
_HISTORY_LOW = '["agent_invocation_history",'
_HISTORY_HIGH = '["agent_invocation_history"-'
_HROOT = encode_json((_HISTORY,))
_CANONICAL = "ticket_agent_invocations"
_CANONICAL_LOW = '["ticket_agent_invocations",'
_CANONICAL_HIGH = '["ticket_agent_invocations"-'
ObjectPath = tuple[str | int, ...]
ParsedRow = tuple[str, ObjectPath, str, str]


@dataclass
class WorkView:
    run_id: str
    baseline: dict[str, Any]
    positions: list[int]
    count: int
    logical_bytes: int
    objects: dict[str, tuple[str, str]]
    materials: dict[str, str]
    canonical_count: int
    hidden_history: dict[str, tuple[int, int]]
    history_sizes: dict[str, tuple[int, int]]
    ticket_number: int | None
    total_changes: int
    timelines: dict[str, sqlite_timeline.TimelineScope] = field(default_factory=dict)


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


def _history_row(run_id: str, position: int, entry: Any, *, canonical_encoding: str | None = None) -> tuple[Any, ...]:
    """Derive metadata, optionally sizing this read's proved unmodified text."""
    value = entry if isinstance(entry, dict) else {}
    ticket = value.get("ticket_number")
    subject = value.get("work_subject")
    if type(ticket) is not int and isinstance(subject, str) and subject.startswith("ticket:"):
        try:
            ticket = int(subject.removeprefix("ticket:"))
        except ValueError:
            ticket = None
    if type(ticket) is not int:
        ticket = None
    texts = [value.get(key) for key in ("work_subject", "started_at", "resume_id")]
    detailed = any(isinstance(value.get(key), list) and value[key]
                   for key in ("execution_segments", "recovery_wait_intervals"))
    return (run_id, position, ticket, *(text if isinstance(text, str) else None for text in texts),
            len((canonical_encoding if canonical_encoding is not None else encode_json(entry)).encode("utf-8")), int(detailed))


def index_run(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
              state: Mapping[str, Any], objects: Mapping[str, tuple[str, str]],
              materials: Mapping[str, str]) -> None:
    """Build sparse indexes only at the explicitly complete dictionary boundary."""
    canonical = state.get(_CANONICAL)
    connection.execute("INSERT OR REPLACE INTO run_sizes VALUES (?, ?, ?)",
                       (run_id, repository._check_size(state), len(canonical) if isinstance(canonical, dict) else 0))
    connection.execute("DELETE FROM run_history WHERE run_id = ?", (run_id,))
    history = state.get(_HISTORY)
    if isinstance(history, list):
        connection.executemany("INSERT INTO run_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (_history_row(run_id, position, entry) for position, entry in enumerate(history)))
    connection.executemany("INSERT INTO run_object_material_refs VALUES (?, ?, ?)",
                          ((run_id, path, identity) for path, (kind, encoded) in objects.items()
                           for identity in _references(repository, kind, encoded, materials)))
    sqlite_job_history.index(connection, run_id, state)


def check_indexes(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                  state: Mapping[str, Any], logical_bytes: int,
                  objects: Mapping[str, tuple[str, str]], materials: Mapping[str, str], *,
                  snapshot_refs: Mapping[str, frozenset[str]] | None = None,
                  snapshot_encodings: Mapping[tuple[str | int, ...], str] | None = None,
                  expected: IndexFacts | None = None) -> IndexFacts:
    """A complete query validates sparse metadata against complete stored facts."""
    if expected is None:
        canonical = state.get(_CANONICAL)
        history = state.get(_HISTORY)
        # These facts retain only scalar metadata and dependency identities,
        # never an additional decoded Job, Invocation or Review Context body.
        expected = IndexFacts(
            run_id, (logical_bytes, len(canonical) if isinstance(canonical, dict) else 0),
            tuple(_history_row(run_id, position, entry,
                              canonical_encoding=(snapshot_encodings or {}).get((_HISTORY, position)))
                  for position, entry in enumerate(history)) if isinstance(history, list) else (),
            frozenset((path, identity) for path, (kind, encoded) in objects.items()
                      # The complete source restoration proved these closures.
                      for identity in (snapshot_refs[path] if kind == "snapshot" and snapshot_refs is not None and path in snapshot_refs
                                       else _references(repository, kind, encoded, materials))),
            MappingProxyType(sqlite_job_history.sizes(state, snapshot_encodings=snapshot_encodings)),
            tuple(sorted(sqlite_job_history.attempt_rows(state))),
        )
    check_facts(connection, run_id, expected, max_bytes=repository.max_bytes)
    return expected


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


@dataclass
class WorkSource:
    """Common work facts proved by the current write transaction only."""
    run_id: str
    state: dict[str, Any]
    rows: list[tuple[str, str, str]]
    context: DecodeContext
    count: int
    size: tuple[int, int]
    history_root: tuple[str, str, str] | None
    total_changes: int


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
    _prefetch_materials(repository, connection, run_id,
                        ((raw, kind, encoded) for raw, path, kind, encoded in parsed if not path or path[0] != _HISTORY), context)
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
        # Synthetic consecutive history positions are checked against their
        # actual global paths by load_work after assembling the projection.
        if kind not in {"material", "snapshot"} or path and path[0] == _HISTORY:
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


def _object_row(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str, path: str) -> tuple[str, str, str] | None:
    bounds = connection.execute("SELECT LENGTH(CAST(object_path AS BLOB)) + LENGTH(CAST(value_json AS BLOB)) FROM run_objects WHERE run_id = ? AND object_path = ?", (run_id, path)).fetchone()
    if bounds is not None and bounds[0] > 8 * repository.max_bytes:
        raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
    row: tuple[str, str, str] | None = connection.execute("SELECT object_path, kind, value_json FROM run_objects WHERE run_id = ? AND object_path = ?", (run_id, path)).fetchone()
    return row


def _entry_rows(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str, positions: set[int]) -> list[tuple[str, str, str]]:
    paths = [encode_json((_HISTORY, position)) for position in sorted(positions)]
    rows = _object_rows(repository, connection, run_id, paths)
    if len(rows) != len(paths):
        raise ValueError("Missing SQLite Run history object")
    by_path = {row[0]: row for row in rows}
    return [by_path[path] for path in paths]


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


def _history_metadata(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                      positions: set[int]) -> dict[int, tuple[Any, ...]]:
    rows: dict[int, tuple[Any, ...]] = {}
    iterator = iter(sorted(positions))
    columns = ("run_id", "position", "ticket_number", "work_subject", "started_at", "resume_id", "logical_bytes", "detailed")
    while batch := tuple(islice(iterator, 256)):
        predicate = "run_id = ? AND position IN (" + ",".join("?" for _ in batch) + ")"
        parameters = (run_id, *batch)
        byte_count = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(work_subject AS BLOB)) + LENGTH(CAST(started_at AS BLOB)) + COALESCE(LENGTH(CAST(resume_id AS BLOB)), 0) + 128), 0) FROM run_history WHERE " + predicate, parameters).fetchone()[0]
        if byte_count > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
        for row in _sql_rows(repository, connection, "SELECT " + ",".join(columns) + " FROM run_history WHERE " + predicate,
                             parameters, columns, byte_count=byte_count):
            rows[row[1]] = row
    return rows


def _copy_work_fields(state: dict[str, Any], fields: Mapping[str, str], *,
                      fresh_snapshots: Mapping[tuple[str | int, ...], Any] | None = None,
                      snapshot_encodings: Mapping[tuple[str | int, ...], str] | None = None) -> dict[str, Any]:
    """Copy only proved encodings with the C codec; other trees stay isolated."""
    fresh = fresh_snapshots or {}
    encodings = snapshot_encodings or {}
    if not fresh and not encodings:
        return {key: json.loads(fields[key]) if key in fields else copy_json_tree(value)
                for key, value in state.items()}
    fresh_children = {path[0] for path in fresh if len(path) == 2}

    def child(path: tuple[str | int, ...], value: Any) -> Any:
        if path in fresh:
            return fresh[path]
        return json.loads(encodings[path]) if path in encodings else copy_json_tree(value)

    result = {}
    for key, value in state.items():
        if (key,) in fresh:
            result[key] = fresh[(key,)]
        elif key in fields and key not in fresh_children:
            result[key] = json.loads(fields[key])
        elif isinstance(value, dict) and any((key, part) in fresh or (key, part) in encodings for part in value):
            result[key] = {part: child((key, part), item) for part, item in value.items()}
        elif isinstance(value, list) and any((key, index) in fresh or (key, index) in encodings for index in range(len(value))):
            result[key] = [child((key, index), item) for index, item in enumerate(value)]
        else:
            result[key] = json.loads(fields[key]) if key in fields else copy_json_tree(value)
    return result


def _fresh_baseline_snapshots(
    objects: Mapping[str, tuple[str, str]], context: DecodeContext,
    encodings: Mapping[tuple[str | int, ...], str], positions: list[int],
) -> tuple[dict[tuple[str | int, ...], Any], dict[tuple[str | int, ...], str]]:
    """Borrow disjoint fresh trees; caller nodes were separately JSON-decoded."""
    slots: dict[str | int, int] = {position: local for local, position in enumerate(positions)}
    fresh, local_encodings = {}, {}
    borrowed: set[str] = set()
    for path, plain in encodings.items():
        # Top-level owners and collection children cover the assembled fields.
        # Deeper nodes retain the ordinary independent field-copy path.
        if not 1 <= len(path) <= 2 or path[0] in context.timelines:
            continue
        local = (_HISTORY, slots[path[1]]) if path[:1] == (_HISTORY,) and len(path) == 2 else path
        local_encodings[local] = plain
        identity = scalar_value(objects[encode_json(path)][1])
        if identity not in context.restored_encodings or identity not in context.snapshots:
            continue
        # Different roots can share a restored nested snapshot. Borrow its
        # containers at most once, so baseline nodes remain independent too.
        dependencies = context.snapshot_refs[identity] & context.snapshots.keys()
        if borrowed & dependencies:
            continue
        borrowed.update(dependencies)
        fresh[local] = context.snapshots[identity]
    return fresh, local_encodings


def _verified_snapshot_encodings(
    objects: Mapping[str, tuple[str, str]], context: DecodeContext,
    *, parsed_rows: list[ParsedRow] | None = None, overridden: set[ObjectPath] | None = None,
) -> dict[tuple[str | int, ...], str]:
    """Canonical text only for current closures without descendant overrides."""
    parsed = parsed_rows if parsed_rows is not None else _parsed_rows(
        ((path, kind, encoded) for path, (kind, encoded) in objects.items()), context)
    overridden = overridden if overridden is not None else _overridden(parsed)
    encodings = {}
    for raw_path, path, kind, encoded in parsed:
        if kind != "snapshot" or path in overridden or path and path[0] in context.timelines:
            continue
        identity = scalar_value(encoded)
        refs = context.snapshot_refs.get(identity)
        actual = context.object_refs.get(raw_path)
        if refs is None or actual is None or len(actual) != len(refs) or not refs.issubset(actual):
            continue
        plain = context.snapshot_encodings.get(identity, context.restored_encodings.get(identity))
        if plain is not None:
            encodings[path] = plain
    return encodings


def _work_encoded_fields(
    repository: SQLiteStateRepository, run_id: str, state: dict[str, Any],
    objects: Mapping[str, tuple[str, str]], context: DecodeContext,
    *, parsed_rows: list[ParsedRow] | None = None, overridden: set[ObjectPath] | None = None,
) -> dict[str, str]:
    """Reuse canonical encodings only after proving their current dependencies."""
    with repository._read_view_lock:
        cached = repository._read_view
    prior = cached.view if cached is not None and cached.view.run_id == run_id else None
    parsed = parsed_rows if parsed_rows is not None else _parsed_rows(
        ((path, kind, encoded) for path, (kind, encoded) in objects.items()), context)
    overridden = overridden if overridden is not None else _overridden(parsed)
    encodings = {}
    for encoded, path, kind, value in parsed:
        if path and (path[0] in context.fields or path[0] in context.timelines):
            continue
        # Sparse Invocation positions differ from their decoded list indices.
        item = kind, value
        if (kind != "snapshot" or path[:1] == (_HISTORY,) or path in overridden
                or context.verified_objects.get(encoded) != item):
            continue
        identity = scalar_value(item[1])
        if identity in context.restored_encodings:
            # This read restored the exact current closure. Its canonical
            # encoding can serve the later size and isolated baseline copy.
            encodings[path] = context.restored_encodings[identity]
            continue
        if prior is None or prior.objects.get(encoded) != item:
            continue
        references = prior.snapshot_refs.get(encoded)
        if references is not None and references == context.snapshot_refs.get(identity):
            # The text was encoded from the restored JSON, rather than copied
            # from a potentially noncanonical material representation.
            encodings[path] = prior.snapshots[encoded]
    return {**snapshot_fields(state, encodings), **context.fields}


def load_work(repository: SQLiteStateRepository, run_id: str, ticket_number: int | None,
              *, thin_timeline: bool = False) -> dict[str, Any] | None:
    repository._local.last_read_view = None
    repository._local.work_projection_bytes = None
    # No run_id argument: the complete-Run SQL aggregation would read siblings.
    with repository._reading(check_schema=False) as connection:
        view = getattr(repository._local, "work_view", None)
        if (connection is not None and repository.in_transaction and isinstance(view, WorkView)
                and view.run_id == run_id and view.ticket_number == ticket_number
                and view.total_changes == connection.total_changes
                and (thin_timeline or not view.timelines)):
            # DDL does not affect total_changes. A cached writer projection
            # must still check the current structure after arbitrary callbacks.
            # Native reads instead prove it in their current source query.
            repository._check_schema(connection)
            # The writer owns this unchanged SQL snapshot; any SQL mutation,
            # save or transaction exit invalidates it. Authorization stays fresh.
            repository._local.work_timelines = view.timelines
            cached: dict[str, Any] = copy_json_tree(view.baseline)
            return cached
        repository._local.work_view = None
        repository._local.work_archives = {}
        repository._local.work_attempts = {}
        repository._local.work_timelines = {}
        if connection is None:
            return None
        if repository._aggregate_rows:
            native = _native_work(repository, connection, run_id, ticket_number, thin_timeline=thin_timeline)
            if native is not None:
                return native
        # Missing JSON functions, noncanonical compatible DDL and large work
        # projections keep the original schema and sparse-read fallback.
        if not repository._check_schema(connection, allow_empty=True):
            return None
        rows: list[tuple[str, str, str]]
        root: dict[str, Any]
        source = getattr(repository._local, "work_source", None)
        if (not thin_timeline and repository.in_transaction and isinstance(source, WorkSource) and source.run_id == run_id
                and source.total_changes == connection.total_changes):
            root = copy_json_tree(source.state)
            rows, context = source.rows, source.context
            count, size, history_root = source.count, source.size, source.history_root
            materials = context.materials
        else:
            if connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                return None
            size = connection.execute("SELECT logical_bytes, canonical_count FROM run_sizes WHERE run_id = ?", (run_id,)).fetchone()
            if size is None or type(size[0]) is not int or not 3 <= size[0] <= repository.max_bytes:
                raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit or has invalid size metadata")
            if type(size[1]) is not int or not 0 <= size[1] <= repository.max_bytes:
                raise ValueError("Invalid SQLite Run canonical count")
            # Explicit PK ranges exclude both historical and sibling canonical bodies.
            ranges = [(None, _HISTORY_LOW), (_HISTORY_HIGH, _CANONICAL_LOW), (_CANONICAL_HIGH, None)]
            rows = []
            physical = 0
            for low, high in ranges:
                predicate = "o.run_id = ?" + (" AND o.object_path >= ?" if low else "") + (" AND o.object_path < ?" if high else "")
                predicate += " AND " + sqlite_job_history.CURRENT_WORK_PREDICATE
                predicate += " AND object_path NOT IN (SELECT object_path FROM run_job_history WHERE run_id=?)"
                params = (run_id, *((low,) if low else ()), *((high,) if high else ()), run_id)
                range_bytes = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB)) + LENGTH(CAST(value_json AS BLOB))), 0) FROM run_objects o INDEXED BY current_work_objects WHERE " + predicate, params).fetchone()[0]
                physical += range_bytes
                if physical > 8 * repository.max_bytes:
                    raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
                rows.extend(_sql_rows(repository, connection,
                    "SELECT object_path, kind, value_json FROM run_objects o INDEXED BY current_work_objects WHERE " + predicate, params,
                    ("object_path", "kind", "value_json"), byte_count=range_bytes))
            history_root = next((row for row in rows if row[0] == _HROOT), None)
            count = scalar_value(history_root[2]) if history_root is not None and history_root[1] == "list" else 0
            if type(count) is not int or count < 0 or count > repository.max_bytes:
                raise ValueError("Invalid SQLite Run history length")
            rows = [(path, kind, "0" if path == _HROOT else encoded) for path, kind, encoded in rows]
            context = DecodeContext()
            root, materials = _decode(repository, connection, run_id, rows, context=context, thin_timeline=thin_timeline)
            if repository.in_transaction and not thin_timeline:
                repository._local.work_source = WorkSource(run_id, copy_json_tree(root), rows, context, count, size, history_root, connection.total_changes)
        archive_sizes = sqlite_job_history.read_sizes(connection, run_id, repository.max_bytes)
        selected_archives = {encode_json(("ticket_jobs", str(ticket_number), field)) for field in sqlite_job_history.FIELDS}
        active = root.get("active_ticket_job")
        if isinstance(active, dict) and active.get("ticket_number") == ticket_number and ticket_number is not None:
            selected_archives.update(encode_json(("active_ticket_job", field)) for field in sqlite_job_history.FIELDS)
        selected_rows = archive_rows(repository, connection, run_id, sorted(selected_archives & archive_sizes.keys()))
        if selected_rows:
            # Decode alongside the base so child nodes override their Job
            # container while preserving all already verified current facts.
            root, materials = _decode(repository, connection, run_id, rows + selected_rows, context=context, thin_timeline=thin_timeline)
            rows = rows + selected_rows
        hidden_history = {path: size for path, size in archive_sizes.items() if path not in selected_archives}
        repository._local.work_archives = hidden_history
        repository._local.work_attempts = read_related(repository, connection, run_id, root, hidden_history, context)
        sqlite_job_history.check_selected(root, archive_sizes)
        positions: set[int] = set()
        current_row = None
        if ticket_number is not None:
            current_path = encode_json((_CANONICAL, str(ticket_number)))
            current_row = _object_row(repository, connection, run_id, current_path)
            if current_row is not None:
                current, content = _decode(repository, connection, run_id, [("[]", "map", "null"), (encode_json((_CANONICAL,)), "map", "null"), current_row], context=context)
                invocation = current[_CANONICAL][str(ticket_number)]
                materials.update(content)
                if isinstance(invocation, dict):
                    positions.update(row[0] for row in connection.execute(
                        "SELECT position FROM run_history WHERE run_id = ? AND work_subject = ? AND started_at = ?",
                        (run_id, invocation.get("work_subject"), invocation.get("started_at"))))
            # The next append can move this boundary item out of the 256 window.
            positions.update(row[0] for row in connection.execute(
                "SELECT position FROM run_history WHERE run_id = ? AND detailed = 1 AND position < ?",
                (run_id, max(count - MAX_DETAILED_INVOCATIONS + 1, 0))))
        audit = root.get("resume_audit")
        events = audit.get("history", []) if isinstance(audit, dict) else []
        for event in events:
            if isinstance(event, dict) and event.get("successor_invocation_started_at") is not None:
                positions.update(row[0] for row in connection.execute(
                    "SELECT position FROM run_history WHERE run_id = ? AND resume_id = ?", (run_id, event.get("resume_id"))))
        if any(position < 0 or position >= count for position in positions):
            raise ValueError("Invalid SQLite Run history index")
        selected_rows = _entry_rows(repository, connection, run_id, positions)
        _prefetch_materials(repository, connection, run_id, selected_rows, context)
        # Decode selected entries under local, consecutive list positions.
        local_rows = [(encode_json((_HISTORY, local)), kind, encoded)
                      for local, (_, kind, encoded) in enumerate(selected_rows)]
        projected, history_materials = _decode(repository, connection, run_id,
            [("[]", "map", "null"), (_HROOT, "list", str(len(local_rows))), *local_rows], context=context)
        if history_root is not None:
            root[_HISTORY] = projected[_HISTORY]
        materials.update(history_materials)
        metadata = _history_metadata(repository, connection, run_id, positions)
        for position, entry in zip(sorted(positions), projected[_HISTORY], strict=True):
            actual = metadata.get(position)
            if actual != _history_row(run_id, position, entry):
                raise ValueError("Invalid SQLite Run history index")
        for path, kind, encoded in selected_rows:
            actual_refs = context.object_refs[path]
            identity = scalar_value(encoded)
            expected = {identity} if kind == "material" else set(context.snapshot_refs[identity]) if kind == "snapshot" else set()
            if actual_refs != expected:
                raise ValueError("Invalid SQLite Run object material references")
        canonical_keys = {str(ticket_number)} if ticket_number is not None else set()
        active = root.get("active_ticket_job")
        if isinstance(active, dict) and type(active.get("ticket_number")) is int:
            canonical_keys.add(str(active["ticket_number"]))
        for entry in projected[_HISTORY]:
            if isinstance(entry, dict):
                subject = entry.get("work_subject")
                if isinstance(subject, str) and subject.startswith("ticket:"):
                    canonical_keys.add(subject.removeprefix("ticket:"))
        canonical_paths = [encode_json((_CANONICAL, key)) for key in canonical_keys if key != str(ticket_number) or current_row is None]
        canonical_rows = _object_rows(repository, connection, run_id, canonical_paths)
        if current_row is not None:
            canonical_rows.append(current_row)
        if canonical_rows:
            canonical, content = _decode(repository, connection, run_id, [("[]", "map", "null"), (encode_json((_CANONICAL,)), "map", "null"), *canonical_rows], context=context)
            root[_CANONICAL] = canonical[_CANONICAL]
            materials.update(content)
        objects = {path: (kind, encoded) for path, kind, encoded in rows + selected_rows + canonical_rows}
        if history_root is not None:
            objects[_HROOT] = ("list", str(count))
        field_encodings = _work_encoded_fields(repository, run_id, root, objects, context)
        repository._local.work_projection_bytes = repository._check_size(root, encoded_fields=field_encodings,
            unloaded_fields={key: scope.encoded for key, scope in context.timelines.items()})
        repository._local.work_timelines = context.timelines
        if repository.in_transaction:
            repository._local.work_view = WorkView(run_id, _copy_work_fields(root, field_encodings), sorted(positions), count, size[0], objects, materials, size[1], hidden_history, archive_sizes, ticket_number, connection.total_changes, context.timelines)
        return root


def _native_work(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                 ticket_number: int | None, *, thin_timeline: bool = False) -> dict[str, Any] | None:
    from agent_run.sqlite_state import _WORK_SCHEMA_PARAMETERS

    parameters = {"run_id": run_id, "ticket_number": ticket_number,
        "ticket_path": encode_json((_CANONICAL, str(ticket_number))) if ticket_number is not None else None,
        "history_low": _HISTORY_LOW, "history_high": _HISTORY_HIGH,
        "canonical_low": _CANONICAL_LOW, "canonical_high": _CANONICAL_HIGH,
        "history_root": _HROOT, "limit_bytes": 8 * repository.max_bytes,
        "job_number": str(ticket_number),
        "aggregate_bytes": min(512 * 1024, 8 * repository.max_bytes), **_WORK_SCHEMA_PARAMETERS}
    payload = read_work(connection, parameters)
    if payload is None:
        repository._aggregate_rows = False
        return None
    logical_bytes, canonical_count, count, physical, object_json, material_json, ref_json, history_json, related_json, related_metadata_json, archive_sizes_json, schema_proved = payload
    # Exact current canonical DDL proves its structural PRAGMA facts. A
    # compatible alternative definition still receives the complete check.
    if not schema_proved and not repository._check_schema(connection, allow_empty=True):
        return None
    if logical_bytes is None:
        return None
    if object_json is None:
        return None
    if (type(logical_bytes) is not int or not 3 <= logical_bytes <= repository.max_bytes
            or type(canonical_count) is not int or not 0 <= canonical_count <= repository.max_bytes
            or type(count) is not int or not 0 <= count <= repository.max_bytes):
        raise ValueError("Invalid SQLite Run size metadata")
    rows = [tuple(row) for row in json.loads(object_json)]
    related_rows = [tuple(row) for row in json.loads(related_json)]
    metadata = {row[1]: tuple(row) for row in json.loads(history_json)}
    positions = sorted(metadata)
    if any(position < 0 or position >= count for position in positions):
        raise ValueError("Invalid SQLite Run history index")
    context = DecodeContext()
    parsed_source = _parsed_rows(rows, context)
    for path, _, _ in rows + related_rows:
        context.object_refs[path] = set()
    for path, identity in json.loads(ref_json):
        context.object_refs[path].add(identity)
    if not isinstance(material_json, MaterialBundle) or not isinstance(material_json.data, bytes):
        raise ValueError("Invalid SQLite Run material bundle")
    with repository._read_view_lock:
        complete = repository._read_view
    known_materials = (complete.view.materials if complete is not None and complete.view.run_id == run_id else {})
    offset = 0
    for identity, length, actual, linked, identity_type, value_type, length_type, linked_type in json.loads(material_json.metadata):
        if ((identity_type, value_type, length_type, linked_type) != ("text", "text", "integer", "text")
                or not isinstance(identity, str) or linked != identity
                or type(length) is not int or type(actual) is not int or length != actual
                or not 0 <= actual <= 8 * repository.max_bytes
                or offset + actual > len(material_json.data)):
            raise ValueError("Invalid SQLite Run material byte length or reference")
        raw = material_json.data[offset:offset + actual]
        offset += actual
        if len(raw) != length:
            raise ValueError("Invalid SQLite Run material identity")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid SQLite Run material UTF-8 encoding") from error
        # Strict UTF-8 has one byte representation for this exact text. The
        # bounded complete view already proved its SHA under this identity;
        # exact current text equality reuses only that pure calculation, never
        # SQL length/type/link/reference or ownership checks. Changed/new
        # contents still receive the actual SHA calculation below.
        if known_materials.get(identity) != text and hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError("Invalid SQLite Run material identity")
        context.materials[identity] = text
        context.byte_length += length
    if offset != len(material_json.data):
        raise ValueError("Invalid SQLite Run material bundle length")
    slots = {position: local for local, position in enumerate(positions)}
    decoded_rows = []
    decoded_parsed = []
    for source_row in parsed_source:
        path, parsed, kind, value = source_row
        if parsed == (_HISTORY,):
            value = str(len(positions))
            source_row = path, parsed, kind, value
        elif parsed[:1] == (_HISTORY,) and len(parsed) == 2:
            if parsed[1] not in slots:
                raise ValueError("Invalid SQLite Run history index")
            path = encode_json((_HISTORY, slots[parsed[1]]))
            source_row = _parsed_rows(((path, kind, value),), context)[0]
        decoded_rows.append((path, kind, value))
        decoded_parsed.append(source_row)
    root, materials = _decode(repository, connection, run_id, decoded_rows, context=context,
                              thin_timeline=thin_timeline, parsed_rows=decoded_parsed)
    selected = root.get(_HISTORY, [])
    if not isinstance(selected, list) or len(selected) != len(positions):
        raise ValueError("Missing SQLite Run history object")
    objects = {path: (kind, value) for path, kind, value in rows}
    overridden = _overridden(parsed_source)
    snapshot_encodings = _verified_snapshot_encodings(objects, context, parsed_rows=parsed_source, overridden=overridden)
    for position, entry in zip(positions, selected, strict=True):
        if metadata[position] != _history_row(run_id, position, entry,
                                              canonical_encoding=snapshot_encodings.get((_HISTORY, position))):
            raise ValueError("Invalid SQLite Run history index")
        path = encode_json((_HISTORY, position))
        row = objects.get(path)
        if row is None:
            raise ValueError("Missing SQLite Run history object")
        kind, identity = row[0], scalar_value(row[1])
        expected = {identity} if kind == "material" else set(context.snapshot_refs[identity]) if kind == "snapshot" else set()
        if context.object_refs[path] != expected:
            raise ValueError("Invalid SQLite Run object material references")
    field_encodings = _work_encoded_fields(repository, run_id, root, objects, context,
                                           parsed_rows=parsed_source, overridden=overridden)
    # The projection's local positions differ from persisted global positions.
    # Only this selection's proved, independently restored entries form its
    # complete field; metadata and actual reference checks above still apply.
    if all((_HISTORY, position) in snapshot_encodings for position in positions):
        field_encodings[_HISTORY] = "[" + ",".join(snapshot_encodings[(_HISTORY, position)] for position in positions) + "]"
    repository._local.work_projection_bytes = repository._check_size(root, encoded_fields=field_encodings,
            unloaded_fields={key: scope.encoded for key, scope in context.timelines.items()})
    archive_sizes = (sqlite_job_history.decode_sizes((tuple(row) for row in json.loads(archive_sizes_json)), repository.max_bytes)
                     if archive_sizes_json is not None
                     else sqlite_job_history.read_sizes(connection, run_id, repository.max_bytes))
    sqlite_job_history.check_selected(root, archive_sizes, snapshot_encodings=snapshot_encodings)
    selected_paths = {path for path, _, _ in rows}
    hidden_history = {path: size for path, size in archive_sizes.items() if path not in selected_paths}
    repository._local.work_archives = hidden_history
    repository._local.work_attempts = decode_related(repository, connection, run_id, related_rows,
        [tuple(row) for row in json.loads(related_metadata_json)], hidden_history, context)
    repository._local.work_timelines = context.timelines
    if repository.in_transaction:
        fresh_snapshots, local_encodings = _fresh_baseline_snapshots(objects, context, snapshot_encodings, positions)
        repository._local.work_view = WorkView(run_id, _copy_work_fields(root, field_encodings,
            fresh_snapshots=fresh_snapshots, snapshot_encodings=local_encodings), positions, count, logical_bytes,
            objects, materials, canonical_count, hidden_history, archive_sizes, ticket_number, connection.total_changes, context.timelines)
    return root
