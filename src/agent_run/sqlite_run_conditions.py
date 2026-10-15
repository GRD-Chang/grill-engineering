"""Explicit Run gates with immutable diagnostics and original evidence refs."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_change_job_codec import _assign, _flatten, _shape, _typed, presence, present
from agent_run.sqlite_run_graph import load_graph, save_graph
from agent_run.sqlite_run_inputs import identity, owner
from agent_run.sqlite_run_schema import DIAGNOSTIC, RECORDS, column, record_table
from agent_run.sqlite_work_evidence import get_evidence, put_evidence

MAX_CONDITION_BYTES = 256 * 1024
_SEQUENCES = frozenset({"NUMBERS", "EDGES", "DIAGNOSTICS"})


def _bounded(values: list[Any]) -> None:
    if sum(len(value.encode("utf-8")) if isinstance(value, str) else 16 for value in values) > MAX_CONDITION_BYTES:
        raise ValueError("Run condition current record exceeds byte limit")


def _check_bytes(c: sqlite3.Connection, table: str, columns: list[str], predicate: str, params: tuple[Any, ...]) -> None:
    size = c.execute("SELECT " + "+".join("COALESCE(LENGTH(CAST(" + name + " AS BLOB)),0)" for name in columns)
                     + f" FROM {table} WHERE {predicate}", params).fetchone()
    if size is not None and (type(size[0]) is not int or size[0] + 16 * len(columns) > MAX_CONDITION_BYTES):
        raise ValueError("Run condition current record exceeds byte limit")


def _read(c: sqlite3.Connection, table: str, columns: list[str], predicate: str, params: tuple[Any, ...]) -> Any:
    _check_bytes(c, table, columns, predicate, params)
    return c.execute("SELECT " + ",".join(columns) + f" FROM {table} WHERE {predicate}", params).fetchone()


def _list(c: sqlite3.Connection, run_id: str, container: str, path: str, value: Any, kind: str) -> None:
    c.execute("DELETE FROM run_condition_tickets WHERE run_id=? AND condition_kind=? AND list_name=?", (run_id, container, path))
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 4096:
        raise ValueError("Invalid or oversized Run condition Ticket list")
    for position, item in enumerate(value):
        if kind == "NUMBERS":
            number, blocker = item, None
        else:
            if not isinstance(item, Mapping) or set(item) != {"ticket_number", "blocked_by"}:
                raise ValueError("Invalid Run condition dependency")
            number, blocker = item["ticket_number"], item["blocked_by"]
        if type(number) is not int or number < 1 or blocker is not None and (type(blocker) is not int or blocker < 1):
            raise ValueError("Invalid Run condition Ticket identity")
        c.execute("INSERT INTO run_condition_tickets VALUES (?,?,?,?,?,?)", (run_id, container, path, position, number, blocker))


def _load_list(c: sqlite3.Connection, run_id: str, container: str, path: str, kind: str) -> list[Any]:
    rows = c.execute("SELECT position,ticket_number,blocker_number FROM run_condition_tickets "
                     "WHERE run_id=? AND condition_kind=? AND list_name=? ORDER BY position LIMIT 4097", (run_id, container, path)).fetchall()
    if len(rows) > 4096:
        raise ValueError("Run condition query exceeds item limit")
    result = []
    for expected, (position, number, blocker) in enumerate(rows):
        if (position != expected or type(number) is not int or number < 1
                or kind == "NUMBERS" and blocker is not None
                or kind == "EDGES" and (type(blocker) is not int or blocker < 1)):
            raise ValueError("Invalid Run condition Ticket reference")
        result.append(number if kind == "NUMBERS" else {"ticket_number": number, "blocked_by": blocker})
    return result


def _encode(c: sqlite3.Connection, run_id: str, container: str, value: Mapping[str, Any], schema: Mapping[str, str]) -> tuple[Any, ...]:
    flat = _flatten(value, schema)
    values = []
    for path, kind in schema.items():
        item = flat.get(path)
        if kind in {"NUMBERS", "EDGES"}:
            _list(c, run_id, container, path, item, kind)
            continue
        if kind == "DIAGNOSTICS":
            save_diagnostics(c, run_id, container + "." + path, item)
            continue
        if kind == "EVIDENCE":
            item = put_evidence(c, run_id, owner(run_id), container + "." + path, item) if item is not None else None
        elif kind == "GRAPH":
            item = save_graph(c, run_id, item)
        else:
            item = _typed(item, kind, path)
        values.append(item)
    _bounded(values)
    return (presence(flat, _shape(schema)), presence((key for key, item in flat.items() if item is None), _shape(schema)), *values)


def _decode(c: sqlite3.Connection, run_id: str, container: str, row: tuple[Any, ...], schema: Mapping[str, str]) -> dict[str, Any]:
    keys, nulls = present(row[0], _shape(schema)), present(row[1], _shape(schema))
    if nulls - keys:
        raise ValueError("Invalid Run condition null fields")
    result: dict[str, Any] = {}
    for path in sorted(keys - schema.keys(), key=lambda item: item.count(".")):
        _assign(result, path, None if path in nulls else {})
    values = iter(row[2:])
    for path, kind in schema.items():
        item = next(values) if kind not in _SEQUENCES else None
        if path not in keys:
            continue
        if path in nulls:
            _assign(result, path, None)
            continue
        if kind in {"NUMBERS", "EDGES"}:
            item = _load_list(c, run_id, container, path, kind)
        elif kind == "DIAGNOSTICS":
            item = load_diagnostics(c, run_id, container + "." + path)
        elif kind == "EVIDENCE":
            if not isinstance(item, str):
                raise ValueError("Invalid Run condition evidence reference")
            item = get_evidence(c, run_id, item, owner_id=owner(run_id), kind=container + "." + path)
        elif kind == "GRAPH":
            if not isinstance(item, str):
                raise ValueError("Invalid Run condition graph reference")
            item = load_graph(c, run_id, item)
        elif kind == "BOOLEAN":
            if type(item) is not int or item not in {0, 1}:
                raise ValueError("Invalid Run condition boolean")
            item = bool(item)
        else:
            _typed(item, kind, path)
        _assign(result, path, item)
    return result


def save_record(c: sqlite3.Connection, run_id: str, field: str, value: Any) -> None:
    table, schema = record_table(field), RECORDS[field]
    c.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    c.execute("DELETE FROM run_condition_tickets WHERE run_id=? AND condition_kind=?", (run_id, field))
    if field == "operator_stop":
        c.execute("DELETE FROM run_diagnostic_slots WHERE run_id=? AND container=?", (run_id, "operator_stop.resume_diagnostics"))
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError("Run condition must be an object")
    values = _encode(c, run_id, field, value, schema)
    c.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(len(values) + 1))})", (run_id, *values))
    _check_bytes(c, table, ["presence", "nulls", *(column(path) for path, kind in schema.items() if kind not in _SEQUENCES)],
                 "run_id=?", (run_id,))


def load_record(c: sqlite3.Connection, run_id: str, field: str) -> Any:
    schema = RECORDS[field]
    columns = [column(path) for path, kind in schema.items() if kind not in _SEQUENCES]
    row = _read(c, record_table(field), ["presence", "nulls", *columns], "run_id=?", (run_id,))
    if row is None:
        raise ValueError("Missing Run condition record")
    _bounded(list(row[2:]))
    return _decode(c, run_id, field, row, schema)


def save_diagnostics(c: sqlite3.Connection, run_id: str, container: str, value: Any) -> None:
    c.execute("DELETE FROM run_diagnostic_slots WHERE run_id=? AND container=?", (run_id, container))
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Invalid or oversized Run diagnostics")
    core_roots = {path.split(".", 1)[0] for path in DIAGNOSTIC}
    for position, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError("Run diagnostic must be an object")
        diagnostic_id = identity(item)
        record = "diagnostic:" + diagnostic_id
        previous = c.execute("SELECT diagnostic_id FROM run_diagnostic_versions WHERE run_id=? AND diagnostic_id=?",
                             (run_id, diagnostic_id)).fetchone()
        if previous is not None:
            if _load_diagnostic(c, run_id, diagnostic_id) != item:
                raise ValueError("Immutable Run diagnostic was changed")
            c.execute("INSERT INTO run_diagnostic_slots VALUES (?,?,?,?)", (run_id, container, position, diagnostic_id))
            continue
        report_id = put_evidence(c, run_id, owner(run_id), "diagnostic.report",
                                 {key: part for key, part in item.items() if key not in core_roots})
        core = {key: part for key, part in item.items() if key in core_roots}
        values = _encode(c, run_id, record, core, DIAGNOSTIC)
        c.execute("INSERT INTO run_diagnostic_versions VALUES (" + ",".join("?" for _ in range(len(values) + 3)) + ")",
                  (run_id, diagnostic_id, *values[:2], report_id, *values[2:]))
        _check_bytes(c, "run_diagnostic_versions", ["presence", "nulls", "report_id",
                     *(column(path) for path, kind in DIAGNOSTIC.items() if kind not in _SEQUENCES)],
                     "run_id=? AND diagnostic_id=?", (run_id, diagnostic_id))
        c.execute("INSERT INTO run_diagnostic_slots VALUES (?,?,?,?)", (run_id, container, position, diagnostic_id))


def _load_diagnostic(c: sqlite3.Connection, run_id: str, diagnostic_id: str) -> dict[str, Any]:
    columns = [column(path) for path, kind in DIAGNOSTIC.items() if kind not in _SEQUENCES]
    row = _read(c, "run_diagnostic_versions", ["presence", "nulls", "report_id", *columns],
                "run_id=? AND diagnostic_id=?", (run_id, diagnostic_id))
    if row is None:
        raise ValueError("Missing Run diagnostic identity")
    result = get_evidence(c, run_id, row[2], owner_id=owner(run_id), kind="diagnostic.report")
    if not isinstance(result, dict):
        raise ValueError("Invalid Run diagnostic report")
    result.update(_decode(c, run_id, "diagnostic:" + diagnostic_id, (*row[:2], *row[3:]), DIAGNOSTIC))
    if identity(result) != diagnostic_id:
        raise ValueError("Invalid immutable Run diagnostic digest")
    return result


def load_diagnostics(c: sqlite3.Connection, run_id: str, container: str) -> list[dict[str, Any]]:
    rows = c.execute("SELECT position,diagnostic_id FROM run_diagnostic_slots WHERE run_id=? AND container=? ORDER BY position LIMIT 33",
                     (run_id, container)).fetchall()
    if len(rows) > 32:
        raise ValueError("Run diagnostics exceed item limit")
    result = []
    for position, (ordinal, diagnostic_id) in enumerate(rows):
        if type(ordinal) is not int or ordinal != position:
            raise ValueError("Invalid Run diagnostic position")
        result.append(_load_diagnostic(c, run_id, diagnostic_id))
    return result
