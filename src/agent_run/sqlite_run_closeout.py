"""Run abandonment and cleanup intents/receipts without mutable JSON copies."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_change_job_codec import _typed, presence, present
from agent_run.sqlite_mutations import _same
from agent_run.sqlite_run_inputs import identity

FIELDS = frozenset({"run_abandonment", "delivery_cleanup"})
_CLOSE = {
    **{key: "TEXT" for key in ("run_id", "integrated_sha", "delivery_type", "actor", "intent_created_at", "intent_binding")},
    **{key: "INTEGER" for key in ("parent_number", "ticket_number", "pr_number", "baseline_event_id")},
    "event_id": "IDENTITY", "dispatch_attempted": "BOOLEAN", "comment_dispatch_attempted": "BOOLEAN",
}
_SCHEMAS = {
    "cleanup": {"status": "TEXT", "last_error": "TEXT", "items": "CHILDREN"},
    "cleanup_item": {**{key: "TEXT" for key in ("kind", "expected_head_sha", "expected_remote_head_sha", "checkout", "status", "last_error", "recovery_kind")},
                     "attempts": "INTEGER", "branch": "KEY"},
    "abandonment": {"phase": "TEXT", "kind": "TEXT", **{key: "CHILDREN" for key in ("parent_pr", "final_pr", "change_prs", "tickets")}},
    "abandonment_pr": {"pr_number": "INTEGER", "status": "TEXT"},
    "abandonment_ticket": {"ticket_number": "INTEGER", "pr_number": "INTEGER", "integrated_sha": "TEXT", "eligible": "BOOLEAN", "status": "TEXT",
                           "recorded_ownership": "RECEIPT", "expected_ownership": "RECEIPT"},
    "close_receipt": _CLOSE,
}


def _columns(schema: Mapping[str, str]) -> list[str]:
    return [key for key, kind in schema.items() if kind not in {"CHILDREN", "KEY"}]


def _ddl(schema: Mapping[str, str]) -> str:
    return ", ".join("value_" + key + " " + ("INTEGER" if kind == "BOOLEAN" else "TEXT" if kind == "RECEIPT" else "BLOB" if kind == "IDENTITY" else kind)
                     for key, kind in schema.items() if kind not in {"CHILDREN", "KEY"})


_FK = "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE"
TABLES = {
    "run_closeout_headers": "CREATE TABLE run_closeout_headers (run_id TEXT NOT NULL, kind TEXT NOT NULL, presence BLOB NOT NULL, "
        "nulls BLOB NOT NULL, value_phase TEXT, value_kind TEXT, value_status TEXT, value_last_error TEXT, PRIMARY KEY(run_id,kind), " + _FK + ")",
    "run_cleanup_items": "CREATE TABLE run_cleanup_items (run_id TEXT NOT NULL, branch TEXT NOT NULL, presence BLOB NOT NULL, nulls BLOB NOT NULL, "
        + _ddl(_SCHEMAS["cleanup_item"]) + ", PRIMARY KEY(run_id,branch), " + _FK + ")",
    "run_abandonment_prs": "CREATE TABLE run_abandonment_prs (run_id TEXT NOT NULL, container TEXT NOT NULL, position INTEGER NOT NULL, "
        "presence BLOB NOT NULL, nulls BLOB NOT NULL, " + _ddl(_SCHEMAS["abandonment_pr"])
        + ", PRIMARY KEY(run_id,container,position), " + _FK + ")",
    "run_abandonment_tickets": "CREATE TABLE run_abandonment_tickets (run_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "presence BLOB NOT NULL, nulls BLOB NOT NULL, " + _ddl(_SCHEMAS["abandonment_ticket"])
        + ", PRIMARY KEY(run_id,position), " + _FK + ")",
    "run_close_receipts": "CREATE TABLE run_close_receipts (run_id TEXT NOT NULL, receipt_id TEXT NOT NULL, source_job_id TEXT, source_record_kind TEXT, "
        "presence BLOB NOT NULL, nulls BLOB NOT NULL, " + _ddl(_CLOSE) + ", PRIMARY KEY(run_id,receipt_id), " + _FK
        + ", FOREIGN KEY(run_id,source_job_id) REFERENCES work_jobs(run_id,job_id) ON DELETE CASCADE)",
}
INDEXES: dict[str, str] = {}


def _bounds(c: sqlite3.Connection, table: str, columns: list[str], predicate: str, params: tuple[Any, ...]) -> None:
    size = "+".join("COALESCE(LENGTH(CAST(" + name + " AS BLOB)),0)" for name in columns)
    count, byte_count = bounded_query(c, f"SELECT COUNT(*),COALESCE(SUM({size}),0) FROM {table} WHERE {predicate}", params).fetchone()
    if count > 4096 or byte_count + 16 * len(columns) * count > 256 * 1024:
        raise ValueError("Run closeout current record exceeds byte or item limit")


def _encode(c: sqlite3.Connection, run_id: str, schema: Mapping[str, str], value: Any) -> tuple[Any, ...]:
    if not isinstance(value, Mapping) or value.keys() - schema.keys():
        raise ValueError("Invalid Run closeout record fields")
    values = []
    for key in _columns(schema):
        item, kind = value.get(key), schema[key]
        item = _save_receipt(c, run_id, item) if kind == "RECEIPT" and item is not None else _typed(item, kind, key) if kind != "RECEIPT" else None
        values.append(item)
    branch_bytes = len(value.get("branch", "").encode("utf-8")) if schema.get("branch") == "KEY" else 0
    if branch_bytes + sum(len(item.encode("utf-8")) if isinstance(item, str) else 16 for item in values) > 256 * 1024:
        raise ValueError("Run closeout current record exceeds byte limit")
    return (presence(value, schema), presence((key for key, item in value.items() if item is None), schema), *values)


def _decode(c: sqlite3.Connection, run_id: str, schema: Mapping[str, str], row: tuple[Any, ...], *, key_value: str | None = None) -> dict[str, Any]:
    keys, nulls = present(row[0], schema), present(row[1], schema)
    if nulls - keys:
        raise ValueError("Invalid Run closeout null fields")
    values = dict(zip(_columns(schema), row[2:], strict=True))
    result = {}
    for key in keys:
        kind = schema[key]
        item: Any
        if key in nulls:
            item = None
        elif kind == "KEY":
            item = key_value
        elif kind == "CHILDREN":
            item = {} if key == "items" else [] if key in {"change_prs", "tickets"} else {}
        else:
            item = values[key]
            if kind == "RECEIPT":
                item = _load_receipt(c, run_id, item)
            elif kind == "BOOLEAN":
                if type(item) is not int or item not in {0, 1}:
                    raise ValueError("Invalid Run closeout boolean")
                item = bool(item)
            else:
                _typed(item, kind, key)
        result[key] = item
    return result


def _save_receipt(c: sqlite3.Connection, run_id: str, value: Any) -> str:
    from agent_run.sqlite_change_job_codec import load_record

    if not isinstance(value, Mapping):
        raise ValueError("Run closeout receipt must be an object")
    receipt_id = identity(value)
    if bounded_query(c, "SELECT 1 FROM run_close_receipts WHERE run_id=? AND receipt_id=?", (run_id, receipt_id)).fetchone():
        if not _same(_load_receipt(c, run_id, receipt_id), value):
            raise ValueError("Immutable Run closeout receipt was changed")
        return receipt_id
    source_job = source_kind = None
    source = bounded_query(c, "SELECT job_id FROM work_job_slots WHERE run_id=? AND container='ticket_jobs' AND slot=?",
                       (run_id, str(value.get("ticket_number")))).fetchone()
    if source is not None:
        owner_row = bounded_query(c, "SELECT evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, source[0])).fetchone()
        for kind in ("ticket_close_ownership", "ticket_close_intent"):
            if _same(load_record(c, run_id, source[0], kind, owner_id=owner_row[0]), value):
                source_job, source_kind = source[0], kind
                break
    encoded = _encode(c, run_id, _CLOSE, {} if source_job else value)
    c.execute("INSERT INTO run_close_receipts VALUES (" + ",".join("?" for _ in range(len(encoded) + 4)) + ")",
              (run_id, receipt_id, source_job, source_kind, *encoded))
    return receipt_id


def _load_receipt(c: sqlite3.Connection, run_id: str, receipt_id: str) -> dict[str, Any]:
    from agent_run.sqlite_change_job_codec import load_record

    columns = ",".join("value_" + key for key in _columns(_CLOSE))
    _bounds(c, "run_close_receipts", ["source_job_id", "source_record_kind", "presence", "nulls", *columns.split(",")],
            "run_id=? AND receipt_id=?", (run_id, receipt_id))
    row = bounded_query(c, f"SELECT source_job_id,source_record_kind,presence,nulls,{columns} FROM run_close_receipts WHERE run_id=? AND receipt_id=?",
                    (run_id, receipt_id)).fetchone()
    if row is None:
        raise ValueError("Missing exact Run closeout receipt")
    if row[0] is not None:
        if row[1] not in {"ticket_close_ownership", "ticket_close_intent"}:
            raise ValueError("Invalid Run closeout receipt source")
        owner_row = bounded_query(c, "SELECT evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, row[0])).fetchone()
        value = load_record(c, run_id, row[0], row[1], owner_id=owner_row[0]) if owner_row else None
    else:
        value = _decode(c, run_id, _CLOSE, row[2:])
    if not isinstance(value, dict) or identity(value) != receipt_id:
        raise ValueError("Run closeout receipt version changed")
    return value


def save(c: sqlite3.Connection, run_id: str, field: str, value: Any) -> None:
    kind = "cleanup" if field == "delivery_cleanup" else "abandonment"
    schema = _SCHEMAS[kind]
    c.execute("DELETE FROM run_closeout_headers WHERE run_id=? AND kind=?", (run_id, kind))
    for table in (["run_cleanup_items"] if kind == "cleanup" else ["run_abandonment_prs", "run_abandonment_tickets"]):
        c.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    if value is None:
        return
    encoded = _encode(c, run_id, schema, value)
    flat = dict(zip(_columns(schema), encoded[2:], strict=True))
    c.execute("INSERT INTO run_closeout_headers VALUES (?,?,?,?,?,?,?,?)", (run_id, kind, *encoded[:2],
              flat.get("phase"), flat.get("kind"), flat.get("status"), flat.get("last_error")))
    _bounds(c, "run_closeout_headers", ["presence", "nulls", "value_phase", "value_kind", "value_status", "value_last_error"],
            "run_id=? AND kind=?", (run_id, kind))
    if kind == "cleanup":
        items = value.get("items", {})
        if items is None:
            return
        if not isinstance(items, Mapping) or len(items) > 4096:
            raise ValueError("Invalid or oversized Run cleanup items")
        for branch, item in items.items():
            if not isinstance(branch, str) or isinstance(item, Mapping) and "branch" in item and item["branch"] != branch:
                raise ValueError("Run cleanup branch identity mismatch")
            values = _encode(c, run_id, _SCHEMAS["cleanup_item"], item)
            c.execute("INSERT INTO run_cleanup_items VALUES (" + ",".join("?" for _ in range(len(values) + 2)) + ")", (run_id, branch, *values))
        _bounds(c, "run_cleanup_items", ["branch", "presence", "nulls", *("value_" + key for key in _columns(_SCHEMAS["cleanup_item"]))],
                "run_id=?", (run_id,))
        return
    for container in ("parent_pr", "final_pr", "change_prs", "tickets"):
        items = value.get(container)
        if items is None:
            continue
        if container in {"parent_pr", "final_pr"}:
            items = [items]
        if not isinstance(items, list) or len(items) > 4096:
            raise ValueError("Invalid or oversized Run abandonment obligations")
        ticket = container == "tickets"
        for position, item in enumerate(items):
            values = _encode(c, run_id, _SCHEMAS["abandonment_ticket" if ticket else "abandonment_pr"], item)
            table, prefix = ("run_abandonment_tickets", (run_id, position)) if ticket else ("run_abandonment_prs", (run_id, container, position))
            c.execute(f"INSERT INTO {table} VALUES (" + ",".join("?" for _ in range(len(values) + len(prefix))) + ")", (*prefix, *values))
        table = "run_abandonment_tickets" if ticket else "run_abandonment_prs"
        _bounds(c, table, ["presence", "nulls", *("value_" + key for key in _columns(_SCHEMAS["abandonment_ticket" if ticket else "abandonment_pr"]))],
                "run_id=?", (run_id,))


def load(c: sqlite3.Connection, run_id: str, field: str) -> dict[str, Any]:
    kind = "cleanup" if field == "delivery_cleanup" else "abandonment"
    schema = _SCHEMAS[kind]
    _bounds(c, "run_closeout_headers", ["presence", "nulls", "value_phase", "value_kind", "value_status", "value_last_error"],
            "run_id=? AND kind=?", (run_id, kind))
    row = bounded_query(c, "SELECT presence,nulls,value_phase,value_kind,value_status,value_last_error FROM run_closeout_headers WHERE run_id=? AND kind=?",
                    (run_id, kind)).fetchone()
    if row is None:
        raise ValueError("Missing Run closeout header")
    all_values = dict(zip(("phase", "kind", "status", "last_error"), row[2:], strict=True))
    result = _decode(c, run_id, schema, (*row[:2], *(all_values[key] for key in _columns(schema))))
    if kind == "cleanup":
        if isinstance(result.get("items"), dict):
            _bounds(c, "run_cleanup_items", ["branch", "presence", "nulls", *("value_" + key for key in _columns(_SCHEMAS["cleanup_item"]))],
                    "run_id=?", (run_id,))
            rows = bounded_query(c, "SELECT branch,presence,nulls," + ",".join("value_" + key for key in _columns(_SCHEMAS["cleanup_item"]))
                             + " FROM run_cleanup_items WHERE run_id=? ORDER BY branch LIMIT 4097", (run_id,)).fetchall()
            if len(rows) > 4096:
                raise ValueError("Run cleanup query exceeds item limit")
            result["items"] = {item[0]: _decode(c, run_id, _SCHEMAS["cleanup_item"], item[1:], key_value=item[0]) for item in rows}
        return result
    for container in ("parent_pr", "final_pr", "change_prs", "tickets"):
        if container not in result or result[container] is None:
            continue
        ticket = container == "tickets"
        child_schema = _SCHEMAS["abandonment_ticket" if ticket else "abandonment_pr"]
        table = "run_abandonment_tickets" if ticket else "run_abandonment_prs"
        predicate, params = ("run_id=?", (run_id,)) if ticket else ("run_id=? AND container=?", (run_id, container))
        _bounds(c, table, ["presence", "nulls", *("value_" + key for key in _columns(child_schema))], predicate, params)
        rows = bounded_query(c, "SELECT position,presence,nulls," + ",".join("value_" + key for key in _columns(child_schema))
                         + f" FROM {table} WHERE {predicate} ORDER BY position LIMIT 4097", params).fetchall()
        if len(rows) > 4096 or any(item[0] != position for position, item in enumerate(rows)):
            raise ValueError("Invalid Run abandonment obligation positions")
        items = [_decode(c, run_id, child_schema, item[1:]) for item in rows]
        if container in {"parent_pr", "final_pr"}:
            if len(items) != 1:
                raise ValueError("Missing Run abandonment PR obligation")
            result[container] = items[0]
        else:
            result[container] = items
    return result
