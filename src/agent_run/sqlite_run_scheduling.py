"""Bounded Ticket selectors, FIFO seats, pause requests and generations."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_run_inputs import owner
from agent_run.sqlite_work_evidence import get_evidence, put_evidence

MAX_SCHEDULING_ITEMS = 4096
MAX_SCHEDULING_BYTES = 256 * 1024
_MAPS = {
    "ticket_pause_requests": ("run_ticket_pause_requests", "requested_at"),
    "retired_ticket_generations": ("run_ticket_generations", "generation"),
    "pending_ticket_retirements": ("run_ticket_retirements", "evidence_id"),
}
_LISTS = {"frontier": "run_frontier", "ticket_work_queue": "run_work_queue",
          "ticket_integration_queue": "run_integration_queue"}


def _number(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Invalid Run scheduling Ticket number")
    return value


def _key(value: Any) -> int:
    if not isinstance(value, str) or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("Invalid Run scheduling Ticket key")
    return _number(int(value))


def _bounded(c: sqlite3.Connection, source: str, columns: str, params: tuple[Any, ...]) -> None:
    names = columns.split(",")
    size = "+".join("COALESCE(LENGTH(CAST(" + name + " AS BLOB)),0)+16" for name in names)
    count, byte_count = c.execute(f"SELECT COUNT(*),COALESCE(SUM({size}),0) FROM {source}", params).fetchone()
    if count > MAX_SCHEDULING_ITEMS or byte_count > MAX_SCHEDULING_BYTES:
        raise ValueError("Run scheduling record exceeds byte or item limit")


def save(c: sqlite3.Connection, run_id: str, field: str, value: Any, *, keys: set[str] | None = None) -> None:
    if field in _MAPS:
        table, column = _MAPS[field]
        if value is None:
            value = {}
        if not isinstance(value, Mapping) or len(value) > MAX_SCHEDULING_ITEMS:
            raise ValueError("Invalid or oversized Run scheduling map")
        if keys is None:
            c.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            keys = set(value)
        for key in keys:
            number = _key(key)
            c.execute(f"DELETE FROM {table} WHERE run_id=? AND ticket_number=?", (run_id, number))
            if key not in value:
                continue
            item = value[key]
            if field == "ticket_pause_requests":
                if not isinstance(item, str):
                    raise ValueError("Invalid Run Ticket pause timestamp")
            elif field == "retired_ticket_generations":
                _number(item)
            else:
                item = put_evidence(c, run_id, owner(run_id), "ticket_retirement:" + key, item)
            c.execute(f"INSERT INTO {table}(run_id,ticket_number,{column}) VALUES (?,?,?)", (run_id, number, item))
        _bounded(c, table + " WHERE run_id=?", "ticket_number," + column, (run_id,))
        return
    table = _LISTS[field]
    c.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    if value is None:
        return
    if not isinstance(value, list) or len(value) > MAX_SCHEDULING_ITEMS:
        raise ValueError("Invalid or oversized Run scheduling queue")
    seen = set()
    for position, item in enumerate(value):
        row: tuple[Any, ...]
        if field == "frontier":
            row = (_number(item),)
        elif field == "ticket_work_queue":
            if not isinstance(item, Mapping) or set(item) != {"ticket_number", "generation", "role", "lease_id"}:
                raise ValueError("Invalid Run work queue entry")
            token = item["lease_id"]
            if not isinstance(item["role"], str) or token is not None and not isinstance(token, str):
                raise ValueError("Invalid Run work queue ownership")
            row = (_number(item["ticket_number"]), _number(item["generation"]), item["role"], token)
        else:
            if not isinstance(item, Mapping) or item.keys() - {"ticket_number", "ready_at"}:
                raise ValueError("Invalid Run integration queue entry")
            timestamp = item.get("ready_at")
            if timestamp is not None and not isinstance(timestamp, str):
                raise ValueError("Invalid Run integration readiness timestamp")
            row = (_number(item["ticket_number"]), timestamp, int("ready_at" in item))
        if row[0] in seen:
            raise ValueError("Duplicate Run scheduling Ticket")
        seen.add(row[0])
        c.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(len(row) + 2))})", (run_id, position, *row))
    columns = {"frontier": "ticket_number", "ticket_work_queue": "ticket_number,generation,role,lease_id",
               "ticket_integration_queue": "ticket_number,ready_at,ready_at_present"}[field]
    _bounded(c, table + " WHERE run_id=?", columns, (run_id,))


def load(c: sqlite3.Connection, run_id: str, field: str, *, keys: set[str] | None = None) -> Any:
    if field in _MAPS:
        table, column = _MAPS[field]
        query = f"SELECT ticket_number,{column} FROM {table} WHERE run_id=?"
        params: tuple[Any, ...] = (run_id,)
        if keys is not None:
            if not keys:
                return {}
            query += " AND ticket_number IN (" + ",".join("?" for _ in keys) + ")"
            params += tuple(_key(key) for key in sorted(keys))
        _bounded(c, query.split(" FROM ", 1)[1], "ticket_number," + column, params)
        query += " ORDER BY ticket_number LIMIT ?"
        rows = c.execute(query, (*params, MAX_SCHEDULING_ITEMS + 1)).fetchall()
        if len(rows) > MAX_SCHEDULING_ITEMS:
            raise ValueError("Run scheduling query exceeds item limit")
        map_result = {}
        for number, item in rows:
            _number(number)
            key = str(number)
            if field == "ticket_pause_requests" and not isinstance(item, str):
                raise ValueError("Invalid Run Ticket pause timestamp")
            if field == "retired_ticket_generations":
                _number(item)
            if field == "pending_ticket_retirements":
                item = get_evidence(c, run_id, item, owner_id=owner(run_id), kind="ticket_retirement:" + key)
            map_result[key] = item
        return map_result
    table = _LISTS[field]
    columns = {"frontier": "ticket_number", "ticket_work_queue": "ticket_number,generation,role,lease_id",
               "ticket_integration_queue": "ticket_number,ready_at,ready_at_present"}[field]
    _bounded(c, table + " WHERE run_id=?", columns, (run_id,))
    rows = c.execute(f"SELECT position,{columns} FROM {table} WHERE run_id=? ORDER BY position LIMIT ?",
                     (run_id, MAX_SCHEDULING_ITEMS + 1)).fetchall()
    if len(rows) > MAX_SCHEDULING_ITEMS:
        raise ValueError("Run scheduling query exceeds item limit")
    list_result = []
    for position, row in enumerate(rows):
        if type(row[0]) is not int or row[0] != position:
            raise ValueError("Invalid Run scheduling queue position")
        _number(row[1])
        if field == "frontier":
            item = row[1]
        elif field == "ticket_work_queue":
            _number(row[2])
            if not isinstance(row[3], str) or row[4] is not None and not isinstance(row[4], str):
                raise ValueError("Invalid Run work queue ownership")
            item = dict(zip(("ticket_number", "generation", "role", "lease_id"), row[1:], strict=True))
        else:
            if row[2] is not None and not isinstance(row[2], str) or type(row[3]) is not int or row[3] not in {0, 1}:
                raise ValueError("Invalid Run integration readiness timestamp")
            item = {"ticket_number": row[1]}
            if row[3]:
                item["ready_at"] = row[2]
        list_result.append(item)
    return list_result
