"""Bounded row patches inside the repository's existing write transaction."""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import copy_json_tree, encode_json, object_path
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS, compact_invocation_details

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository

from agent_run.sqlite_work_patch import patch_work_objects
from agent_run import sqlite_job_history, sqlite_timeline

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


def _identity(entry: Any) -> tuple[Any, ...]:
    if isinstance(entry, dict) and isinstance(entry.get("work_subject"), str) and isinstance(entry.get("started_at"), str):
        return entry["work_subject"], entry["started_at"]
    return (encode_json(entry),)


def save_work(repository: SQLiteStateRepository, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any],
              *, timeline_updates: Mapping[str, dict[str, Any]] | None = None) -> None:
    from agent_run.sqlite_work_objects import (
        WorkView, _HISTORY, _HROOT, _CANONICAL, _history_row, _references, _decode, _entry_rows, _object_row,
    )

    view = getattr(repository._local, "work_view", None)
    if (not repository.in_transaction or not isinstance(view, WorkView) or view.run_id != run_id
            or view.total_changes != repository._local.connection.total_changes or not _same(view.baseline, baseline)):
        raise ValueError("SQLite work patch requires its current transaction baseline")
    timeline_updates = timeline_updates or {}
    if (timeline_updates.keys() - view.timelines.keys()
            or any(key in state or key in baseline for key in view.timelines)):
        raise ValueError("SQLite work patch cannot replace unloaded Timeline fields")
    connection = repository._local.connection
    from agent_run.sqlite_integrity import require_field_sizes
    from agent_run.sqlite_work_proofs import previous_objects, check_materials, sync_proofs

    require_field_sizes(connection, run_id, max_bytes=repository.max_bytes)
    old_history = baseline.get(_HISTORY, [])
    new_history = state.get(_HISTORY, [])
    if not isinstance(old_history, list) or not isinstance(new_history, list):
        raise ValueError("SQLite work history must remain an array")
    old_ids = [_identity(entry) for entry in old_history]
    new_ids = [_identity(entry) for entry in new_history]
    if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids):
        raise ValueError("SQLite work history identities must be unique")
    persisted_ids = set(old_ids)
    if [identity for identity in new_ids if identity in persisted_ids] != old_ids:
        raise ValueError("SQLite work patches cannot remove or reorder persisted Invocation history")
    positions = dict(zip(old_ids, view.positions, strict=True))
    count = view.count
    for identity in new_ids:
        if identity not in positions:
            positions[identity] = count
            count += 1
    entries = {positions[identity]: copy_json_tree(entry) for identity, entry in zip(new_ids, new_history, strict=True)}
    prior_entries = dict(zip(view.positions, old_history, strict=True))
    cutoff = max(count - MAX_DETAILED_INVOCATIONS, 0)
    # Multi-append callers can cross more than the one preloaded boundary.
    crossing = {row[0] for row in connection.execute(
        "SELECT position FROM run_history WHERE run_id = ? AND detailed = 1 AND position < ?",
        (run_id, cutoff))} - entries.keys()
    if crossing:
        rows = _entry_rows(repository, connection, run_id, crossing)
        local_rows = [(encode_json((_HISTORY, local)), kind, encoded) for local, (_, kind, encoded) in enumerate(rows)]
        extra, _ = _decode(repository, connection, run_id,
            [("[]", "map", "null"), (_HROOT, "list", str(len(rows))), *local_rows])
        prior_entries.update(zip(sorted(crossing), copy_json_tree(extra[_HISTORY]), strict=True))
        entries.update(zip(sorted(crossing), extra[_HISTORY], strict=True))
        view.objects.update((path, (kind, encoded)) for path, kind, encoded in rows)
    eligible = [entry for position, entry in entries.items() if position < cutoff]
    compact_state = dict(state)
    protected = dict(state.get(_CANONICAL) or {})
    for entry in eligible:
        if not isinstance(entry, dict):
            continue
        subject = entry.get("work_subject")
        if not isinstance(subject, str) or not subject.startswith("ticket:"):
            continue
        key = subject.removeprefix("ticket:")
        if key in protected:
            continue
        path = encode_json((_CANONICAL, key))
        row = _object_row(repository, connection, run_id, path)
        if row is not None:
            canonical, _ = _decode(repository, connection, run_id, [("[]", "map", "null"), (encode_json((_CANONICAL,)), "map", "null"), row])
            protected[key] = canonical[_CANONICAL][key]
    compact_state[_CANONICAL] = protected
    compact_state[_HISTORY] = eligible + [None] * MAX_DETAILED_INVOCATIONS
    compact_invocation_details(compact_state)

    replacements: dict[str, tuple[str, str]] = {}
    deleted: set[str] = set()
    materials: dict[str, str] = {}
    byte_delta = 0
    field_deltas: dict[str, int] = {}

    def add_bytes(field: str, amount: int) -> None:
        nonlocal byte_delta
        byte_delta += amount
        field_deltas[field] = field_deltas.get(field, 0) + amount
    moved_references: dict[str, set[str]] = {}
    canonical_count = view.canonical_count
    prior_archives = sqlite_job_history.histories(baseline)
    new_archives = {}
    for path, value in sqlite_job_history.histories(state).items():
        if path in prior_archives and _same(prior_archives[path], value):
            new_archives[path] = view.history_sizes[path]
        else:
            new_archives[path] = len(encode_json(value).encode()), len(value) if isinstance(value, list) else -1
    active_before, active_after = baseline.get("active_ticket_job"), state.get("active_ticket_job")
    active_replaced = (isinstance(active_before, dict) and
                       (not isinstance(active_after, dict) or active_before.get("ticket_number") != active_after.get("ticket_number")))
    removed_empty_archives: dict[tuple[str | int, ...], int] = {}
    for encoded, (size, _) in view.hidden_history.items():
        archive_path = object_path(encoded)
        if archive_path[0] == "active_ticket_job":
            owner = active_before
            removed = active_replaced
        else:
            owner = baseline.get("ticket_jobs", {}).get(archive_path[1])
            removed = archive_path[1] not in state.get("ticket_jobs", {})
        if encoded in new_archives and not (archive_path[0] == "active_ticket_job" and active_replaced):
            raise ValueError("SQLite work patch cannot replace unloaded Job history")
        if removed:
            # The original complete logical size includes this omitted field.
            # A pointer replacement/removal must subtract it without reading
            # its old Review Context bodies; ordinary thin Job updates keep it.
            add_bytes(str(archive_path[0]), -(len(encode_json(archive_path[-1]).encode()) + 1 + size + bool(owner)))
            if not owner:
                prefix = archive_path[:-1]
                removed_empty_archives[prefix] = removed_empty_archives.get(prefix, 0) + 1
            if encoded not in new_archives:
                deleted.add(encoded)
    for prefix, removed_count in removed_empty_archives.items():
        add_bytes(str(prefix[0]), -max(removed_count - 1, 0))

    def field_paths(key: str, child: str | None = None, *, complete: bool = False) -> set[str]:
        target = (key,) if child is None else (key, child)
        if complete:
            root = encode_json(target)
            return {row[0] for row in connection.execute(
                "SELECT object_path FROM run_objects WHERE run_id=? AND (object_path=? OR (object_path>=? AND object_path<?))",
                (run_id, root, root[:-1] + ",", root[:-1] + "-"))}
        return {path for path in view.objects if object_path(path)[:len(target)] == target}

    def encode_field(key: str, value: Any, child: str | None = None) -> None:
        prefix = (key,) if child is None else (key, child)
        untouched = {path for path in new_archives if object_path(path)[:-1] == prefix
                     and path in prior_archives and new_archives[path] == view.history_sizes[path]
                     and _same(prior_archives[path], value.get(object_path(path)[-1]))}
        if untouched:
            value = {field: item for field, item in value.items()
                     if encode_json((*prefix, field)) not in untouched}
        semantic = value.get(sqlite_job_history.FIELD) if isinstance(value, dict) else None
        if isinstance(semantic, list):
            value = {field: item for field, item in value.items() if field != sqlite_job_history.FIELD}
        selected = {key: value} if child is None else {key: {child: value}}
        objects, content = repository._objects(selected)
        materials.update(content)
        if isinstance(semantic, list):
            parent = encode_json((*prefix, sqlite_job_history.FIELD))
            objects[parent] = ("list", str(len(semantic)))
            old = prior_archives.get(parent, [])
            for position, attempt in enumerate(semantic):
                path = encode_json((*prefix, sqlite_job_history.FIELD, position))
                if position < len(old) and _same(old[position], attempt):
                    objects[path] = view.objects[path]
                    continue
                # Reuse the object codec for only the changed Attempt. Discard
                # the temporary owner's unreferenced empty snapshot material.
                item_objects, item_materials = repository._objects({"active_ticket_job": {sqlite_job_history.FIELD: [attempt]}})
                item = item_objects[encode_json(("active_ticket_job", sqlite_job_history.FIELD, 0))]
                objects[path] = item
                dependencies = _references(repository, *item, item_materials)
                materials.update((identity, item_materials[identity]) for identity in dependencies)
        wanted = field_paths(key, child, complete=key == "active_ticket_job" and active_replaced)
        wanted = {path for path in wanted if not any(object_path(path)[:len(object_path(root))] == object_path(root)
                                                    for root in untouched)}
        objects = {path: item for path, item in objects.items() if object_path(path)[:len(prefix)] == prefix}
        deleted.update(wanted - objects.keys())
        replacements.update((path, item) for path, item in objects.items() if view.objects.get(path) != item)

    def field_bytes(key: str, value: Any, child: str | None, archives: Mapping[str, tuple[int, int]]) -> int:
        prefix = (key,) if child is None else (key, child)
        if not isinstance(value, dict) or (key != "active_ticket_job" and key != "ticket_jobs"):
            return len(encode_json(value).encode())
        archive_fields = sqlite_job_history.FIELDS.intersection(value)
        core = {field: item for field, item in value.items() if field not in archive_fields}
        result = len(encode_json(core).encode())
        result += sum(len(encode_json(field).encode()) + 1 + archives[encode_json((*prefix, field))][0]
                      for field in archive_fields)
        return result + (len(archive_fields) if core else max(len(archive_fields) - 1, 0))

    def patch_timeline(key: str, old: list[Any], new: list[Any]) -> int:
        root = encode_json((key,))
        if view.objects.get(root) != ("list", str(len(old))):
            raise ValueError("Missing or invalid SQLite Run timeline object")
        prior: dict[int, dict[str, tuple[str, str]]] = {}
        for path in field_paths(key):
            parsed = object_path(path)
            if len(parsed) > 1:
                position = parsed[1]
                if type(position) is not int or not 0 <= position < len(old):
                    raise ValueError("Invalid SQLite Run timeline item")
                prior.setdefault(position, {})[path] = view.objects[path]
        delta = max(len(new) - 1, 0) - max(len(old) - 1, 0)
        for position in range(max(len(old), len(new))):
            path = encode_json((key, position))
            previous = prior.get(position, {})
            if position < len(old) and path not in previous:
                raise ValueError("Missing SQLite Run timeline item")
            if position < min(len(old), len(new)) and _same(old[position], new[position]):
                continue
            if position < len(old):
                delta -= len(encode_json(old[position]).encode())
            objects: dict[str, tuple[str, str]] = {}
            if position < len(new):
                delta += len(encode_json(new[position]).encode())
                item_objects, content = repository._objects({key: [new[position]]})
                materials.update(content)
                objects = {encode_json((key, position, *object_path(encoded)[2:])): item
                           for encoded, item in item_objects.items() if len(object_path(encoded)) > 1}
            deleted.update(previous.keys() - objects.keys())
            replacements.update((encoded, item) for encoded, item in objects.items() if previous.get(encoded) != item)
        if len(old) != len(new):
            replacements[root] = ("list", str(len(new)))
        return delta

    for key, event in timeline_updates.items():
        if not isinstance(event, dict):
            raise ValueError("Invalid SQLite Run Timeline event")
        scope = view.timelines[key]
        timeline_count = scope.count
        shift = key == "timeline_continuation" and timeline_count == sqlite_timeline.limit(key)
        if timeline_count >= sqlite_timeline.limit(key) and not shift:
            raise ValueError("Invalid SQLite Run Timeline capacity")
        add_bytes(key, len(encode_json(event).encode()))
        if shift:
            add_bytes(key, -len(scope.entries[0].encode()))
            # Move the exact already-proved objects and closures, never
            # re-encode old events or fetch them through another snapshot.
            for position in range(timeline_count - 1):
                source = encode_json((key, position + 1))
                target = encode_json((key, position))
                item = view.objects[source]
                if view.objects[target] != item:
                    replacements[target] = item
                    moved_references[target] = set(scope.references[position + 1])
            position = timeline_count - 1
        else:
            position = timeline_count
            add_bytes(key, int(bool(timeline_count)))
            replacements[encode_json((key,))] = ("list", str(timeline_count + 1))
        objects, content = repository._objects({key: [event]})
        materials.update(content)
        path = encode_json((key, position))
        replacements[path] = objects[encode_json((key, 0))]

    old_keys, new_keys = baseline.keys() - {_HISTORY}, state.keys() - {_HISTORY}
    for key in old_keys | new_keys:
        if key in old_keys and key in new_keys and _same(baseline[key], state[key]):
            continue
        if (key in {"timeline", "timeline_continuation"}
                and isinstance(baseline.get(key), list) and isinstance(state.get(key), list)):
            add_bytes(key, patch_timeline(key, baseline[key], state[key]))
            continue
        if key in {"ticket_jobs", "ticket_agent_invocations"} and isinstance(baseline.get(key), dict) and isinstance(state.get(key), dict):
            old, new = baseline[key], state[key]
            for child in old.keys() | new.keys():
                if child in old and child in new and _same(old[child], new[child]):
                    continue
                if child in old:
                    add_bytes(key, -(len(encode_json(child).encode()) + 1 + field_bytes(key, old[child], child, view.history_sizes)))
                if child in new:
                    add_bytes(key, len(encode_json(child).encode()) + 1 + field_bytes(key, new[child], child, new_archives))
                    encode_field(key, new[child], child)
                else:
                    deleted.update(field_paths(key, child, complete=True))
            if key == _CANONICAL:
                canonical_count = view.canonical_count + len(new) - len(old)
                add_bytes(key, max(canonical_count - 1, 0) - max(view.canonical_count - 1, 0))
            else:
                add_bytes(key, max(len(new) - 1, 0) - max(len(old) - 1, 0))
            continue
        if key == _CANONICAL:
            if isinstance(baseline.get(key), dict):
                raise ValueError("SQLite work patches cannot replace the sparse Invocation collection")
            canonical_count = len(state[key]) if isinstance(state.get(key), dict) else 0
        if key in old_keys:
            add_bytes(key, -(len(encode_json(key).encode()) + 1 + field_bytes(key, baseline[key], None, view.history_sizes)))
        if key in new_keys:
            add_bytes(key, len(encode_json(key).encode()) + 1 + field_bytes(key, state[key], None, new_archives))
            encode_field(key, state[key])
        else:
            deleted.update(field_paths(key, complete=key == "active_ticket_job" and active_replaced))
    byte_delta += max(len(state) - 1, 0) - max(len(baseline) - 1, 0)
    if _HISTORY in baseline and _HISTORY not in state:
        raise ValueError("SQLite work patches cannot delete Invocation history")
    if _HISTORY not in baseline and _HISTORY in state:
        add_bytes(_HISTORY, len(encode_json(_HISTORY).encode()) + 1 + 2)
    for position, entry in entries.items():
        if position in prior_entries and _same(prior_entries[position], entry):
            continue
        row = connection.execute("SELECT logical_bytes FROM run_history WHERE run_id = ? AND position = ?",
                                 (run_id, position)).fetchone()
        entry_bytes = len(encode_json(entry).encode())
        add_bytes(_HISTORY, entry_bytes - (row[0] if row is not None else 0))
        objects, content = repository._objects({_HISTORY: [entry]})
        materials.update(content)
        for path, item in objects.items():
            parsed = object_path(path)
            if len(parsed) > 1:
                location = encode_json((_HISTORY, position, *parsed[2:]))
                if view.objects.get(location) != item:
                    replacements[location] = item
        connection.execute("INSERT OR REPLACE INTO run_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _history_row(run_id, position, entry))
    add_bytes(_HISTORY, max(count - 1, 0) - max(view.count - 1, 0))
    logical_bytes = view.logical_bytes + byte_delta
    if not 3 <= logical_bytes <= repository.max_bytes:
        raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
    if _HISTORY in state and (_HISTORY not in baseline or count != view.count):
        replacements[_HROOT] = ("list", str(count))
    references = {path: (moved_references[path] if path in moved_references
                         else _references(repository, kind, encoded, materials))
                  for path, (kind, encoded) in replacements.items()}
    # Encoding a changed container may also visit unchanged snapshots. Only
    # the changed objects' complete dependency closure enters the mutation;
    # the current read already verified materials for untouched objects.
    needed_materials = set().union(*references.values()) if references else set()
    materials = {identity: value for identity, value in materials.items() if identity in needed_materials}
    previous = previous_objects(connection, run_id, view, replacements, deleted)
    identities = check_materials(connection, run_id, previous, references)
    repository._aggregate_rows = patch_work_objects(connection, run_id, replacements=replacements, deleted=deleted,
        materials=materials, references=references, native=repository._aggregate_rows)
    for path in deleted:
        connection.execute("DELETE FROM run_job_history WHERE run_id = ? AND object_path = ?", (run_id, path))
    for path, archive_size in new_archives.items():
        prefix = object_path(path)
        if any(object_path(changed)[:len(prefix)] == prefix for changed in replacements.keys() | deleted):
            connection.execute("INSERT OR REPLACE INTO run_job_history VALUES (?, ?, ?, ?)", (run_id, path, *archive_size))
    current_archives = sqlite_job_history.histories(state)
    for path in replacements:
        parsed = object_path(path)
        if len(parsed) > 2 and parsed[-2] == sqlite_job_history.FIELD and type(parsed[-1]) is int:
            archive_parent = encode_json(parsed[:-1])
            attempt = current_archives[archive_parent][parsed[-1]]
            connection.execute("INSERT OR REPLACE INTO run_job_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (run_id, *sqlite_job_history.attempt_row(archive_parent, parsed[-1], attempt)))
    connection.execute("UPDATE run_sizes SET logical_bytes = ?, canonical_count = ? WHERE run_id = ? "
                       "AND (logical_bytes IS NOT ? OR canonical_count IS NOT ?)",
                       (logical_bytes, canonical_count, run_id, logical_bytes, canonical_count))
    repository._aggregate_rows = sync_proofs(connection, run_id, view, state, baseline, replacements=replacements, deleted=deleted,
        previous=previous, references=references, identities=identities, field_deltas=field_deltas,
        prior_entries=prior_entries, entries=entries, count=count, canonical_count=canonical_count,
        timeline_updates=timeline_updates, max_bytes=repository.max_bytes, native=repository._aggregate_rows)
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    metadata = [value if isinstance(value, str) else None for value in
                (state.get("repository"), state.get("status"), state.get("phase"), state.get("created_at"), state.get("updated_at"))]
    values = (metadata[0], parent_number if type(parent_number) is int else None, *metadata[1:])
    # A no-op work observation must not manufacture a database revision and
    # force other scheduling readers to rebuild an unchanged projection.
    # Compare the actual SQL metadata, so drift is still corrected atomically.
    connection.execute("UPDATE runs SET repository=?, parent_number=?, status=?, phase=?, created_at=?, updated_at=? WHERE run_id=? "
                       "AND NOT (repository IS ? AND parent_number IS ? AND status IS ? AND phase IS ? AND created_at IS ? AND updated_at IS ?)",
                       (*values, run_id, *values))
    repository._local.view = None
    repository._local.work_view = None
