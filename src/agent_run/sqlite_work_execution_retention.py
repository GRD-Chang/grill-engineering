"""Compact only old, terminal, unreferenced relational Invocation details."""
from __future__ import annotations

import sqlite3
from typing import Any

from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_work_execution_details import restore_details, summary_values
from agent_run.sqlite_work_execution_schema import INVOCATION_FIELDS, presence, present_fields
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS, compact_invocation_detail


def compact_details(c: sqlite3.Connection, run_id: str) -> None:
    latest = bounded_query(c,
        "SELECT MAX(position) FROM work_invocation_history WHERE run_id=?", (run_id,),
    ).fetchone()[0]
    # History positions are append-only and consecutive. Seek its last key
    # rather than visiting the retained window on every Job-only commit.
    if latest is None or latest < MAX_DETAILED_INVOCATIONS:
        return
    cutoff = latest - MAX_DETAILED_INVOCATIONS + 1
    candidates = bounded_query(c,
        "SELECT v.work_subject,v.started_at FROM work_invocations v INDEXED BY work_invocation_detail_retained "
        "CROSS JOIN work_invocation_history h USING(run_id,work_subject,started_at) "
        "WHERE v.run_id=? AND v.details_retained=1 AND v.status IN ('completed','failed') AND h.position<? "
        "AND NOT EXISTS (SELECT 1 FROM work_invocation_current r WHERE r.run_id=v.run_id "
        "AND r.work_subject=v.work_subject AND r.started_at=v.started_at) "
        "AND NOT EXISTS (SELECT 1 FROM work_job_attempt_roots p WHERE p.run_id=v.run_id "
        "AND p.pending_attempt_id=v.attempt_id)", (run_id, cutoff),
    ).fetchall()
    # These candidates have retained arrays, bounded by the recent window and
    # exact current/pending references. No archived Attempt or material is read.
    from agent_run.sqlite_work_execution_invocations import _row

    for subject, started in candidates:
        row = _row(c, run_id, (subject, started))
        if row is None:
            raise ValueError("Missing retained Invocation")
        included = present_fields(row["presence"], INVOCATION_FIELDS)
        record: dict[str, Any] = {"work_subject": subject, "started_at": started, "status": row["status"]}
        for field in ("reported_thread_id", "execution_not_started", "ended_at"):
            if field in included:
                record[field] = (bool(row[field]) if field == "execution_not_started" and row[field] is not None else row[field])
        restore_details(c, (run_id, subject, started), row, record, included)
        if not compact_invocation_detail(record):
            continue
        values = summary_values(record)
        fields = included | {key for key in record if key.endswith("_summary")}
        values["presence"] = presence({field: None for field in fields}, INVOCATION_FIELDS)
        values["details_retained"] = 0
        c.execute("UPDATE work_invocations SET " + ",".join(f"{column}=?" for column in values)
                  + " WHERE run_id=? AND work_subject=? AND started_at=?",
                  (*values.values(), run_id, subject, started))
        for table in ("work_invocation_segments", "work_invocation_waits"):
            c.execute(f"DELETE FROM {table} WHERE run_id=? AND work_subject=? AND started_at=?", (run_id, subject, started))
