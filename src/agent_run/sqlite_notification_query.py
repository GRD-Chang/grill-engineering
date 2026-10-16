"""Read current boundaries and the notification journal's exact delivery scope."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from agent_run.notification_events import events
from agent_run.attempt_evidence import _subject_variants
from agent_run.notification_outbox import MAX_PENDING
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.sqlite_notification_milestones import formal_attempt, had_repair, starts
from agent_run.sqlite_query_rows import bounded_rows
from agent_run.sqlite_status_query import load as current_query
from agent_run.sqlite_work_evidence import get_evidence
from agent_run.sqlite_work_execution_attempts import load_version
from agent_run.sqlite_work_execution_invocations import load_invocation
from agent_run.state_limits import QUERY_BATCH_ROWS
from agent_run.work_projection import JobProjection


def _executor_activity(c: sqlite3.Connection, state: dict[str, Any]) -> str:
    """Prove this notification reader is the current handshaken Executor.

    This ephemeral observation grants no write authority. Readers in another
    process still need the separate Executor Host audit to establish liveness.
    """
    from agent_run.execution_timing import parse_optional_timestamp
    from agent_run.executor_host import _process_start_token
    from agent_run.task_control import TASK_CONTROL_PROTOCOL
    from agent_run.task_identity import TaskKey

    receipt = state.get("action_application_receipt")
    parent = (state.get("parent") or {}).get("number")
    repository = state.get("repository")
    if (not isinstance(receipt, dict) or receipt.get("protocol") != TASK_CONTROL_PROTOCOL
            or receipt.get("run_id") != state["run_id"]
            or receipt.get("kind") not in {"run", "resume", "requeue", "approve", "revise"}
            or not isinstance(receipt.get("action_id"), str) or not receipt["action_id"]
            or not isinstance(receipt.get("payload_digest"), str) or not receipt["payload_digest"]
            or type(receipt.get("executor_generation")) is not int or receipt["executor_generation"] <= 0
            or type(parent) is not int or parent <= 0 or not isinstance(repository, str)):
        return "unknown"
    rows = list(bounded_rows(c,
        "SELECT t.task_id,t.workspace,t.run_state_dir,e.process_start_token,e.handshake_at "
        "FROM control_actions a JOIN control_tasks t ON t.task_id=a.task_id "
        "JOIN control_executors e ON e.task_id=t.task_id AND e.action_id=t.current_executor_action_id "
        "AND e.generation=t.current_executor_generation "
        "WHERE a.action_id=? AND a.kind=? AND a.payload_digest=? AND a.run_id=? AND a.executor_generation=? "
        "AND a.status IN ('accepted','applying','completed') AND a.application_observed=1 "
        "AND t.protocol=? AND LOWER(t.repository)=? AND t.parent_number=? AND t.run_id=a.run_id "
        "AND t.current_action_id=a.action_id AND t.current_executor_action_id=a.action_id "
        "AND t.current_executor_generation=a.executor_generation "
        "AND e.status='running' AND e.run_id=t.run_id AND e.pid=? AND e.exited_at IS NULL LIMIT 2",
        (receipt["action_id"], receipt["kind"], receipt["payload_digest"], state["run_id"],
         receipt["executor_generation"], TASK_CONTROL_PROTOCOL, repository.lower(), parent, os.getpid()),
        ("task_id", "workspace", "run_state_dir", "process_start_token", "handshake_at"),
    ))
    if len(rows) != 1:
        return "unknown"
    task_id, workspace, directory, token, handshake = rows[0]
    if (not isinstance(workspace, str) or not Path(workspace).is_absolute()
            or (directory is not None and (not isinstance(directory, str) or not Path(directory).is_absolute()))
            or not isinstance(token, str) or not token or token != _process_start_token(os.getpid())
            or parse_optional_timestamp(handshake) is None
            or TaskKey(Path(workspace), repository, parent).fingerprint != task_id):
        return "unknown"
    database = next((row[2] for row in c.execute("PRAGMA database_list") if row[1] == "main"), None)
    if not isinstance(database, str) or not database:
        return "unknown"
    # Task Control permits an unpinned directory. Its rows are already in this
    # Run's database; an explicit directory must name that same physical root.
    same_directory = directory is None or Path(database).parent.resolve() == Path(directory).resolve()
    return "running" if same_directory else "unknown"


def _select_attempt_owners(c: sqlite3.Connection, run_id: str, state: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    """Prove selected Attempt membership without inventing an empty archive."""
    selected = {attempt["attempt_id"] for attempt in attempts}
    by_subject: dict[str, list[str]] = {}
    for attempt in attempts:
        by_subject.setdefault(attempt["work_subject"], []).append(attempt["attempt_id"])
    owners: dict[str, tuple[list[JobProjection], set[str]]] = {}
    for subject, owner in _subject_variants(state):
        if not isinstance(owner, JobProjection):
            continue
        related, subjects = owners.setdefault(owner.job_id, ([], set()))
        if not any(owner is value for value in related):
            related.append(owner)
        subjects.add(subject)
    for job_id, (related, subjects) in owners.items():
        known: set[str] = set()
        pending = related[0].get("pending_semantic_attempt")
        if isinstance(pending, dict) and pending.get("attempt_id") in selected:
            known.add(pending["attempt_id"])
        identities = sorted({identity for subject in subjects for identity in by_subject.get(subject, [])} - known)
        for offset in range(0, len(identities), QUERY_BATCH_ROWS):
            batch = identities[offset:offset + QUERY_BATCH_ROWS]
            known.update(row[0] for row in bounded_rows(c,
                "SELECT attempt_id FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id IN ("
                + ",".join("?" for _ in batch) + ")", (run_id, job_id, *batch), ("attempt_id",),
            ))
        for owner in related:
            owner["_query_attempt_ids"] = sorted(known)


def _attempt(c: sqlite3.Connection, run_id: str, attempt_id: str) -> dict[str, Any]:
    row = next(bounded_rows(c, "SELECT version_id FROM work_attempt_current WHERE run_id=? AND attempt_id=?",
                            (run_id, attempt_id), ("version_id",)), None)
    if row is None:
        raise ValueError("Missing notification Semantic Attempt")
    value = load_version(c, run_id, attempt_id, row[0])
    if value.get("role") == "reviewer":
        returned = next(bounded_rows(c,
            "SELECT m.evidence_id FROM work_attempt_result_members m JOIN work_attempt_returns r "
            "USING(run_id,attempt_id,evidence_id) WHERE m.run_id=? AND m.attempt_id=? "
            "AND m.result_field='acceptance_artifact' ORDER BY r.sequence DESC LIMIT 1", (run_id, attempt_id),
            ("evidence_id",),
        ), None)
        if returned is not None:
            raw = get_evidence(c, run_id, returned[0], owner_id="attempt:" + attempt_id, kind="returned_result")
            if not isinstance(raw, dict) or raw.get("kind") != "review" or not isinstance(raw.get("artifact"), dict):
                raise ValueError("Notification review has an invalid original result")
            value = {**value, "acceptance_artifact": raw["artifact"]}
    return value


def _first_failed_review(
    c: sqlite3.Connection, run_id: str, selected: list[dict[str, Any]], prior: dict[str, Any],
) -> tuple[str | None, str | None]:
    known, version = prior.get("first_failed_review"), prior.get("first_failed_version")
    if isinstance(known, str):
        row = next(bounded_rows(c, "SELECT version_id FROM work_attempt_current WHERE run_id=? AND attempt_id=?",
                                (run_id, known), ("version_id",)), None)
        if row is not None and row[0] == version:
            return known, version
    failures = [attempt for attempt in selected if attempt.get("work_subject") == f"run-acceptance:{run_id}"
                and any(check.get("status") == "fail" for check in (attempt.get("acceptance_artifact", {}).get("checks") or {}).values()
                        if isinstance(check, dict))]
    if not failures:
        return None, None
    # A cold sender with a failed recent review must distinguish the first
    # overall failure from later failures. Open only this necessary review
    # evidence; ordinary saves use the immutable version receipt above.
    for identity, stored_version in bounded_rows(c,
        "SELECT a.attempt_id,p.version_id FROM work_attempts a JOIN work_attempt_current p USING(run_id,attempt_id) "
        "WHERE a.run_id=? AND a.work_subject=? AND a.role='reviewer' "
        "ORDER BY a.generation,a.budget_window,a.ordinal", (run_id, f"run-acceptance:{run_id}"),
        ("attempt_id", "version_id"),
    ):
        attempt = next((item for item in selected if item["attempt_id"] == identity), None) or _attempt(c, run_id, identity)
        if any(check.get("status") == "fail" for check in (attempt.get("acceptance_artifact", {}).get("checks") or {}).values()
               if isinstance(check, dict)):
            return identity, stored_version
    return None, None


def _card_facts(c: sqlite3.Connection, run_id: str, state: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    from agent_run.execution_timing import record_execution_timing
    from agent_run.sqlite_query_timing import load as timing_facts

    projected = events(state)
    boundaries = [event for event in projected if event.get("current") and event["id"] not in document.get("active", [])]
    completions = [event for event in projected if event["kind"] == "ticket_completed"]
    formal = state.get("_notification_formal_attempt")
    if not boundaries and not completions and not formal:
        return {}
    # New boundaries need Run totals; Ticket completion needs only its subject.
    subjects = {f"ticket:{event['task_number']}" for event in completions}
    subject = next(iter(subjects)) if len(subjects) == 1 and not boundaries and not formal else None
    facts = timing_facts(c, run_id, state, work_subject=subject)
    result = {}
    for event in completions:
        work = f"ticket:{event['task_number']}"
        result[event["id"]] = {**facts.totals(work_subject=work), "effort": facts.effort(work)}
    for event in boundaries:
        at = None
        if state.get("status") in {"completed", "abandoned"}:
            timeline = [*state.get("timeline", []), *state.get("timeline_continuation", [])]
            at = next((point.get("at") for point in reversed(timeline) if point.get("status") == state["status"]), None)
        result[event["id"]] = facts.totals(at)
    if formal:
        record = next((record for record in facts.records if record.get("attempt_id") == formal), None)
        if record is not None:
            timing = record_execution_timing(facts.state, record)
            for event in projected:
                if event["kind"] == "acceptance_passed":
                    result[event["id"]] = {
                        "attempt_id": formal, "round": record.get("ordinal"),
                        "phase": event.get("phase"),
                        "duration_seconds": timing.get("execution_seconds") if timing["execution_timing_complete"] else None,
                        "execution_timing_complete": timing["execution_timing_complete"],
                        "recorded_execution_seconds": timing.get("recorded_execution_seconds"),
                    }
    return result


def load(c: sqlite3.Connection, run_id: str, observation: dict[str, Any]) -> dict[str, Any]:
    from agent_run import sqlite_run_audit as audit, sqlite_timeline

    state = current_query(c, run_id, include_timing=False)
    state["_executor_control"] = {"activity": _executor_activity(c, state)}
    prior = observation.get("observation", {})
    if not isinstance(prior, dict):
        raise ValueError("Invalid notification observation receipt")
    latest = c.execute("SELECT COALESCE(MAX(position),-1) FROM work_invocation_history WHERE run_id=?", (run_id,)).fetchone()[0]
    cursor = prior.get("history_position")
    if cursor is not None and (type(cursor) is not int or cursor < -1 or cursor > latest):
        raise ValueError("Notification observation is outside this Run history")
    window = list(bounded_rows(c,
        "SELECT work_subject,started_at,position FROM work_invocation_history WHERE run_id=? "
        + ("ORDER BY position DESC LIMIT ?" if cursor is None else "AND position>? ORDER BY position"),
        (run_id, MAX_PENDING) if cursor is None else (run_id, cursor),
        ("work_subject", "started_at", "position"),
    ))
    open_attempts = prior.get("open_attempts", [])
    if not isinstance(open_attempts, list) or any(not isinstance(value, str) for value in open_attempts):
        raise ValueError("Invalid notification open Attempt receipt")
    selected = set(open_attempts)
    for owner in semantic_attempt_subjects(state):
        if isinstance((pending := owner.get("pending_semantic_attempt")), dict):
            selected.add(pending["attempt_id"])
    current_invocations = [state.get("active_agent_invocation"), *(state.get("ticket_agent_invocations") or {}).values()]
    for invocation in current_invocations:
        if isinstance(invocation, dict) and invocation.get("status") in {"running", "resuming", "failed"}:
            if isinstance((attempt := invocation.get("semantic_attempt")), dict):
                selected.add(attempt["attempt_id"])
    pending_events = [item["event"] for item in observation.get("pending", [])]
    selected.update(event["attempt_id"] for event in pending_events if isinstance(event.get("attempt_id"), str))
    values = {(item["work_subject"], item["started_at"]): item for item in state.get("agent_invocation_history", [])}
    positions = {}
    for subject, started, position in window:
        key = subject, started
        if key not in values:
            values[key] = load_invocation(c, run_id, key)
        positions[key] = position
        if isinstance((attempt := values[key].get("semantic_attempt")), dict):
            selected.add(attempt["attempt_id"])
    state["_notification_starts"] = starts(c, run_id, state, observation)
    state["_notification_had_repair"] = had_repair(c, run_id)
    formal = formal_attempt(c, run_id, state, observation)
    if formal:
        selected.add(formal)
        state["_notification_formal_attempt"] = formal
    for identity in sorted(selected):
        for subject, started, position in bounded_rows(c,
            "SELECT v.work_subject,v.started_at,h.position FROM work_invocations v INDEXED BY work_invocation_attempt "
            "JOIN work_invocation_history h USING(run_id,work_subject,started_at) WHERE v.run_id=? AND v.attempt_id=? ORDER BY h.position",
            (run_id, identity), ("work_subject", "started_at", "position"),
        ):
            key = subject, started
            if key not in values:
                values[key] = load_invocation(c, run_id, key)
            positions[key] = position
    attempts = [_attempt(c, run_id, identity) for identity in sorted(selected)]
    _select_attempt_owners(c, run_id, state, attempts)
    state["semantic_agent_attempts"] = attempts
    state["agent_invocation_history"] = [values[key] for key in sorted(positions, key=lambda key: positions[key])]
    resumed = audit.load_resume_events(c, run_id, attempt_ids=selected,
                                      resume_ids={event["resume_id"] for event in pending_events if isinstance(event.get("resume_id"), str)},
                                      include_responses=True)
    state.update(resumed)
    for field in ("timeline", "timeline_continuation"):
        tail = sqlite_timeline.load_tail(c, run_id, field=field, limit=MAX_PENDING)
        if tail:
            state[field] = tail
    completed = {}
    already_completed = prior.get("completed", {})
    if not isinstance(already_completed, dict):
        raise ValueError("Invalid notification completion receipt")
    state["_notification_completed"] = []
    from agent_run.notification_event_facts import event_envelope

    for number, job in state.get("ticket_jobs", {}).items():
        if job.get("phase") == "completed":
            identity = event_envelope(state, "ticket_completed", [number, job.get("generation")], "")["id"]
            completed[number] = identity
            if already_completed.get(number) == identity:
                state["_notification_completed"].append(number)
    failed, version = _first_failed_review(c, run_id, attempts, prior)
    state["_notification_first_failed_review"] = failed
    state["_notification_scope"] = {
        "attempt_ids": sorted(selected), "resume_ids": [event["resume_id"] for event in state.get("resume_audit", {}).get("history", [])],
        "receipt": {"history_position": latest,
                    "open_attempts": sorted(attempt["attempt_id"] for attempt in attempts if attempt.get("status") == "pending"),
                    "completed": completed, "first_failed_review": failed, "first_failed_version": version},
    }
    state["_notification_card_facts"] = _card_facts(c, run_id, state, observation)
    return state
