"""Keep object projections' proofs current after a sparse Ticket commit."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

from agent_run import sqlite_timeline
from agent_run.sqlite_encoding import encode_json, object_path
from agent_run.sqlite_integrity import (
    KEYED_FIELDS, SOURCE_DIGEST_FIELDS, field_content_digest, field_size_proof,
    history_item_digest, require_field_sizes, require_material_counts, root_fields,
    source_material_dependencies, source_object_digest, sync_material_counts,
)

if TYPE_CHECKING:
    from agent_run.sqlite_work_objects import WorkView

_MODULUS = 1 << 256
_HISTORY = "agent_invocation_history"
_CANONICAL = "ticket_agent_invocations"


def previous_objects(
    connection: sqlite3.Connection, run_id: str, view: WorkView,
    replacements: Mapping[str, tuple[str, str]], deleted: set[str],
) -> dict[str, tuple[str, str]]:
    """A removed hidden archive contributes headers, never decoded bodies."""
    changed = replacements.keys() | deleted
    previous = {path: view.objects[path] for path in changed if path in view.objects}
    missing = list(changed - previous.keys())
    for offset in range(0, len(missing), 256):
        batch = missing[offset:offset + 256]
        previous.update((path, (kind, encoded)) for path, kind, encoded in connection.execute(
            "SELECT object_path,kind,value_json FROM run_objects "
            f"WHERE run_id=? AND object_path IN ({','.join('?' for _ in batch)})", (run_id, *batch),
        ))
    return previous


def check_materials(
    connection: sqlite3.Connection, run_id: str,
    previous: Mapping[str, tuple[str, str]], references: Mapping[str, set[str]],
) -> set[str]:
    identities = source_material_dependencies(connection, run_id, previous)
    identities.update(identity for refs in references.values() for identity in refs)
    require_material_counts(connection, run_id, identities)
    return identities


def _text_digest(position: int, text: str) -> int:
    # TimelineScope already proved this canonical restored encoding. Avoid
    # decoding/encoding every retained event just to calculate a position hash.
    encoded = "[" + encode_json(position) + "," + text + "]"
    return int(hashlib.sha256(encoded.encode()).hexdigest(), 16)


def _digest(
    field: str, old_digest: str | None, baseline: Mapping[str, Any], state: Mapping[str, Any],
    previous: Mapping[str, tuple[str, str]], replacements: Mapping[str, tuple[str, str]],
    deleted: set[str], prior_entries: Mapping[int, Any], entries: Mapping[int, Any],
    view: WorkView, timeline_updates: Mapping[str, dict[str, Any]],
) -> str:
    total = int(old_digest, 16) if old_digest is not None else 0
    if field in SOURCE_DIGEST_FIELDS:
        for path in replacements.keys() | deleted:
            if object_path(path)[:1] != (field,):
                continue
            if path in previous:
                total -= source_object_digest(path, previous[path])
            if path in replacements:
                total += source_object_digest(path, replacements[path])
    elif field == _HISTORY:
        for position, entry in entries.items():
            if encode_json((field, position)) not in replacements:
                continue
            if position in prior_entries:
                total -= history_item_digest(position, prior_entries[position])
            total += history_item_digest(position, entry)
    elif field in timeline_updates:
        texts = view.timelines[field].entries
        shifted = field == "timeline_continuation" and len(texts) == sqlite_timeline.limit(field)
        if shifted:
            for position, text in enumerate(texts):
                total -= _text_digest(position, text)
            for position, text in enumerate(texts[1:]):
                total += _text_digest(position, text)
        total += history_item_digest(len(texts) - int(shifted), timeline_updates[field])
    elif field in KEYED_FIELDS:
        old, new = baseline.get(field, {}), state.get(field, {})
        if isinstance(old, dict) and isinstance(new, dict):
            keys = {parts[1] for path in replacements.keys() | deleted
                    if len(parts := object_path(path)) > 1 and parts[0] == field
                    and isinstance(parts[1], str)}
            for key in keys:
                if key in old:
                    total -= history_item_digest(key, old[key])
                if key in new:
                    total += history_item_digest(key, new[key])
        else:
            return field_content_digest(field, state[field])
    else:
        old, new = baseline.get(field), state[field]
        if isinstance(old, list) and isinstance(new, list):
            positions = {parts[1] for path in replacements.keys() | deleted
                         if len(parts := object_path(path)) > 1 and parts[0] == field
                         and type(parts[1]) is int}
            for position in positions:
                if position < len(old):
                    total -= history_item_digest(position, old[position])
                if position < len(new):
                    total += history_item_digest(position, new[position])
        else:
            return field_content_digest(field, new)
    return f"{total % _MODULUS:064x}"


def sync_proofs(
    connection: sqlite3.Connection, run_id: str, view: WorkView,
    state: Mapping[str, Any], baseline: Mapping[str, Any], *,
    replacements: Mapping[str, tuple[str, str]], deleted: set[str],
    previous: Mapping[str, tuple[str, str]], references: Mapping[str, set[str]],
    identities: set[str], field_deltas: Mapping[str, int],
    prior_entries: Mapping[int, Any], entries: Mapping[int, Any],
    count: int, canonical_count: int, timeline_updates: Mapping[str, dict[str, Any]],
    max_bytes: int, native: bool,
) -> bool:
    from agent_run.sqlite_objects import invocation_identity, invocation_attempt_identity
    from agent_run.sqlite_mutations import _batch

    changed = replacements.keys() | deleted
    native = _batch(connection, ((run_id, path) for path in changed),
        sql="DELETE FROM run_object_materials WHERE run_id=? AND object_path=?",
        native_sql="DELETE FROM run_object_materials WHERE (run_id,object_path) IN "
                   "(SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?))",
        native=native)
    connection.executemany("DELETE FROM run_object_index WHERE run_id=? AND object_path=?",
                           ((run_id, path) for path in deleted))
    native = _batch(connection, ((run_id, path, identity) for path, refs in references.items() for identity in refs),
        sql="INSERT INTO run_object_materials VALUES (?,?,?)",
        native_sql="INSERT INTO run_object_materials SELECT json_extract(value,'$[0]'),"
                   "json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(?)", native=native)
    sync_material_counts(connection, run_id, identities)

    locator_rows: list[tuple[Any, ...]] = []
    unlinked = []
    for position, entry in entries.items():
        path = encode_json((_HISTORY, position))
        if path not in replacements:
            continue
        if not isinstance(entry, dict):
            unlinked.append((run_id, path))
            continue
        locator_rows.append((run_id, path, invocation_identity(entry), entry.get("resume_id"), invocation_attempt_identity(entry)))
    connection.executemany("DELETE FROM run_object_index WHERE run_id=? AND object_path=?", unlinked)
    connection.executemany(
        "INSERT INTO run_object_index(run_id,object_path,invocation_id,resume_id,logical_bytes,attempt_id) "
        "VALUES (?,?,?,?,0,?) ON CONFLICT(run_id,object_path) DO UPDATE SET "
        "invocation_id=excluded.invocation_id,resume_id=excluded.resume_id,attempt_id=excluded.attempt_id", locator_rows,
    )

    fields = root_fields(connection, run_id)
    removed, added = baseline.keys() - state.keys(), state.keys() - baseline.keys()
    if removed or added:
        fields.difference_update(removed)
        fields.update(added)
        connection.execute("UPDATE runs SET root_keys_json=? WHERE run_id=?", (encode_json(sorted(fields)), run_id))
    changed_fields = {str(object_path(path)[0]) for path in changed if object_path(path)}
    changed_fields.update(field for field, delta in field_deltas.items() if delta)
    paths = [encode_json((field,)) for field in changed_fields & fields]
    records: dict[str, tuple[Any, ...]] = {}
    for offset in range(0, len(paths), 256):
        batch = paths[offset:offset + 256]
        records.update((row[0], row[1:]) for row in connection.execute(
            "SELECT o.object_path,i.logical_bytes,i.content_digest,i.child_count,o.kind,o.value_json "
            "FROM run_objects o LEFT JOIN run_object_index i USING(run_id,object_path) "
            f"WHERE o.run_id=? AND o.object_path IN ({','.join('?' for _ in batch)})", (run_id, *batch),
        ))
    summary_rows: list[tuple[Any, ...]] = []
    for field in changed_fields & fields:
        path = encode_json((field,))
        row = records.get(path)
        if row is None:
            raise ValueError("Missing SQLite Run field object after work commit")
        prior_size, prior_digest, _, kind, encoded = row
        # Added-field deltas include its colon; a field proof also budgets the
        # separator. Existing fields' name overhead cancels in their delta.
        size = (prior_size if prior_size is not None else 1) + field_deltas.get(field, 0)
        digest = _digest(field, prior_digest, baseline, state,
                         previous, replacements, deleted, prior_entries, entries, view, timeline_updates)
        source = kind, encoded
        child_count: int | None
        if field == _HISTORY:
            child_count = count
        elif field == _CANONICAL:
            child_count = canonical_count
        elif field in view.timelines:
            child_count = int(source[1])
        else:
            value = state.get(field)
            child_count = len(value) if isinstance(value, dict | list) else None
            if field == "active_ticket_job" and isinstance(value, dict):
                child_count = len(value) + sum(
                    1 for encoded in view.hidden_history
                    if (parts := object_path(encoded))[:1] == (field,)
                    and parts[-1] not in value and encoded not in deleted
                )
        summary_rows.append((run_id, path, size, digest, field_size_proof(path, size, digest, source, child_count), child_count))
    connection.executemany(
        "INSERT INTO run_object_index VALUES (?,?,NULL,NULL,?,?,?,?,NULL) "
        "ON CONFLICT(run_id,object_path) DO UPDATE SET logical_bytes=excluded.logical_bytes,"
        "content_digest=excluded.content_digest,size_proof=excluded.size_proof,child_count=excluded.child_count", summary_rows,
    )
    require_field_sizes(connection, run_id, max_bytes=max_bytes)
    return native
