"""剩余 Run 域的明确关系入口；公开 JSON 只由查询组装。"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any

from agent_run.sqlite_change_job_codec import presence, present, _typed
from agent_run import sqlite_timeline as timeline
from agent_run import sqlite_run_resume as resume
from agent_run.state_limits import require_record_size

FIELDS = frozenset(
    {
        "timeline",
        "timeline_continuation",
        "timeline_at_capacity",
        "resume_audit",
        "human_response_audit",
        "notifications",
        "action_application_receipt",
    }
)
_NOTIFICATION = {
    "enabled": "BOOLEAN",
    "open_id": "TEXT",
    "profile": "TEXT",
    "app_id": "TEXT",
    "mode": "TEXT",
    "unavailable_reason": "TEXT",
}
_RECEIPT = {
    "protocol": "INTEGER",
    "action_id": "TEXT",
    "kind": "TEXT",
    "payload_digest": "TEXT",
    "run_id": "TEXT",
    "executor_generation": "INTEGER",
    "applied_at": "TEXT",
    "qualified_ticket_number": "INTEGER",
}
TABLES = {
    "run_query_roots": "CREATE TABLE run_query_roots (run_id TEXT PRIMARY KEY,presence BLOB NOT NULL,timeline_at_capacity INTEGER,FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "run_notification_config": "CREATE TABLE run_notification_config (run_id TEXT PRIMARY KEY,presence BLOB NOT NULL,"
    + ",".join(key + (" INTEGER" if kind == "BOOLEAN" else " TEXT") for key, kind in _NOTIFICATION.items())
    + ",FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "run_application_receipt": "CREATE TABLE run_application_receipt (owner_run_id TEXT PRIMARY KEY,presence BLOB NOT NULL,"
    + ",".join(key + " " + kind for key, kind in _RECEIPT.items())
    + ",FOREIGN KEY(owner_run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    **timeline.TABLES,
    **resume.TABLES,
}
INDEXES = {**timeline.INDEXES, **resume.INDEXES}


def _keys(c: sqlite3.Connection, run_id: str) -> set[str]:
    require_record_size(c, "run_query_roots", ("run_id", "presence", "timeline_at_capacity"), "run_id=?", (run_id,))
    row = c.execute("SELECT presence FROM run_query_roots WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("Missing explicit Run query root")
    return present(row[0], FIELDS)


def _record(c: sqlite3.Connection, run_id: str, table: str, identity: str, schema: dict[str, str]) -> dict[str, Any]:
    columns = ["presence", *schema]
    require_record_size(c, table, columns, identity + "=?", (run_id,))
    row = c.execute(
        "SELECT " + ",".join(columns) + " FROM " + table + " WHERE " + identity + "=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Missing Run " + table + " record")
    included = present(row[0], schema)
    result = {}
    for key, value in zip(schema, row[1:], strict=True):
        if key not in included:
            if value is not None:
                raise ValueError("Unexpected Run record column")
            continue
        if schema[key] == "BOOLEAN":
            if value is not None and (type(value) is not int or value not in (0, 1)):
                raise ValueError("Invalid Run record boolean")
            result[key] = None if value is None else bool(value)
        else:
            result[key] = _typed(value, schema[key], key)
    return result


def load(
    c: sqlite3.Connection,
    run_id: str,
    *,
    fields: set[str] | None = None,
    thin_timeline: bool = False,
    current_only: bool = False,
) -> tuple[dict[str, Any], dict[str, timeline.TimelineScope]]:
    keys = _keys(c, run_id)
    selected = keys if fields is None else keys & fields
    result: dict[str, Any] = {}
    scopes: dict[str, timeline.TimelineScope] = {}
    if "notifications" in selected:
        result["notifications"] = _record(c, run_id, "run_notification_config", "run_id", _NOTIFICATION)
    if "action_application_receipt" in selected:
        result["action_application_receipt"] = _record(c, run_id, "run_application_receipt", "owner_run_id", _RECEIPT)
    if "timeline_at_capacity" in selected:
        value = c.execute("SELECT timeline_at_capacity FROM run_query_roots WHERE run_id=?", (run_id,)).fetchone()[0]
        if type(value) is not int or value not in (0, 1):
            raise ValueError("Invalid Timeline capacity flag")
        result["timeline_at_capacity"] = bool(value)
    for field in selected & timeline.FIELDS:
        if thin_timeline:
            scopes[field] = timeline.scope(c, run_id, field)
        else:
            result[field] = timeline.load(c, run_id, field)
    if selected & {"resume_audit", "human_response_audit"}:
        audit = resume.load(c, run_id, current_only=current_only, include_responses="human_response_audit" in selected)
        result.update((key, value) for key, value in audit.items() if key in selected)
    return result, scopes


def load_current(c: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    result, _ = load(c, run_id, fields=set(FIELDS - timeline.FIELDS), current_only=True)
    return result


def load_resume_events(
    c: sqlite3.Connection,
    run_id: str,
    *,
    attempt_ids: set[str] | None = None,
    resume_ids: set[str] | None = None,
    include_responses: bool = False,
) -> dict[str, Any]:
    return resume.load(c, run_id, attempt_ids=attempt_ids, resume_ids=resume_ids, include_responses=include_responses)


def _save_record(c: sqlite3.Connection, run_id: str, table: str, schema: dict[str, str], value: Any) -> None:
    if not isinstance(value, Mapping) or value.keys() - schema.keys():
        raise ValueError("Unsupported " + table + " fields")
    values = tuple(_typed(value.get(key), kind, key) for key, kind in schema.items())
    c.execute(
        "INSERT OR REPLACE INTO " + table + " VALUES (" + ",".join("?" for _ in range(len(values) + 2)) + ")",
        (run_id, presence(value, schema), *values),
    )
    identity = "owner_run_id" if table == "run_application_receipt" else "run_id"
    require_record_size(c, table, ["presence", *schema], identity + "=?", (run_id,))


def save(
    c: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    fields: set[str],
    timeline_updates: Mapping[str, dict[str, Any]] | None = None,
) -> None:
    c.execute("INSERT OR IGNORE INTO run_query_roots VALUES (?,?,NULL)", (run_id, presence((), FIELDS)))
    keys = _keys(c, run_id)
    for field in fields & FIELDS:
        was_present = field in keys
        if field in state:
            keys.add(field)
        elif field not in (timeline_updates or {}):
            keys.discard(field)
        if field == "notifications":
            if field in state:
                _save_record(c, run_id, "run_notification_config", _NOTIFICATION, state[field])
            else:
                c.execute("DELETE FROM run_notification_config WHERE run_id=?", (run_id,))
        elif field == "action_application_receipt":
            if field in state:
                _save_record(c, run_id, "run_application_receipt", _RECEIPT, state[field])
            else:
                raise ValueError("Cannot remove the Run application receipt")
        elif field == "timeline_at_capacity":
            value = state.get(field)
            c.execute(
                "UPDATE run_query_roots SET timeline_at_capacity=? WHERE run_id=?",
                (_typed(value, "BOOLEAN", field), run_id),
            )
        elif field in timeline.FIELDS:
            if timeline_updates is not None and field in timeline_updates:
                timeline.append(c, run_id, field, [timeline_updates[field]])
                keys.add(field)
            elif field in state:
                timeline.save(c, run_id, field, state[field])
            elif field in baseline:
                timeline.save(c, run_id, field, [])
            elif was_present:
                raise ValueError("Cannot remove an unread Timeline")
    resume.save(c, run_id, state, baseline, fields=fields)
    c.execute("UPDATE run_query_roots SET presence=? WHERE run_id=?", (presence(keys, FIELDS), run_id))
