"""Read exact completed Publication records without expanding owner archives."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import encode_json, object_path, scalar_value
from agent_run.sqlite_job_history import FIELD, attempt_row

if TYPE_CHECKING:
    from agent_run.sqlite_objects import DecodeContext
    from agent_run.sqlite_state import SQLiteStateRepository


def archive_rows(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                 paths: list[str]) -> list[tuple[str, str, str]]:
    from agent_run.sqlite_objects import _sql_rows

    rows = []
    for path in paths:
        prefix = path[:-1]
        predicate = "run_id=? AND (object_path=? OR (object_path>=? AND object_path<?))"
        parameters = (run_id, path, prefix + ",", prefix + "-")
        size = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+LENGTH(CAST(value_json AS BLOB))),0) FROM run_objects WHERE " + predicate, parameters).fetchone()[0]
        if size > 8 * repository.max_bytes:
            raise ValueError(f"Run state exceeds {repository.max_bytes} byte persistence limit")
        rows.extend(_sql_rows(repository, connection,
            "SELECT object_path,kind,value_json FROM run_objects WHERE " + predicate, parameters,
            ("object_path", "kind", "value_json"), byte_count=size))
    return rows


def read_related(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                 state: Mapping[str, Any], archives: Mapping[str, tuple[int, int]], context: DecodeContext,
                 ) -> dict[str, dict[str, Any]]:
    from agent_run.sqlite_objects import _object_rows, _prefetch_materials

    owners = [(('active_ticket_job',), state.get("active_ticket_job"))] + [
        (("ticket_jobs", key), value) for key, value in state.get("ticket_jobs", {}).items()]
    metadata = []
    for owner_path, owner in owners:
        parent = encode_json((*owner_path, FIELD))
        if (parent not in archives or not isinstance(owner, dict)
                or FIELD in owner or owner.get("publication_operation_retry") is None):
            continue
        pending = owner.get("pending_semantic_attempt")
        if isinstance(pending, dict) and pending.get("role") == "publication":
            continue
        record = connection.execute(
            "SELECT object_path,parent_path,position,role,status,ordinal FROM run_job_attempts "
            "WHERE run_id=? AND parent_path=? AND role='publication' AND status='completed' AND ordinal=? "
            "ORDER BY position DESC LIMIT 1", (run_id, parent, owner.get("publication_attempts")),
        ).fetchone()
        if record is not None:
            metadata.append(record)
    rows = _object_rows(repository, connection, run_id, [row[0] for row in metadata])
    _prefetch_materials(repository, connection, run_id, rows, context)
    return decode_related(repository, connection, run_id, rows, metadata, archives, context)


def decode_related(repository: SQLiteStateRepository, connection: sqlite3.Connection, run_id: str,
                   rows: list[tuple[str, str, str]], metadata: list[tuple[Any, ...]],
                   archives: Mapping[str, tuple[int, int]], context: DecodeContext,
                   ) -> dict[str, dict[str, Any]]:
    from agent_run.sqlite_objects import _decode, _HISTORY, _HROOT

    if len(rows) != len(metadata) or {row[0] for row in rows} != {row[0] for row in metadata}:
        raise ValueError("Missing SQLite Run related Job Attempt")
    by_path = {row[0]: row for row in rows}
    ordered = [by_path[row[0]] for row in metadata]
    local = [(encode_json((_HISTORY, position)), kind, encoded)
             for position, (_, kind, encoded) in enumerate(ordered)]
    projected, _ = _decode(repository, connection, run_id,
        [("[]", "map", "null"), (_HROOT, "list", str(len(local))), *local], context=context)
    related = {}
    for (path, parent, position, role, status, ordinal), (_, kind, encoded), value in zip(
        metadata, ordered, projected[_HISTORY], strict=True,
    ):
        if (parent not in archives or type(position) is not int or not 0 <= position < archives[parent][1]
                or attempt_row(parent, position, value) != (path, parent, position, role, status, ordinal)
                or kind != "snapshot" or not isinstance(value, dict)):
            raise ValueError("Invalid SQLite Run related Job Attempt index")
        identity = scalar_value(encoded)
        if context.object_refs[path] != set(context.snapshot_refs[identity]):
            raise ValueError("Invalid SQLite Run related Job Attempt references")
        related[encode_json(object_path(parent)[:-1])] = value
    return related
