"""Read execution totals from typed facts without opening historical reports."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent_run.execution_timing import execution_totals, parse_optional_timestamp
from agent_run.history_facts import _ensure_history_record
from agent_run.sqlite_query_rows import bounded_query, bounded_rows
from agent_run.sqlite_work_execution_details import restore_details
from agent_run.sqlite_work_execution_schema import INVOCATION_FIELDS, present_fields
from agent_run.state_limits import (
    MAX_BATCH_BYTES, MAX_CURRENT_BYTES, QUERY_BATCH_ROWS,
    consume_query_bytes, consume_query_rows,
)
from agent_run.ticket_effort import ticket_effort

_SCALARS = (
    "status", "reported_thread_id", "ended_at", "execution_not_started",
    "predecessor_invocation_started_at", "model", "reasoning_effort",
)
_DETAILS = ("execution_segments_null", "timing_presence", "timing_execution_seconds",
            "timing_recorded_execution_seconds", "timing_execution_timing_complete")
_COLUMNS = ("presence", *_SCALARS, "attempt_id", "attempt_version_id", *_DETAILS)
_ATTEMPT_COLUMNS = ("attempt_id", "work_subject", "generation", "role", "ordinal", "budget_window")
_OWNER_CONTAINERS = ("ticket_jobs", "parent_job", "run_acceptance", "run_publication",
                     "retired_semantic_attempt_owners")


@dataclass
class TimingFacts:
    state: dict[str, Any]
    records: list[dict[str, Any]]

    def totals(self, at: object = None, *, work_subject: str | None = None) -> dict[str, Any]:
        return execution_totals(self.state, self.records, at, work_subject=work_subject)

    def effort(self, work_subject: str) -> dict[str, dict[str, Any]]:
        return ticket_effort(self.state, self.records, work_subject)


def _visible_owners(c: sqlite3.Connection, run_id: str) -> set[str]:
    owners = {row[0] for row in bounded_rows(c,
        "SELECT job_id FROM work_job_slots WHERE run_id=? AND container IN ("
        + ",".join("?" for _ in _OWNER_CONTAINERS) + ")", (run_id, *_OWNER_CONTAINERS),
        ("job_id",),
    )}
    selected = next(bounded_rows(c, "SELECT active_job_id FROM work_job_layout WHERE run_id=?", (run_id,),
                                 ("active_job_id",)), None)
    if selected is not None and selected[0] is not None:
        owners.add(selected[0])
    for identity in list(owners):
        row = next(bounded_rows(c, "SELECT repair_job_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity),
                                ("repair_job_id",)), None)
        if row is not None and row[0] is not None:
            owners.add(row[0])
    return owners


def _attempts(c: sqlite3.Connection, run_id: str, work_subject: str | None) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for owner in sorted(_visible_owners(c, run_id)):
        rows = bounded_rows(c,
            "SELECT " + ",".join("a." + key for key in _ATTEMPT_COLUMNS)
            + " FROM work_job_attempt_history h JOIN work_attempts a USING(run_id,attempt_id) "
            "WHERE h.run_id=? AND h.job_id=?" + (" AND a.work_subject=?" if work_subject else "")
            + " ORDER BY h.position", (run_id, owner, work_subject) if work_subject else (run_id, owner),
            _ATTEMPT_COLUMNS,
        )
        for row in rows:
            value = dict(zip(_ATTEMPT_COLUMNS, row, strict=True))
            result[value["attempt_id"]] = value
    return list(result.values())


def _invocations(c: sqlite3.Connection, run_id: str, work_subject: str | None) -> list[dict[str, Any]]:
    # Transfer only numeric sizes before a potentially damaged scalar body.
    size_sql = "+".join(f"COALESCE(LENGTH(CAST(v.{key} AS BLOB)),0)+16" for key in ("work_subject", "started_at", *_COLUMNS))
    cursor = bounded_query(c,
        "SELECT h.position," + size_sql + " FROM work_invocation_history h "
        "JOIN work_invocations v USING(run_id,work_subject,started_at) WHERE h.run_id=?"
        + (" AND h.work_subject=?" if work_subject else "") + " ORDER BY h.position",
        (run_id, work_subject) if work_subject else (run_id,),
    )
    result = []
    for position, size in cursor:
        if type(size) is not int or size > MAX_CURRENT_BYTES:
            raise ValueError("Invocation scalar record exceeds byte limit")
        consume_query_rows()
        consume_query_bytes(size)
        row = c.execute(
            "SELECT h.work_subject,h.started_at," + ",".join("v." + key for key in _COLUMNS)
            + " FROM work_invocation_history h JOIN work_invocations v USING(run_id,work_subject,started_at) "
            "WHERE h.run_id=? AND h.position=?", (run_id, position),
        ).fetchone()
        if row is None:
            raise ValueError("Missing Invocation timing facts")
        subject, started = row[:2]
        stored = dict(zip(_COLUMNS, row[2:], strict=True))
        included = present_fields(stored["presence"], INVOCATION_FIELDS)
        value = {"work_subject": subject, "started_at": started}
        value.update((key, bool(stored[key]) if key == "execution_not_started" and stored[key] is not None else stored[key])
                     for key in _SCALARS if key in included)
        if stored["attempt_id"] is not None:
            attempt = next(bounded_rows(c,
                "SELECT " + ",".join(_ATTEMPT_COLUMNS) + " FROM work_attempts WHERE run_id=? AND attempt_id=?",
                (run_id, stored["attempt_id"]), _ATTEMPT_COLUMNS,
            ), None)
            if attempt is None:
                raise ValueError("Missing Invocation Attempt identity")
            value["semantic_attempt"] = dict(zip(_ATTEMPT_COLUMNS, attempt, strict=True))
        details = bounded_query(c,
            "SELECT COUNT(*),COALESCE(SUM(COALESCE(LENGTH(CAST(started AS BLOB)),0)+"
            "COALESCE(LENGTH(CAST(ended AS BLOB)),0)+COALESCE(LENGTH(CAST(presence AS BLOB)),0)+48),0) "
            "FROM work_invocation_segments WHERE run_id=? AND work_subject=? AND started_at=?",
            (run_id, subject, started),
        ).fetchone()
        if details[1] > MAX_BATCH_BYTES:
            raise ValueError("Invocation timing segments exceed query batch byte limit")
        restore_details(c, (run_id, subject, started), stored, value,
                        included & {"execution_segments", "execution_timing_summary"})
        result.append(value)
    return result


def load(c: sqlite3.Connection, run_id: str, current: dict[str, Any], *, work_subject: str | None = None) -> TimingFacts:
    from agent_run.sqlite_run_audit import load_resume_events
    from agent_run.semantic_attempt import semantic_attempt_subjects

    invocations = _invocations(c, run_id, work_subject)
    records: dict[str, dict[str, Any]] = {}
    for attempt in _attempts(c, run_id, work_subject):
        _ensure_history_record(records, attempt=attempt)
    for owner in semantic_attempt_subjects(current):
        pending = owner.get("pending_semantic_attempt")
        if isinstance(pending, dict) and (work_subject is None or pending.get("work_subject") == work_subject):
            _ensure_history_record(records, attempt=pending)
    for invocation in invocations:
        _ensure_history_record(records, attempt=invocation.get("semantic_attempt") or {}, invocation=invocation)["invocations"].append(invocation)
    state = {**current, "agent_invocation_history": invocations}
    attempts = sorted({record["attempt_id"] for record in records.values() if record.get("attempt_id")})
    resumes: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(attempts), QUERY_BATCH_ROWS):
        selected = load_resume_events(c, run_id, attempt_ids=set(attempts[offset:offset + QUERY_BATCH_ROWS]))
        resumes.update((event["resume_id"], event) for event in selected.get("resume_audit", {}).get("history", []))
    state["resume_audit"] = {**current.get("resume_audit", {}),
                             "history": sorted(resumes.values(), key=lambda event: event.get("sequence", 0))}
    for record in records.values():
        raw = record["invocations"]
        starts = [parsed for item in raw if (parsed := parse_optional_timestamp(item.get("started_at"))) is not None]
        ends = [parsed for item in raw if (parsed := parse_optional_timestamp(item.get("ended_at"))) is not None]
        record.update(started_at=min(starts).isoformat() if starts else None,
                      ended_at=max(ends).isoformat() if ends and all(item.get("ended_at") is not None for item in raw) else None,
                      activity="not_running")
    ordered = sorted(records.values(), key=lambda item: (
        parse_optional_timestamp(item.get("started_at")) or parse_optional_timestamp(item.get("ended_at")) or datetime.max.replace(tzinfo=UTC),
        str(item.get("work_subject") or ""), str(item.get("role") or ""), item.get("ordinal") or 0,
    ))
    return TimingFacts(state, ordered)
