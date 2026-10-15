"""Separate immutable Ticket Attempt archives from current scheduling facts."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from agent_run.sqlite_encoding import encode_json, object_path

FIELD = "semantic_attempt_history"
FIELDS = frozenset({FIELD, "review_budget_history", "human_blocker_history", "publication_operation_retry_history",
                    "development_thread_history", "reviewer_thread_ids"})
CURRENT_WORK_PREDICATE = (
    "(instr(object_path, ',\"semantic_attempt_history\",')=0 OR NOT "
    "((object_path>='[\"ticket_jobs\",' AND object_path<'[\"ticket_jobs\"-') OR "
    "(object_path>='[\"active_ticket_job\",' AND object_path<'[\"active_ticket_job\"-')))"
)


def histories(state: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for key, job in jobs.items():
            if isinstance(job, dict):
                values.update((encode_json(("ticket_jobs", key, field)), job[field])
                              for field in FIELDS if field in job)
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        values.update((encode_json(("active_ticket_job", field)), active[field])
                      for field in FIELDS if field in active)
    return values


def sizes(state: Mapping[str, Any], *,
          snapshot_encodings: Mapping[tuple[str | int, ...], str] | None = None) -> dict[str, tuple[int, int]]:
    """Size restored archives, reusing only proved unmodified encodings."""
    encodings = snapshot_encodings or {}
    result = {}
    for encoded, value in histories(state).items():
        path = object_path(encoded)
        if path in encodings:
            size = len(encodings[path].encode("utf-8"))
        elif isinstance(value, list) and all((*path, index) in encodings for index in range(len(value))):
            size = 2 + max(len(value) - 1, 0) + sum(
                len(encodings[(*path, index)].encode("utf-8")) for index in range(len(value)))
        else:
            size = len(encode_json(value).encode("utf-8"))
        result[encoded] = size, len(value) if isinstance(value, list) else -1
    return result


def decode_sizes(rows: Iterable[tuple[Any, ...]], limit: int) -> dict[str, tuple[int, int]]:
    """Validate archive metadata captured with the work objects' SQL snapshot."""
    values: dict[str, tuple[int, int]] = {}
    for encoded, size, count, kind in rows:
        path = object_path(encoded)
        expected_kind = "list" if path[-1:] == (FIELD,) else "snapshot"
        if (type(size) is not int or not 2 <= size <= limit or type(count) is not int
                or not 0 <= count <= limit or kind != expected_kind
                or not (len(path) == 2 and path[0] == "active_ticket_job" and path[1] in FIELDS
                        or len(path) == 3 and path[0] == "ticket_jobs" and isinstance(path[1], str) and path[2] in FIELDS)):
            raise ValueError("Invalid SQLite Run Job history size")
        values[encoded] = size, count
    return values


def read_sizes(connection: sqlite3.Connection, run_id: str, limit: int) -> dict[str, tuple[int, int]]:
    byte_count = connection.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+64),0) FROM run_job_history WHERE run_id = ?", (run_id,),
    ).fetchone()[0]
    if byte_count > 8 * limit:
        raise ValueError(f"Run state exceeds {limit} byte persistence limit")
    rows = connection.execute(
        "SELECT j.object_path, j.logical_bytes, j.item_count, o.kind FROM run_job_history j "
        "LEFT JOIN run_objects o ON o.run_id=j.run_id AND o.object_path=j.object_path WHERE j.run_id = ?", (run_id,))
    return decode_sizes(rows, limit)


def check_selected(state: Mapping[str, Any], indexed: Mapping[str, tuple[int, int]], *,
                   snapshot_encodings: Mapping[tuple[str | int, ...], str] | None = None) -> None:
    if any(indexed.get(path) != size for path, size in sizes(state, snapshot_encodings=snapshot_encodings).items()):
        raise ValueError("Invalid SQLite Run Job history size")


def index(connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any]) -> None:
    connection.execute("DELETE FROM run_job_history WHERE run_id = ?", (run_id,))
    connection.executemany("INSERT INTO run_job_history VALUES (?, ?, ?, ?)",
                           ((run_id, path, *size) for path, size in sizes(state).items()))
    connection.execute("DELETE FROM run_job_attempts WHERE run_id = ?", (run_id,))
    connection.executemany("INSERT INTO run_job_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ((run_id, *row) for row in attempt_rows(state)))


def check(connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any], limit: int) -> None:
    if read_sizes(connection, run_id, limit) != sizes(state):
        raise ValueError("Invalid SQLite Run Job history size")
    actual = list(connection.execute(
        "SELECT object_path,parent_path,position,role,status,ordinal FROM run_job_attempts WHERE run_id = ? ORDER BY object_path", (run_id,)))
    if actual != sorted(attempt_rows(state)):
        raise ValueError("Invalid SQLite Run Job Attempt index")


def attempt_row(parent: str, position: int, value: Any) -> tuple[Any, ...]:
    attempt = value if isinstance(value, dict) else {}
    role, status, ordinal = (attempt.get(key) for key in ("role", "status", "ordinal"))
    return (encode_json((*object_path(parent), position)), parent, position,
            role if isinstance(role, str) else None, status if isinstance(status, str) else None,
            ordinal if type(ordinal) is int else None)


def attempt_rows(state: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    return [attempt_row(parent, position, value)
            for parent, history in histories(state).items() if object_path(parent)[-1] == FIELD and isinstance(history, list)
            for position, value in enumerate(history)]
