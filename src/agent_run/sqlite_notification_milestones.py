"""Indexed milestone identities; old reports are not needed to prove a start."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from agent_run.execution_timing import invocation_activity, invocation_started
from agent_run.notification_event_facts import event_copy, event_envelope, event_subject
from agent_run.notification_milestones import milestones
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_query_rows import bounded_rows


def starts(c: sqlite3.Connection, run_id: str, state: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    parent_only = state.get("delivery_type") == "parent_only"
    subjects = ([f"parent-only:{run_id}"] if parent_only else
                [f"ticket:{number}" for number in (state.get("ticket_graph") or {}).get("tickets", {})])
    if not parent_only:
        subjects.append(f"run-acceptance:{run_id}")
    seen = set(document.get("milestones", []))
    result = []
    for subject in subjects:
        review = subject.startswith("run-acceptance:")
        kind = "acceptance_started" if review else "parent_started" if parent_only else "ticket_started"
        identity = run_id if review else subject
        envelope = event_envelope(state, kind, identity, "")
        if envelope["id"] in seen:
            continue
        rows = bounded_rows(c,
            "SELECT h.work_subject,h.started_at,v.reported_thread_id,v.attempt_id "
            "FROM work_invocation_history h INDEXED BY work_invocation_history_subject "
            "JOIN work_invocations v USING(run_id,work_subject,started_at) "
            "LEFT JOIN work_attempts a ON a.run_id=v.run_id AND a.attempt_id=v.attempt_id "
            "WHERE h.run_id=? AND h.work_subject=? AND TRIM(COALESCE(v.reported_thread_id,''))!='' "
            "AND COALESCE(a.role,v.role) IN (" + ("'reviewer','fresh_acceptance'" if review else "'development'")
            + ") ORDER BY h.position", (run_id, subject),
            ("work_subject", "started_at", "reported_thread_id", "attempt_id"),
        )
        first = None
        for row in rows:
            if invocation_started(dict(zip(("work_subject", "started_at", "reported_thread_id", "attempt_id"), row, strict=True))):
                first = row
                break
        if first is None:
            if review and (state.get("run_acceptance") or {}).get("acceptance_generation"):
                result.append(event_envelope(state, kind, identity, event_copy(state, kind), attempt_id=None, live=False, started_at=None))
            continue
        active = state.get("active_agent_invocation") or {}
        live = ((active.get("semantic_attempt") or {}).get("attempt_id") == first[3]
                and invocation_activity(active, {"executor_control": state.get("_executor_control")}) == "running")
        facts = {} if review else event_subject(state, subject)
        title = event_copy(state, kind) if review else event_copy(state, kind, title=facts.get("task_title") or subject)
        result.append(event_envelope(state, kind, identity, title,
                                     attempt_id=first[3], live=live, started_at=first[1], **facts))
    return result


def formal_attempt(c: sqlite3.Connection, run_id: str, state: dict[str, Any], document: dict[str, Any]) -> str | None:
    # Only the still-current formal pass needs its exact closed round duration.
    passes = [event for event in milestones(state, []) if event["kind"] == "acceptance_passed"]
    if not passes or passes[0]["id"] in document.get("seen", []):
        return None
    parent_only = state.get("delivery_type") == "parent_only"
    owner = state.get("parent_job" if parent_only else "run_acceptance") or {}
    record = owner.get("acceptance_record") or {}
    artifact = record.get("artifact")
    digest = hashlib.sha256(encode_json(artifact).encode()).hexdigest()
    row = next(bounded_rows(c,
        "SELECT a.attempt_id FROM work_attempt_result_members m INDEXED BY work_attempt_result_content "
        "JOIN work_attempts a USING(run_id,attempt_id) WHERE m.run_id=? AND m.result_field='acceptance_artifact' "
        "AND m.value_fingerprint=? AND a.work_subject=? "
        "AND (? IS NULL OR m.thread_id=?) ORDER BY a.generation DESC,a.budget_window DESC,a.ordinal DESC LIMIT 1",
        (run_id, digest, ("parent-only:" if parent_only else "run-acceptance:") + run_id,
         record.get("reviewer_thread_id"), record.get("reviewer_thread_id")),
        ("attempt_id",),
    ), None)
    return row[0] if row else None


def had_repair(c: sqlite3.Connection, run_id: str) -> bool:
    return bool(c.execute(
        "SELECT EXISTS(SELECT 1 FROM work_job_slots a JOIN work_job_slots r ON r.run_id=a.run_id "
        "AND r.container='completed_repair_jobs:'||a.job_id WHERE a.run_id=? AND a.container='run_acceptance')",
        (run_id,),
    ).fetchone()[0] == 1)
