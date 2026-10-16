"""明确 Timeline 事件和有界流；只读范围不再依赖路径节点或快照证明。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from copy import deepcopy
import sqlite3
from typing import Any

from agent_run.history_check_facts import CHECKS_HISTORY_FIELDS
from agent_run.sqlite_change_job_codec import presence, present, _typed
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_work_evidence import get_evidence, put_evidence
from agent_run.state_limits import consume_query_rows, require_record_size

FIELDS = frozenset({"timeline", "timeline_continuation"})
INTEGER_FIELDS = frozenset(
    {
        "ticket",
        "attempt",
        "pr_number",
        "semantic_attempt_ordinal",
        "budget_window",
        "output_attempt",
        "publication_operation_retry_attempts",
        "publication_operation_retry_limit",
        "explicit_resume_sequence",
        "history_generation",
        "history_budget_window",
    }
)
EVIDENCE_FIELDS = frozenset(
    {"human_blockers", "graph_change_summary", "required_checks_evidence", "external_wait_reason"}
)
TEXT_FIELDS = (
    frozenset(
        {
            "at",
            "kind",
            "status",
            "worker",
            "thread_id",
            "phase",
            "commit_sha",
            "approval_granted_at",
            "required_checks_result",
            "required_checks_observed_at",
            "accepted_graph_revision",
            "observed_graph_revision",
            "next_action",
            "semantic_attempt_id",
            "semantic_attempt_role",
            "agent_invocation_started_at",
            "agent_invocation_status",
            "explicit_resume_kind",
            "explicit_resume_thread_id",
            "explicit_resume_attempt_id",
            "result",
            *CHECKS_HISTORY_FIELDS,
        }
    )
    - INTEGER_FIELDS
    - EVIDENCE_FIELDS
)
EVENT_FIELDS = TEXT_FIELDS | INTEGER_FIELDS | EVIDENCE_FIELDS
_COLUMNS = sorted(EVENT_FIELDS)
TABLES = {
    "run_timeline_events": "CREATE TABLE run_timeline_events (run_id TEXT NOT NULL, event_id TEXT NOT NULL, presence BLOB NOT NULL, "
    + ", ".join("value_" + key + (" INTEGER" if key in INTEGER_FIELDS else " TEXT") for key in _COLUMNS)
    + ", PRIMARY KEY(run_id,event_id), FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "run_timeline_slots": "CREATE TABLE run_timeline_slots (run_id TEXT NOT NULL, stream TEXT NOT NULL, position INTEGER NOT NULL, "
    "event_id TEXT NOT NULL, PRIMARY KEY(run_id,stream,position), "
    "FOREIGN KEY(run_id,event_id) REFERENCES run_timeline_events(run_id,event_id) ON DELETE CASCADE)",
}
INDEXES = {
    "run_timeline_event_reference": "CREATE INDEX run_timeline_event_reference ON run_timeline_slots(run_id,event_id)"
}


def limit(key: str) -> int:
    if key not in FIELDS:
        raise ValueError("Unsupported Timeline stream")
    return 256


@dataclass(frozen=True)
class TimelineScope:
    count: int
    latest: dict[str, Any] | None

    def tail(self) -> dict[str, Any] | None:
        return deepcopy(self.latest) if self.latest is not None else None


def _owner(run_id: str, event_id: str) -> str:
    return f"run:{run_id}:timeline:{event_id}"


def _read(c: sqlite3.Connection, run_id: str, event_id: str) -> dict[str, Any]:
    columns = ["presence", *("value_" + key for key in _COLUMNS)]
    require_record_size(c, "run_timeline_events", columns, "run_id=? AND event_id=?", (run_id, event_id))
    row = c.execute(
        "SELECT " + ",".join(columns) + " FROM run_timeline_events WHERE run_id=? AND event_id=?", (run_id, event_id)
    ).fetchone()
    if row is None:
        raise ValueError("Missing Timeline event")
    consume_query_rows(1)
    keys = present(row[0], EVENT_FIELDS)
    result = {}
    for key, value in zip(_COLUMNS, row[1:], strict=True):
        if key not in keys:
            if value is not None:
                raise ValueError("Unexpected Timeline event column")
            continue
        if key in EVIDENCE_FIELDS:
            result[key] = (
                None if value is None else get_evidence(c, run_id, value, owner_id=_owner(run_id, event_id), kind=key)
            )
        else:
            result[key] = _typed(value, "INTEGER" if key in INTEGER_FIELDS else "TEXT", key)
    if hashlib.sha256(encode_json(result).encode()).hexdigest() != event_id:
        raise ValueError("Immutable Timeline event changed")
    return result


def load_tail(c: sqlite3.Connection, run_id: str, field: str = "timeline", limit: int = 1) -> list[dict[str, Any]]:
    if field not in FIELDS or type(limit) is not int or not 0 <= limit <= 256:
        raise ValueError("Invalid Timeline query limit")
    rows = c.execute(
        "SELECT event_id FROM run_timeline_slots WHERE run_id=? AND stream=? ORDER BY position DESC LIMIT ?",
        (run_id, field, limit),
    ).fetchall()
    return [_read(c, run_id, event_id) for event_id, in reversed(rows)]


def load(c: sqlite3.Connection, run_id: str, field: str) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT position,event_id FROM run_timeline_slots WHERE run_id=? AND stream=? ORDER BY position LIMIT 257",
        (run_id, field),
    ).fetchall()
    if len(rows) > limit(field) or any(
        type(position) is not int or position != index for index, (position, _) in enumerate(rows)
    ):
        raise ValueError("Invalid Timeline positions or capacity")
    return [_read(c, run_id, event_id) for _, event_id in rows]


def scope(c: sqlite3.Connection, run_id: str, field: str) -> TimelineScope:
    position = c.execute(
        "SELECT MAX(position) FROM run_timeline_slots WHERE run_id=? AND stream=?", (run_id, field)
    ).fetchone()[0]
    count = 0 if position is None else position + 1
    if type(count) is not int or not 0 <= count <= limit(field):
        raise ValueError("Invalid Timeline capacity")
    tail = load_tail(c, run_id, field)
    return TimelineScope(count, tail[-1] if tail else None)


def _put(c: sqlite3.Connection, run_id: str, event: Mapping[str, Any]) -> str:
    if not isinstance(event, Mapping) or event.keys() - EVENT_FIELDS:
        raise ValueError("Unsupported Timeline event fields")
    event_id = hashlib.sha256(encode_json(event).encode()).hexdigest()
    if c.execute("SELECT 1 FROM run_timeline_events WHERE run_id=? AND event_id=?", (run_id, event_id)).fetchone():
        if _read(c, run_id, event_id) != event:
            raise ValueError("Immutable Timeline event changed")
        return event_id
    values = []
    for key in _COLUMNS:
        value = event.get(key)
        if key in EVIDENCE_FIELDS:
            value = put_evidence(c, run_id, _owner(run_id, event_id), key, value) if value is not None else None
        else:
            value = _typed(value, "INTEGER" if key in INTEGER_FIELDS else "TEXT", key)
        values.append(value)
    c.execute(
        "INSERT INTO run_timeline_events VALUES (" + ",".join("?" for _ in range(len(values) + 3)) + ")",
        (run_id, event_id, presence(event, EVENT_FIELDS), *values),
    )
    require_record_size(
        c,
        "run_timeline_events",
        ["presence", *("value_" + key for key in _COLUMNS)],
        "run_id=? AND event_id=?",
        (run_id, event_id),
    )
    return event_id


def _collect_event(c: sqlite3.Connection, run_id: str, event_id: str) -> None:
    if c.execute(
        "SELECT 1 FROM run_timeline_slots WHERE run_id=? AND event_id=? LIMIT 1", (run_id, event_id)
    ).fetchone():
        return
    owner = _owner(run_id, event_id)
    materials = [
        row[0]
        for row in c.execute(
            "SELECT material_id FROM work_evidence WHERE run_id=? AND owner_id=? AND material_id IS NOT NULL",
            (run_id, owner),
        )
    ]
    c.execute("DELETE FROM run_timeline_events WHERE run_id=? AND event_id=?", (run_id, event_id))
    c.execute(
        "DELETE FROM work_evidence WHERE run_id=? AND owner_id=? AND NOT EXISTS (SELECT 1 FROM work_evidence r WHERE r.run_id=work_evidence.run_id AND r.source_evidence_id=work_evidence.evidence_id)",
        (run_id, owner),
    )
    c.executemany(
        "DELETE FROM work_evidence_materials WHERE material_id=? AND NOT EXISTS (SELECT 1 FROM work_evidence WHERE material_id=work_evidence_materials.material_id)",
        ((identity,) for identity in materials),
    )


def append(c: sqlite3.Connection, run_id: str, field: str, events: list[dict[str, Any]]) -> None:
    count = scope(c, run_id, field).count
    for event in events:
        released = None
        if count == limit(field):
            if field != "timeline_continuation":
                raise ValueError("Timeline reached its event limit")
            released = c.execute(
                "SELECT event_id FROM run_timeline_slots WHERE run_id=? AND stream=? AND position=0", (run_id, field)
            ).fetchone()
            c.execute("DELETE FROM run_timeline_slots WHERE run_id=? AND stream=? AND position=0", (run_id, field))
            c.execute("UPDATE run_timeline_slots SET position=-position WHERE run_id=? AND stream=?", (run_id, field))
            c.execute(
                "UPDATE run_timeline_slots SET position=-position-1 WHERE run_id=? AND stream=?", (run_id, field)
            )
            count -= 1
        c.execute("INSERT INTO run_timeline_slots VALUES (?,?,?,?)", (run_id, field, count, _put(c, run_id, event)))
        count += 1
        if released is not None:
            _collect_event(c, run_id, released[0])


def save(c: sqlite3.Connection, run_id: str, field: str, value: Any) -> None:
    if not isinstance(value, list) or len(value) > limit(field):
        raise ValueError("Invalid Timeline stream or capacity")
    previous = [
        row[0]
        for row in c.execute("SELECT event_id FROM run_timeline_slots WHERE run_id=? AND stream=?", (run_id, field))
    ]
    c.execute("DELETE FROM run_timeline_slots WHERE run_id=? AND stream=?", (run_id, field))
    append(c, run_id, field, value)
    for event_id in previous:
        _collect_event(c, run_id, event_id)


def append_events(
    state: dict[str, Any],
    previous: dict[str, Any],
    scopes: dict[str, TimelineScope],
) -> dict[str, dict[str, Any]]:
    """Use the same marker and duplicate rules without expanding old events."""
    from agent_run.state import (
        MAX_TIMELINE_EVENTS,
        _timeline_change,
        _timeline_capacity_event,
        _continuation_kind,
        _event_matches_marker,
        _timeline_event,
    )

    marker, previous_marker = _timeline_change(state, previous)
    if marker is None:
        return {}
    updates = {}
    timeline = scopes["timeline"]
    if state.get("timeline_at_capacity") is not True:
        if timeline.count >= MAX_TIMELINE_EVENTS - 1:
            updates["timeline"] = _timeline_capacity_event(state)
            state["timeline_at_capacity"] = True
        elif not _event_matches_marker(timeline.tail(), marker):
            updates["timeline"] = _timeline_event(marker, previous_marker)
    if state.get("timeline_at_capacity") is True:
        continuation_marker = dict(marker)
        continuation_marker["kind"] = _continuation_kind(marker, previous_marker)
        continuation = scopes.get("timeline_continuation")
        if not _event_matches_marker(continuation.tail() if continuation else None, continuation_marker):
            event = _timeline_event(continuation_marker, previous_marker)
            if continuation is None:
                # This field did not exist in the actual baseline. Its new
                # single event is complete, so the ordinary writer handles it.
                state["timeline_continuation"] = [event]
            else:
                updates["timeline_continuation"] = event
    return updates
