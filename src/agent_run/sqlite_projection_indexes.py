"""Keep Ticket work indexes current after an owned-object commit."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agent_run.sqlite_integrity import require_field_sizes

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
    for path in changed:
        connection.execute("INSERT INTO run_object_material_refs SELECT run_id,object_path,material_id "
                           "FROM run_object_materials WHERE run_id=? AND object_path=?", (run_id, path))
    connection.execute("INSERT OR REPLACE INTO run_sizes VALUES (?,?,0)",
                       (run_id, require_field_sizes(connection, run_id, max_bytes=repository.max_bytes)))
