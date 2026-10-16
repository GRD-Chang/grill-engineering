"""Explicit current query; historical reports are outside this read contract."""
from __future__ import annotations

import sqlite3
from typing import Any

from agent_run import sqlite_change_jobs as jobs
from agent_run import sqlite_run_records as roots
from agent_run import sqlite_work_execution as execution
from agent_run.sqlite_encoding import object_path
from agent_run.sqlite_work_execution_invocations import load_invocation
from agent_run.sqlite_query_rows import bounded_rows
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.work_projection import JobProjection


def load(c: sqlite3.Connection, run_id: str, *, include_timing: bool = True) -> dict[str, Any]:
    from agent_run import sqlite_run_audit as audit, sqlite_timeline

    state = roots.load(c, run_id)
    state.update(jobs.load_jobs(c, run_id, current_only=True))
    for current_owner in semantic_attempt_subjects(state):
        if not isinstance(current_owner, JobProjection) or "reviewer_thread_ids" not in current_owner.archives:
            continue
        row = next(bounded_rows(c,
            "SELECT position,thread_id FROM work_job_archives WHERE run_id=? AND job_id=? "
            "AND history_kind='reviewer_thread_ids' ORDER BY position DESC LIMIT 1", (run_id, current_owner.job_id),
            ("position", "thread_id"),
        ), None)
        count = current_owner.archives["reviewer_thread_ids"][1]
        if (row is None and count) or (row is not None and (row[0] != count - 1 or not isinstance(row[1], str))):
            raise ValueError("Invalid latest Reviewer Thread identity")
        current_owner["_query_latest_reviewer_thread"] = row[1] if row is not None else None
    state.update(audit.load_current(c, run_id))
    resumes = {event["resume_id"] for event in state.get("resume_audit", {}).get("history", [])}
    current, positions = execution.load_invocations(c, run_id, current_only=True, history=True, resume_ids=resumes)
    state.update(current)
    latest = next(bounded_rows(c,
        "SELECT work_subject,started_at,position FROM work_invocation_history WHERE run_id=? ORDER BY position DESC LIMIT 1",
        (run_id,), ("work_subject", "started_at", "position"),
    ), None)
    if latest is not None:
        values = {(item["work_subject"], item["started_at"]): item for item in state.get("agent_invocation_history", [])}
        key = latest[0], latest[1]
        if key not in values:
            values[key] = load_invocation(c, run_id, key)
        from agent_run.sqlite_encoding import encode_json

        positions[encode_json(key)] = latest[2]
        state["agent_invocation_history"] = sorted(values.values(), key=lambda item: positions[encode_json((item["work_subject"], item["started_at"]))])
    # Current publication retry identity may belong to a closed exact Attempt.
    # Read that identity, rather than every preceding publication report.
    archives = jobs.read_job_archives(c, run_id, state)
    for encoded, attempt in jobs.read_related_attempts(c, run_id, state, archives).items():
        owner: Any = state
        for part in object_path(encoded):
            owner = owner[part]
        owner["semantic_attempt_history"] = [attempt]
    for field in ("timeline", "timeline_continuation"):
        tail = sqlite_timeline.load_tail(c, run_id, field=field, limit=1)
        if tail:
            state[field] = tail
    if include_timing:
        from agent_run.sqlite_query_timing import load as timing_facts

        facts = timing_facts(c, run_id, state)
        selected: dict[str, Any] = state.get("active_agent_invocation") or next(reversed(state.get("agent_invocation_history", [])), {})
        identity = (selected.get("semantic_attempt") or {}).get("attempt_id")
        records = [record for record in facts.records if record.get("attempt_id") == identity] if identity else []
        state["_query_timing"] = {
            "totals": facts.totals(), "records": records,
            "resumes": [event for event in facts.state.get("resume_audit", {}).get("history", [])
                        if event.get("semantic_attempt_id") == identity],
        }
    return state
