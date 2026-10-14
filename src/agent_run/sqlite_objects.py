"""Indexed current-object projections; all payloads remain in run_objects."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from itertools import islice
from typing import Any, TYPE_CHECKING, cast

from agent_run.sqlite_encoding import encode_json, object_path, scalar_value
from agent_run.sqlite_mutations import _batch
from agent_run.sqlite_integrity import (
    KEYED_FIELDS, SOURCE_DIGEST_FIELDS, source_objects_digest, field_content_digest, field_size_proof, history_item_digest,
    require_field_sizes, require_material_counts, require_material_payloads, root_fields,
    source_material_dependencies, sync_material_counts,
)

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository

_HISTORY = "agent_invocation_history"


def invocation_identity(invocation: Mapping[str, Any]) -> str:
    return encode_json((str(invocation.get("work_subject")), str(invocation.get("started_at"))))



def invocation_attempt_identity(invocation: Mapping[str, Any]) -> str | None:
    attempt = invocation.get("semantic_attempt")
    value = attempt.get("attempt_id") if isinstance(attempt, Mapping) else None
    return value if isinstance(value, str) else None


def _require_root(connection: sqlite3.Connection, run_id: str) -> None:
    root = connection.execute("SELECT kind,value_json FROM run_objects WHERE run_id=? AND object_path='[]'", (run_id,)).fetchone()
    if root != ("map", "null"):
        raise ValueError("Missing or invalid SQLite Run object root")


def _rows(connection: sqlite3.Connection, run_id: str, path: str) -> dict[str, tuple[str, str]]:
    prefix = path[:-1] + ","
    return {key: (kind, value) for key, kind, value in connection.execute(
        "SELECT object_path, kind, value_json FROM run_objects WHERE run_id=? "
        "AND (object_path=? OR (object_path>=? AND object_path<?))",
        (run_id, path, prefix, prefix[:-1] + "-"),
    )}


def _rows_for_paths(
    connection: sqlite3.Connection, run_id: str, paths: list[str],
) -> dict[str, tuple[str, str]]:
    """Read exact subtrees in bounded batches from the same SQL snapshot."""
    result: dict[str, tuple[str, str]] = {}
    for offset in range(0, len(paths), 80):
        batch = paths[offset:offset + 80]
        arguments: list[Any] = []
        for path in batch:
            prefix = path[:-1] + ","
            arguments.extend((path, prefix, prefix[:-1] + "-"))
        # Keep requested paths outside the indexed lookup. A multi-subtree OR
        # otherwise scans the Run, and one UNION per path repeats SQL parsing.
        values = ",".join("(?,?,?)" for _ in batch)
        result.update((path, (kind, value)) for path, kind, value in connection.execute(
            f"WITH requested(path,start,stop) AS (VALUES {values}) "
            "SELECT o.object_path,o.kind,o.value_json FROM requested p CROSS JOIN run_objects o "
            "WHERE o.run_id=? AND o.object_path=p.path UNION ALL "
            "SELECT o.object_path,o.kind,o.value_json FROM requested p CROSS JOIN run_objects o "
            "WHERE o.run_id=? AND o.object_path>=p.start AND o.object_path<p.stop", [*arguments, run_id, run_id],
        ))
    return result


def _dependencies(identity: str, materials: Mapping[str, str], *, snapshot: bool) -> set[str]:
    result = {identity}
    if not snapshot:
        return result
    def visit(value: Any) -> None:
        if not isinstance(value, list) or len(value) != 2:
            return
        kind, content = value
        if kind == "map":
            for child in content.values():
                visit(child)
        elif kind == "list":
            for child in content:
                visit(child)
        elif kind in {"snapshot", "material"}:
            if content not in result:
                result.add(content)
                if kind == "snapshot":
                    visit(json.loads(materials[content]))
    try:
        visit(json.loads(materials[identity]))
    except json.JSONDecodeError:
        pass  # Literal large text is a material, rather than a snapshot.
    return result


def index_objects(
    connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any],
    objects: Mapping[str, tuple[str, str]], materials: Mapping[str, str],
    previous: Mapping[str, tuple[str, str]],
) -> None:
    changed = {path for path in previous.keys() | objects.keys() if previous.get(path) != objects.get(path)}
    changed_fields = {object_path(path)[0] for path in changed if object_path(path)}
    connection.executemany("DELETE FROM run_object_materials WHERE run_id=? AND object_path=?",
                           ((run_id, path) for path in changed))
    connection.executemany("DELETE FROM run_object_index WHERE run_id=? AND object_path=?",
                           ((run_id, path) for path in previous.keys() - objects.keys()))
    for path in changed & objects.keys():
        kind, encoded = objects[path]
        if kind in {"material", "snapshot"}:
            refs = _dependencies(json.loads(encoded), materials, snapshot=kind == "snapshot")
            connection.executemany("INSERT INTO run_object_materials VALUES (?,?,?)",
                                   ((run_id, path, identity) for identity in refs))
    entries: list[tuple[str, str, str | None, Any, int, str | None, str | None, int | None, str | None]] = []
    history_claims = dict((row[0], row[1:]) for row in connection.execute(
        "SELECT object_path,invocation_id,resume_id,logical_bytes,content_digest,size_proof,child_count,attempt_id FROM run_object_index "
        "WHERE run_id=? AND invocation_id IS NOT NULL", (run_id,),
    )) if _HISTORY in state else {}
    for field, value in state.items():
        if field == _HISTORY:
            for position, item in enumerate(value):
                if isinstance(item, dict):
                    path = encode_json((field, position))
                    claim = invocation_identity(item), item.get("resume_id"), 0, None, None, None, invocation_attempt_identity(item)
                    if history_claims.get(path) != claim:
                        entries.append((run_id, path, *claim))
        if field not in changed_fields:
            continue
        path = encode_json((field,))
        encoded_value = encode_json(value).encode()
        size = len(encode_json(field).encode()) + len(encoded_value) + 2
        digest = field_content_digest(field, value) if isinstance(value, list) or field in KEYED_FIELDS else hashlib.sha256(encoded_value).hexdigest()
        if field in SOURCE_DIGEST_FIELDS:
            digest = source_objects_digest({location: source for location, source in objects.items()
                                            if object_path(location)[:1] == (field,)})
        child_count = len(value) if isinstance(value, (dict, list)) else None
        entries.append((run_id, path, None, None, size, digest,
                        field_size_proof(path, size, digest, objects[path], child_count), child_count, None))
    connection.executemany(
        "INSERT INTO run_object_index VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,object_path) "
        "DO UPDATE SET invocation_id=excluded.invocation_id,resume_id=excluded.resume_id,"
        "logical_bytes=excluded.logical_bytes,content_digest=excluded.content_digest,size_proof=excluded.size_proof,child_count=excluded.child_count,attempt_id=excluded.attempt_id", entries,
    )


def _prefetch_root_materials(
    connection: sqlite3.Connection, rows: Mapping[str, tuple[str, str]],
    run_id: str, materials: dict[str, str],
) -> None:
    """Verify bounded direct dependencies in this read's actual SQL snapshot."""
    identities = {scalar_value(encoded) for kind, encoded in rows.values()
                  if kind in {"material", "snapshot"}}
    pending = iter(identity for identity in identities
                   if isinstance(identity, str) and identity not in materials)
    while batch := tuple(islice(pending, 256)):
        source = (" FROM run_materials m JOIN run_material_refs r USING(material_id) "
                  "WHERE r.run_id=? AND m.material_id IN (" + ",".join("?" for _ in batch) + ")")
        parameters = (run_id, *batch)
        physical = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(m.value AS BLOB))),0)" + source, parameters,
        ).fetchone()[0]
        # Never build an aggregate body copy for an oversized batch. The
        # original lazy reader also handles missing Run links and nested tags.
        if physical > 512 * 1024:
            continue
        try:
            encoded = connection.execute(
                "SELECT json_group_array(json_array(m.material_id,"
                "CASE WHEN typeof(m.value)='text' THEN m.value END,"
                "CASE WHEN typeof(m.byte_length)='integer' THEN m.byte_length END,"
                "LENGTH(CAST(m.value AS BLOB)),typeof(m.value),typeof(m.byte_length)))" + source,
                parameters,
            ).fetchone()[0]
        except sqlite3.OperationalError as error:
            if "no such function" not in str(error):
                raise
            return
        for identity, text, declared, actual, text_type, length_type in json.loads(encoded):
            if (not isinstance(identity, str) or text_type != "text" or length_type != "integer"
                    or not isinstance(text, str) or type(declared) is not int or declared != actual):
                raise ValueError("Invalid SQLite Run material identity")
            raw = text.encode("utf-8")
            if len(raw) != declared or hashlib.sha256(raw).hexdigest() != identity:
                raise ValueError("Invalid SQLite Run material identity")
            materials[identity] = text


def _decode(
    connection: sqlite3.Connection, rows: Mapping[str, tuple[str, str]], *, run_id: str,
    material_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    materials: dict[str, str] = {} if material_values is None else material_values
    _prefetch_root_materials(connection, rows, run_id, materials)
    def material(identity: str) -> str:
        if identity not in materials:
            row = connection.execute("SELECT m.value,m.byte_length FROM run_materials m JOIN run_material_refs r "
                                     "ON m.material_id=r.material_id WHERE r.run_id=? AND m.material_id=?", (run_id, identity)).fetchone()
            if row is None:
                raise ValueError("Missing SQLite Run material")
            text, length = row
            encoded = text.encode()
            if len(encoded) != length or hashlib.sha256(encoded).hexdigest() != identity:
                raise ValueError("Invalid SQLite Run material identity")
            materials[identity] = text
        return materials[identity]
    resolving: set[str] = set()
    def snapshot(identity: str) -> Any:
        if identity in resolving:
            raise ValueError("Cyclic SQLite Run snapshot")
        resolving.add(identity)
        try:
            return restore(json.loads(material(identity)))
        finally:
            resolving.remove(identity)
    def restore(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        if len(value) != 2:
            raise ValueError("Invalid SQLite Run snapshot")
        kind, content = value
        if kind == "map" and isinstance(content, dict):
            return {key: restore(child) for key, child in content.items()}
        if kind == "list" and isinstance(content, list):
            return [restore(child) for child in content]
        if kind == "material" and isinstance(content, str):
            return material(content)
        if kind == "snapshot" and isinstance(content, str):
            return snapshot(content)
        raise ValueError("Invalid SQLite Run snapshot")
    nodes: dict[tuple[str | int, ...], Any] = {(): {}}
    lengths = {}
    for encoded_path, (kind, encoded) in rows.items():
        path = object_path(encoded_path)
        value = scalar_value(encoded)
        if kind == "map" and value is None:
            value = {}
        elif kind == "list" and type(value) is int and value >= 0:
            lengths[path] = value
            value = []
        elif kind == "material":
            value = material(value)
        elif kind == "snapshot":
            value = snapshot(value)
        elif kind != "scalar" or isinstance(value, dict | list):
            raise ValueError("Invalid SQLite Run object value")
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
    return cast(dict[str, Any], nodes[()])


def _require_history_claim(
    connection: sqlite3.Connection, run_id: str, path: str, item: Mapping[str, Any],
    *, identity: str | None = None, indexed: tuple[Any, ...] | None = None,
) -> None:
    if not isinstance(item, Mapping):
        raise ValueError("Invalid SQLite Invocation history index claim")
    parts = object_path(path)
    if indexed is None:
        indexed = connection.execute(
            "SELECT invocation_id,resume_id,attempt_id FROM run_object_index WHERE run_id=? AND object_path=?",
            (run_id, path),
        ).fetchone()
    actual = invocation_identity(item)
    if (len(parts) != 2 or parts[0] != _HISTORY or type(parts[1]) is not int
            or parts[1] < 0 or indexed != (actual, item.get("resume_id"), invocation_attempt_identity(item))
            or identity is not None and actual != identity):
        raise ValueError("Invalid SQLite Invocation history index claim")


def load_projection(connection: sqlite3.Connection, run_id: str, *, excluded_fields: set[str] | None = None, max_bytes: int,
                    related_tickets: bool = False, ticket_numbers: set[int] | None = None,
                    scheduler_tickets: bool = False) -> tuple[dict[str, Any], dict[str, int]] | None:
    if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        return None
    _require_root(connection, run_id)
    require_field_sizes(connection, run_id, max_bytes=max_bytes)
    excluded_fields = set(excluded_fields or set())
    if related_tickets or ticket_numbers is not None or scheduler_tickets:
        excluded_fields.update(KEYED_FIELDS)
    paths = []
    for (path,) in connection.execute("SELECT object_path FROM run_object_index WHERE run_id=? AND logical_bytes>0", (run_id,)):
        if object_path(path) != (_HISTORY,) and object_path(path)[0] not in (excluded_fields or set()):
            paths.append(path)
    rows = _rows_for_paths(connection, run_id, paths)
    if any(path not in rows for path in paths):
        raise ValueError("Missing indexed SQLite Run object")
    # Current owners and linked history often share the same fixed evidence.
    # Verify its actual bytes once in this consistent read, never across reads.
    material_values: dict[str, str] = {}
    state = _decode(connection, rows, run_id=run_id, material_values=material_values)
    if related_tickets or ticket_numbers is not None or scheduler_tickets:
        from agent_run.sqlite_collections import load_paths
        numbers = set(ticket_numbers or set())
        active = state.get("active_ticket_job")
        if isinstance(active, dict) and type(active.get("ticket_number")) is int:
            numbers.add(active["ticket_number"])
        holder = state.get("ticket_integration_holder")
        if type(holder) is int:
            numbers.add(holder)
        for item in [holder, *state.get("ticket_integration_queue", []), *state.get("ticket_work_queue", [])]:
            if isinstance(item, dict) and type(item.get("ticket_number")) is int:
                numbers.add(item["ticket_number"])
        change = state.get("unsupported_scope_change")
        summary = change.get("graph_change_summary") if isinstance(change, dict) else None
        reopened = summary.get("reopened_completed_tickets") if isinstance(summary, dict) else None
        if isinstance(reopened, list):
            # This gate depends on completed owners outside the active/queue
            # selection. Read only its exact references in the same snapshot.
            numbers.update(number for number in reopened if type(number) is int)
        whole_fields = {"ticket_jobs", "ticket_agent_invocations", "retired_ticket_generations"} if scheduler_tickets else set()
        child_paths: set[tuple[str | int, ...]] = {(field, str(number)) for field in KEYED_FIELDS - whole_fields for number in numbers}
        subsets = load_paths(connection, run_id, child_paths, material_values=material_values)
        for field in KEYED_FIELDS:
            state[field] = subsets.get(field, {})
        if scheduler_tickets:
            whole = _decode(connection, _rows_for_paths(connection, run_id, [encode_json((field,)) for field in whole_fields]),
                            run_id=run_id, material_values=material_values)
            for field in whole_fields:
                state[field] = whole.get(field, {})
    ids = {invocation_identity(invocation) for invocation in
           [state.get("active_agent_invocation"), *state.get("ticket_agent_invocations", {}).values()]
           if isinstance(invocation, dict)} - {None}
    resumes = {record.get("resume_id") for record in state.get("resume_audit", {}).get("history", [])
               if isinstance(record, dict)} - {None}
    selected: set[str] = set()
    for column, values in (("invocation_id", ids), ("resume_id", resumes)):
        pending = iter(values)
        while batch := tuple(islice(pending, 256)):
            selected.update(path for (path,) in connection.execute(
                f"SELECT object_path FROM run_object_index WHERE run_id=? "
                f"AND {column} IN ({','.join('?' for _ in batch)})", (run_id, *batch)))
    history, positions = [], {}
    ordered = sorted(selected, key=lambda value: object_path(value)[1])
    claimed: dict[str, tuple[Any, ...]] = {}
    for offset in range(0, len(ordered), 256):
        paths = ordered[offset:offset + 256]
        claimed.update((row[0], row[1:]) for row in connection.execute(
            "SELECT o.object_path,o.kind,o.value_json,i.invocation_id,i.resume_id,i.attempt_id "
            "FROM run_objects o LEFT JOIN run_object_index i USING(run_id,object_path) "
            f"WHERE o.run_id=? AND o.object_path IN ({','.join('?' for _ in paths)})", (run_id, *paths),
        ))
    if selected - claimed.keys():
        raise ValueError("Missing indexed SQLite Invocation")
    # Every selected record retains its actual claim and independent returned
    # containers. Batch their exact roots so shared evidence and material
    # prefetch do not create one SQL round trip per history item.
    items = _decode(connection, {encode_json((f"item-{index}",)): (claimed[path][0], claimed[path][1])
                                for index, path in enumerate(ordered)},
                    run_id=run_id, material_values=material_values) if ordered else {}
    for index, path in enumerate(ordered):
        row = claimed.get(path)
        if row is None:
            raise ValueError("Missing indexed SQLite Invocation")
        item = items[f"item-{index}"]
        _require_history_claim(connection, run_id, path, item, indexed=row[2:])
        history.append(item)
        positions[invocation_identity(item)] = int(object_path(path)[1])
    state[_HISTORY] = history
    return state, positions


def save_projection(
    repository: SQLiteStateRepository, connection: sqlite3.Connection,
    run_id: str, state: Mapping[str, Any], fields: set[str], positions: Mapping[str, int],
    *, keyed_changes: Mapping[str, set[str]] | None = None, append_changes: Mapping[str, list[Any]] | None = None,
) -> None:
    if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        raise ValueError("Delivery Run disappeared during object commit")
    _require_root(connection, run_id)
    require_field_sizes(connection, run_id, max_bytes=repository.max_bytes)
    old_fields = root_fields(connection, run_id)
    from agent_run.sqlite_history_retention import retain_details
    state, fields = retain_details(connection, run_id, state, fields, keyed_changes or {})
    keyed_changes = keyed_changes or {}
    append_changes = append_changes or {}
    partial_fields = keyed_changes.keys() | append_changes.keys()
    selected = {key: state[key] for key in fields - {_HISTORY} - partial_fields if key in state}
    objects, materials = repository._objects(selected)
    objects.pop("[]", None)
    previous = {}
    for field in fields - {_HISTORY} - partial_fields:
        previous.update(_rows(connection, run_id, encode_json((field,))))
    history_bytes_delta = 0
    history_digest_delta = 0
    if _HISTORY in fields:
        row = connection.execute("SELECT value_json FROM run_objects WHERE run_id=? AND object_path=?",
                                 (run_id, encode_json((_HISTORY,)))).fetchone()
        length = int(row[0]) if row else 0
        summary = connection.execute("SELECT logical_bytes,content_digest FROM run_object_index WHERE run_id=? AND object_path=?",
                                     (run_id, encode_json((_HISTORY,)))).fetchone()
        prior_history_size = summary[0] if summary else len(encode_json(_HISTORY).encode()) + 4
        prior_history_digest = int(summary[1], 16) if summary else 0
        for item in state.get(_HISTORY, []):
            identity = invocation_identity(item)
            position = positions.get(identity)
            existing = connection.execute("SELECT object_path FROM run_object_index WHERE run_id=? AND invocation_id=?", (run_id, identity)).fetchone()
            if existing is not None:
                position = int(object_path(existing[0])[1])
            elif position is None:
                position = length
                length += 1
            path = encode_json((_HISTORY, position))
            item_objects, item_materials = repository._objects({_HISTORY: [item]})
            old = connection.execute("SELECT kind,value_json FROM run_objects WHERE run_id=? AND object_path=?", (run_id, path)).fetchone()
            if old is not None:
                previous[path] = old
                old_item = _decode(connection, {encode_json(("item",)): old}, run_id=run_id)["item"]
                _require_history_claim(connection, run_id, path, old_item, identity=identity)
                history_bytes_delta -= len(encode_json(old_item).encode())
                history_digest_delta -= history_item_digest(position, old_item)
            else:
                history_bytes_delta += bool(position)
            history_bytes_delta += len(encode_json(item).encode())
            history_digest_delta += history_item_digest(position, item)
            objects[path] = item_objects[encode_json((_HISTORY, 0))]
            materials.update(item_materials)
            connection.execute("INSERT INTO run_object_index(run_id,object_path,invocation_id,resume_id,logical_bytes,attempt_id) VALUES (?,?,?,?,0,?) ON CONFLICT(run_id,object_path) "
                               "DO UPDATE SET invocation_id=excluded.invocation_id,resume_id=excluded.resume_id,attempt_id=excluded.attempt_id",
                               (run_id, path, identity, item.get("resume_id"), invocation_attempt_identity(item)))
        objects[encode_json((_HISTORY,))] = ("list", str(length))
    from agent_run.sqlite_collections import prepare_collections, save_summaries
    summaries = prepare_collections(repository, connection, run_id, state, keyed_changes, append_changes,
                                    objects, materials, previous)
    candidates = source_material_dependencies(connection, run_id, previous)
    require_material_counts(connection, run_id, candidates | materials.keys())
    require_material_payloads(connection, materials)
    index_objects(connection, run_id, selected, objects, materials, previous)
    if _HISTORY in fields:
        path = encode_json((_HISTORY,))
        size = prior_history_size + history_bytes_delta
        digest = f"{(prior_history_digest + history_digest_delta) % (1 << 256):064x}"
        connection.execute("INSERT INTO run_object_index VALUES (?,?,NULL,NULL,?,?,?,?,NULL) "
                           "ON CONFLICT(run_id,object_path) DO UPDATE SET logical_bytes=excluded.logical_bytes,"
                           "content_digest=excluded.content_digest,size_proof=excluded.size_proof,child_count=excluded.child_count",
                           (run_id, path, size, digest, field_size_proof(path, size, digest, objects[path], length), length))
    save_summaries(connection, run_id, summaries, objects)
    new_fields = (old_fields - fields) | selected.keys() | partial_fields | ({_HISTORY} if _HISTORY in fields else set())
    connection.execute("UPDATE runs SET root_keys_json=? WHERE run_id=?", (encode_json(sorted(new_fields)), run_id))
    affected = {identity for identity in candidates if connection.execute(
        "SELECT 1 FROM run_object_materials WHERE run_id=? AND material_id=? LIMIT 1",
        (run_id, identity),
    ).fetchone() is None}
    from agent_run.sqlite_projection_indexes import release_references, sync_indexes
    changed_paths = {path for path in previous.keys() | objects.keys() if previous.get(path) != objects.get(path)}
    release_references(connection, run_id, changed_paths)
    # New references must exist before old material collection. Payload changes,
    # identity indexes and size accounting share the caller's short transaction.
    _batch(connection, ((run_id, path) for path in previous.keys() - objects.keys()),
           sql="DELETE FROM run_objects WHERE run_id=? AND object_path=?", native_sql="", native=False)
    connection.executemany("INSERT INTO run_objects VALUES (?,?,?,?) ON CONFLICT(run_id,object_path) DO UPDATE SET kind=excluded.kind,value_json=excluded.value_json",
                           ((run_id, path, *value) for path, value in objects.items() if previous.get(path) != value))
    connection.executemany("INSERT OR IGNORE INTO run_materials VALUES (?,?,?)",
                           ((identity, value, len(value.encode())) for identity, value in materials.items()))
    connection.executemany("INSERT OR IGNORE INTO run_material_refs(run_id,material_id) VALUES (?,?)", ((run_id, identity) for identity in materials))
    sync_indexes(repository, connection, run_id, objects, previous)
    connection.executemany("DELETE FROM run_material_refs WHERE run_id=? AND material_id=?", ((run_id, identity) for identity in affected))
    connection.executemany("DELETE FROM run_materials WHERE material_id=? AND NOT EXISTS (SELECT 1 FROM run_material_refs WHERE material_id=?)",
                           ((identity, identity) for identity in affected))
    sync_material_counts(connection, run_id, candidates | materials.keys())
    require_field_sizes(connection, run_id, max_bytes=repository.max_bytes)
    columns = {"repository", "status", "phase", "created_at", "updated_at"} & fields
    for column in columns:
        connection.execute(f"UPDATE runs SET {column}=? WHERE run_id=?", (state.get(column), run_id))
    if "parent" in fields:
        connection.execute("UPDATE runs SET parent_number=? WHERE run_id=?", (state.get("parent", {}).get("number"), run_id))


def load_fields(connection: sqlite3.Connection, run_id: str, fields: set[str], *, max_bytes: int) -> dict[str, Any] | None:
    """Read an explicit owner set without assembling other current objects."""
    if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        return None
    _require_root(connection, run_id)
    require_field_sizes(connection, run_id, max_bytes=max_bytes)
    rows = _rows_for_paths(connection, run_id, [encode_json((field,)) for field in fields])
    return _decode(connection, rows, run_id=run_id)
