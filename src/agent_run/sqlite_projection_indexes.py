"""Keep Ticket work indexes current after an owned-object commit."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agent_run.sqlite_encoding import encode_json, object_path
from agent_run.sqlite_integrity import require_field_sizes
from agent_run.sqlite_objects import _decode, _rows
from agent_run.sqlite_work_objects import _history_row
from agent_run import sqlite_job_history

if TYPE_CHECKING:
    from agent_run.sqlite_state import SQLiteStateRepository


def release_references(connection: sqlite3.Connection, run_id: str, paths: set[str]) -> None:
    # Updates do not trigger the object foreign key's deletion cascade. Remove
    # obsolete references before their released material can be collected.
    connection.executemany("DELETE FROM run_object_material_refs WHERE run_id=? AND object_path=?",
                           ((run_id, path) for path in paths))


def sync_indexes(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                 objects: Mapping[str, tuple[str, str]], previous: Mapping[str, tuple[str, str]]) -> None:
    changed = {path for path in objects.keys() | previous.keys() if objects.get(path) != previous.get(path)}
    archives: set[tuple[str | int, ...]] = set()
    for path in changed:
        connection.execute("INSERT INTO run_object_material_refs SELECT run_id,object_path,material_id "
                           "FROM run_object_materials WHERE run_id=? AND object_path=?", (run_id, path))
        parts = object_path(path)
        if len(parts) == 2 and parts[0] == "agent_invocation_history" and type(parts[1]) is int:
            if path not in objects:
                connection.execute("DELETE FROM run_history WHERE run_id=? AND position=?", (run_id, parts[1]))
            else:
                entry = _decode(connection, {encode_json(("item",)): objects[path]}, run_id=run_id)["item"]
                connection.execute("INSERT OR REPLACE INTO run_history VALUES (?,?,?,?,?,?,?,?)",
                                   _history_row(run_id, parts[1], entry))
        is_archive = (len(parts) == 2 and parts[0] == "active_ticket_job" and parts[-1] in sqlite_job_history.FIELDS
                      or len(parts) == 3 and parts[0] == "ticket_jobs" and parts[-1] in sqlite_job_history.FIELDS)
        # A changed Attempt requires refreshing its archive's indexed metadata,
        # even when the list header retained the same length.
        if (len(parts) == 3 and parts[0] == "active_ticket_job" and parts[-2] == sqlite_job_history.FIELD
                or len(parts) == 4 and parts[0] == "ticket_jobs" and parts[-2] == sqlite_job_history.FIELD):
            parts = parts[:-1]
            is_archive = True
        if is_archive:
            archives.add(parts)
    for parts in archives:
        archive_path = encode_json(parts)
        rows = _rows(connection, run_id, archive_path)
        if archive_path not in rows:
            connection.execute("DELETE FROM run_job_history WHERE run_id=? AND object_path=?", (run_id, archive_path))
            continue
        relative = {encode_json(("item", *object_path(location)[len(parts):])): value for location, value in rows.items()}
        value = _decode(connection, relative, run_id=run_id)["item"]
        connection.execute("INSERT OR REPLACE INTO run_job_history VALUES (?,?,?,?)",
                           (run_id, archive_path, len(encode_json(value).encode()), len(value) if isinstance(value, list) else -1))
        if parts[-1] == sqlite_job_history.FIELD:
            connection.execute("DELETE FROM run_job_attempts WHERE run_id=? AND parent_path=?", (run_id, archive_path))
            connection.executemany("INSERT INTO run_job_attempts VALUES (?,?,?,?,?,?,?)",
                                   ((run_id, *sqlite_job_history.attempt_row(archive_path, position, attempt))
                                    for position, attempt in enumerate(value)))
    count = connection.execute("SELECT child_count FROM run_object_index WHERE run_id=? AND object_path=?",
                               (run_id, encode_json(("ticket_agent_invocations",)))).fetchone()
    connection.execute("INSERT OR REPLACE INTO run_sizes VALUES (?,?,?)",
                       (run_id, require_field_sizes(connection, run_id, max_bytes=repository.max_bytes), count[0] if count and type(count[0]) is int else 0))
