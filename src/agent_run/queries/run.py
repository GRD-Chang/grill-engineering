"""Read-only public facts from the canonical store, never a writable Run."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal, Mapping, Sequence

from agent_run.contracts.task import TaskKey
from agent_run.storage.notification_outbox import read_notifications

from agent_run.queries.execution import activity, recorded_profile, worker_activity_start
from agent_run.queries.resume_facts import resume_facts
from agent_run.queries.model import HistoryPage, RunStatus
from agent_run.errors import KernelError
from agent_run.model import Role, artifact
from agent_run.storage.sqlite import QUERY_LIMIT, Storage, Transaction
from agent_run.storage.preflight import UnsupportedState, require_supported_state
from agent_run.queries.read_facts import QueryError as QueryError, all_rows as _all, material as _material, timestamp as _timestamp, read_header as _header, public_phase as _public_phase, read_boundaries
from agent_run.queries.read_facts import action_guidance, recorded_review
from agent_run.queries.read_facts import _one, _rows
from agent_run.storage.run_scope import run_statement
from agent_run.storage.routing import open_run
from agent_run.queries.current_observation import capture as capture_observation, read_current
from agent_run.core.kernel import Kernel


@dataclass(frozen=True)
class QueryDocument:
    kind: Literal["status", "history", "runs", "settings"]
    language: str
    data: Mapping[str, Any]


def _state_error(path: Path, error: Exception) -> QueryError:
    code = error.code if isinstance(error, KernelError) and error.code in {"query_limit", "storage_limit"} else "incompatible_run_state"
    return QueryError(code, f"Cannot interpret Run state at {path}: {error}. "
                      "Only the current native format is supported. Preserve the state and use a "
                      "Runner matching the authoritative database for inspection; do not rewrite its Task pointer or fences.")


def review_findings(value: Any) -> list[str]:
    """Select original human-facing evidence without translating or deciding it."""
    if not isinstance(value, Mapping):
        return []
    blockers = value.get("human_blockers")
    result = [item for item in blockers if isinstance(item, str)] if isinstance(blockers, list) else []
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        for lane in checks.values():
            if isinstance(lane, Mapping) and lane.get("status") in {"fail", "blocked"}:
                findings = lane.get("findings")
                if isinstance(findings, list):
                    result.extend(item for item in findings if isinstance(item, str))
                if lane.get("status") == "blocked" and isinstance(lane.get("evidence"), str):
                    result.append(lane["evidence"])
    return result


def header(path: Path, *, storage: Storage) -> dict[str, Any]:
    try:
        selected = open_run(path, storage=storage)
        with selected.transaction(write=False) as tx:
            return _header(tx, path)
    except UnsupportedState:
        raise
    except KernelError as error:
        raise _state_error(path, error) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise _state_error(path, error) from error








def _invocations(tx: Transaction, subject: str, attempt: str | None = None) -> list[dict[str, Any]]:
    sql, parameters = run_statement(tx, "SELECT i.*,a.role,t.role AS binding_role,t.profile_revision,t.backend_id,t.subject AS thread_subject,a.subject "
        "FROM invocations i JOIN attempts a ON a.run_id=i.run_id AND a.id=i.attempt "
        "LEFT JOIN threads t ON t.run_id=i.run_id AND t.id=i.thread WHERE i.run_id=? AND a.subject=?", (subject,))
    values = tuple(parameters)
    if attempt is not None:
        sql += " AND a.id=?"
        values += (attempt,)
    result = [dict(row) for row in tx.all(sql + " ORDER BY i.rowid", values)]
    for row in result:
        if row.pop("thread_subject") != subject:
            raise QueryError("incompatible_run_state", "Invocation has a missing or foreign Thread binding")
        profile = recorded_profile(tx, row["profile_revision"])["profiles"][row["binding_role"]]
        row.update({"model": profile["model"], "effort": profile["reasoning_effort"], "profile_reference": profile["reference"]})
        row["allocated_at"] = _timestamp(row["allocated_at"])
        outputs = _all(tx, 'SELECT * FROM invocation_outputs WHERE run_id=? AND invocation=? ORDER BY ordinal', (row['id'],))
        if not outputs:
            raise QueryError("incompatible_run_state", "Invocation has no physical output allocation")
        for output in outputs:
            for field in ("allocated_at", "dispatched_at", "started_at", "returned_at", "exited_at", "recovery_ready_at"):
                output[field] = _timestamp(output[field])
            output["raw_return"] = _material(tx, output["raw_id"], subject)
            output["failure_evidence"] = _material(tx, output["start_evidence"], subject)
            measured = activity(tx, row["id"], output["ordinal"], subject)
            output["activity"] = measured.pop("events")
            output.update(measured)
            output["process_span_seconds"] = (output["exited_at"] - output["started_at"]
                if output["started_at"] is not None and output["exited_at"] is not None
                and output["exited_at"] >= output["started_at"] else None)
            output.pop("context_id")
            output.pop("process_identity")
            output.pop("start_evidence")
        row["outputs"] = outputs
        row["elapsed_seconds"] = (sum(output["elapsed_seconds"] for output in outputs)
                                  if all(output["elapsed_seconds"] is not None for output in outputs) else None)
        row["live_status"] = "unknown"
    return result


def _attempt(tx: Transaction, record: sqlite3.Row) -> dict[str, Any]:
    """One native Attempt representation for current pointers and historical closure."""
    attempt = dict(record)
    attempt["result"] = _material(tx, attempt["result_id"], attempt["subject"])
    attempt["invocations"] = _invocations(tx, attempt["subject"], attempt["id"])
    attempt.pop("context_id")
    return attempt


def _effect_failures(tx: Transaction, subject: str | None = None, *, effect: str | None = None) -> list[dict[str, Any]]:
    sql, parameters = run_statement(tx, "SELECT f.*,e.subject,e.operation,e.status FROM effect_failures f "
        "JOIN effects e ON e.run_id=? AND e.run_id=f.run_id AND e.id=f.effect")
    values = tuple(parameters) + (() if subject is None else (subject,))
    clauses = ["e.subject=?"] if subject is not None else []
    if effect is not None:
        clauses.append("e.id=?")
        values += (effect,)
    rows = [dict(row) for row in tx.all(sql + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY f.observed_at,f.effect,f.dispatch_serial", values)]
    for row in rows:
        row["observed_at"] = _timestamp(row["observed_at"])
        row["evidence"] = _material(tx, row["evidence"], row["subject"])
    return rows


def _resync(tx: Transaction, subject: str) -> dict[str, Any]:
    allowance = _one(tx, 'SELECT * FROM resync_allowances WHERE run_id=? AND subject=? AND current=1', (subject,))
    snapshots = _rows(tx, 'SELECT * FROM integration_snapshots WHERE run_id=? AND subject=? ORDER BY window DESC LIMIT 1', (subject,))
    snapshot = dict(snapshots[0]) if snapshots else None
    return {"generation": allowance["generation"], "used": allowance["used"], "limit": allowance["allowance"],
            "window": snapshot["window"] if snapshot else None, "open": bool(snapshot and snapshot["open"]),
            "source_sha": snapshot["source_sha"] if snapshot else None, "target_sha": snapshot["target_sha"] if snapshot else None}


def _subject(tx: Transaction, run: Mapping[str, Any], job: dict[str, Any], boundary: Mapping[str, Any]) -> dict[str, Any]:
    subject = job["id"]
    budgets = _all(tx, 'SELECT * FROM budgets WHERE run_id=? AND subject=? ORDER BY purpose,window', (subject,))
    pending = None
    if job["current_attempt"]:
        pending = _attempt(tx, _one(tx, 'SELECT * FROM attempts WHERE run_id=? AND id=? AND subject=?', (job['current_attempt'], subject)))
    review = None
    if job["review_attempt"]:
        attempt = _one(tx, 'SELECT * FROM attempts WHERE run_id=? AND id=? AND subject=?', (job['review_attempt'], subject))
        review = {"attempt_id": attempt["id"], "outcome": attempt["outcome"],
                  "artifact": artifact(Role.REVIEW, tx.get(attempt["result_id"], subject)) if attempt["result_id"] else None}
    effects = _all(tx, "SELECT id,operation,status,attempts,before_sha,after_sha FROM effects WHERE run_id=? AND subject=? AND status IN ('prepared','sent','observed') ORDER BY rowid", (subject,))
    checks = None
    if job["checks_id"]:
        check = dict(_one(tx, 'SELECT * FROM checks WHERE run_id=? AND id=? AND subject=?', (job['checks_id'], subject)))
        check["evidence"] = _material(tx, check["material_id"], subject)
        checks = check
    title = _rows(tx, 'SELECT title FROM requirement_versions WHERE run_id=? AND issue=? AND revision=?', (job['issue'], job['revision']))
    repairs = _all(tx, 'SELECT * FROM run_repairs WHERE run_id=? AND subject=? ORDER BY sequence', (subject,))
    for repair in repairs:
        repair["required_checks"] = None
        if repair["checks_id"]:
            repair["required_checks"] = _material(tx,
                _one(tx, 'SELECT material_id FROM checks WHERE run_id=? AND id=? AND subject=?',
                     (repair['checks_id'], subject))[0], subject)
    return {"subject": subject, "kind": job["kind"], "issue": job["issue"],
            "title": title[0][0] if title else None, "phase": job["phase"], "purpose": job["purpose"],
            "generation": job["generation"], "revision": job["revision"],
            "candidate_sha": job["candidate_sha"], "publication_sha": job["publication_sha"],
            "integrated_sha": job["integrated_sha"], "pr_number": job["pr_number"],
            "repair_pr": next((repair for repair in repairs if repair["state"] == "open"), None),
            "repair_history": repairs,
            "ci_evidence": _material(tx, job["ci_failure"], subject),
            "gates": boundary["gates"], "budgets": budgets, "pending_attempt": pending, "review": review, "review_state": recorded_review(tx, run, job), "required_checks": checks,
            "integration_resync": _resync(tx, subject), "effect_failures": _effect_failures(tx, subject),
            "pending_writes": effects, "delivery_cleanup": boundary["delivery_cleanup"],
            "blocked_by": [row[0] for row in tx.all(*run_statement(tx,
                "SELECT s.issue FROM dependencies d JOIN subjects s ON s.run_id=d.run_id AND s.id=d.predecessor "
                "WHERE d.run_id=? AND d.subject=? AND s.phase!='complete' ORDER BY s.issue", (subject,)))]}


def _next_action(run: Mapping[str, Any], actions: list[dict[str, Any]], status: str,
                 cleanup: Mapping[str, Any] | None) -> str:
    if status == "abandonment_pending":
        return action_guidance("abandon", run_id=run["id"], repository=run["repository"], language=run["language"])
    if status == "abandoned":
        return "无"
    if actions:
        action = actions[0]
        return str(action["next_action"])
    if cleanup and cleanup["status"] == "cleanup_pending":
        return str(cleanup["items"][0]["recovery_action"])
    if status == "ticket_completed":
        return action_guidance("run", run_id=run["id"], repository=run["repository"], language=run["language"])
    if status in {"completed", "abandoned"}:
        return "无"  # Stable machine copy; human rendering localizes it.
    return action_guidance("status", run_id=run["id"], repository=run["repository"], language=run["language"])


def _control_history(tx: Transaction, *, identities: Sequence[str] | None = None) -> list[dict[str, Any]]:
    actions = (_all(tx, "SELECT * FROM actions WHERE run_id=? AND kind='resume' ORDER BY rowid DESC LIMIT 1")
               if identities is None else
               [dict(row) for identity in identities for row in _rows(tx, 'SELECT * FROM actions WHERE run_id=? AND id=?', (identity,))])
    for action in actions:
        # A Run-wide scope is not a material owner. The referenced intent keeps
        # its original owner when Final or another subject appears later.
        owner = action["subject"] or _one(tx, 'SELECT subject FROM material WHERE run_id=? AND id=?', (action['payload_id'],))[0]
        action["payload"] = _material(tx, action["payload_id"], owner)
        action["receipt"] = _material(tx, action["receipt_id"], owner)
    _resume_progress(tx, actions)
    return actions


def _resume_progress(tx: Transaction, control: list[dict[str, Any]]) -> None:
    """Display original acceptance and the shared exact-target outcome facts."""
    resumes: list[dict[str, Any]] = []
    applications = []
    for action in control:
        if action["kind"] != "resume":
            continue
        successors = _all(tx, 'SELECT id FROM invocations WHERE run_id=? AND action_id=? ORDER BY rowid', (action['id'],))
        receipt = action["receipt"]
        application = _rows(tx, "SELECT position,generation FROM history WHERE run_id=? AND kind='control_applied' AND identity=? AND material_id=? ORDER BY position LIMIT 1", (action['id'], action['receipt_id']))
        if application:
            applications.append(application[0])
        action["resume"] = {"applied": isinstance(receipt, Mapping) and receipt.get("applied") is True,
                            "successor_invocations": [row["id"] for row in successors],
                            "started_at": None, "progress": None}
        resumes.append(action)
    if not resumes or not applications:
        return
    by_action = {row["id"]: row["resume"] for row in resumes}
    # This is a candidate read, not a second Resume interpretation. Only the
    # shared reader decides whether a proof belongs to the original Action.
    # Archived status bodies/phase copy are never needed by that reader.
    sql, parameters = run_statement(tx, "SELECT run_id,position,kind,identity,subject,generation,material_id,created_at FROM history WHERE run_id=? AND position>? "
        "AND kind IN ('worker_started','worker_active','effect_consumed','checks_observed','role_consumed','paused')",
        (min(row["position"] for row in applications),))
    values = tuple(parameters)
    if len(resumes) == 1 and resumes[0]["subject"] is not None:
        sql += " AND subject=? AND generation=?"
        values += (resumes[0]["subject"], applications[0]["generation"])
    for row in tx.all(sql + " ORDER BY position", values):
        facts = resume_facts(tx, dict(row))
        if facts and facts["action_id"] in by_action:
            by_action[facts["action_id"]]["progress"] = facts
            started = worker_activity_start(tx, row["subject"], position=row["position"])
            if started is not None:
                by_action[facts["action_id"]]["started_at"] = _timestamp(started["started_at"])


def _resume_waits(run: Mapping[str, Any], subjects: list[dict[str, Any]],
                  leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name outstanding original Workers after Stop, without accepting a Resume."""
    stopped = {job["subject"]: job for job in subjects if run["stopped"]
               or any(gate["code"] == "operator_stopped" for gate in job["gates"])}
    return [{**lease, "issue": stopped[lease["subject"]]["issue"], "reason": "worker_exit_unproven"}
            for lease in leases if lease["subject"] in stopped]



def _notification_facts(tx: Transaction, run: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Project the existing journal without claiming a configuration receipt."""
    rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
    inputs = _material(tx, rows[0]["material_id"], rows[0]["subject"]) if rows else {}
    configuration = inputs.get("notifications") if isinstance(inputs, Mapping) else None
    if not isinstance(configuration, Mapping):
        configuration = None
    journal = read_notifications(path.parent, run["id"])
    reason = journal.get("unavailable_reason")
    records = journal.get("records", [])
    readable = not reason or "pending" in journal or "records" in journal
    if any(not isinstance(item, Mapping) for item in records):
        reason = "Invalid notification delivery record"
        readable = False
    result = dict(journal)
    result.update({"configuration": configuration,
                   "configuration_source": "run_snapshot" if configuration is not None else "unavailable",
                   "journal_binding": {"status": "unknown", "reason": "not_recorded"},
                   "journal_state": "unavailable" if not readable else "available" if journal else "missing",
                   "unconfirmed_count": None})
    if reason:
        result["unavailable_reason"] = reason
    if readable and journal:
        result["unconfirmed_count"] = len(journal.get("pending", [])) + sum(
            not item.get("cancelled") and bool(item.get("exhausted") or item.get("outcome") in ("failed", "unknown"))
            for item in records)
    return result


def _frozen_settings(tx: Transaction, run: Mapping[str, Any]) -> dict[str, Any]:
    from agent_run.contracts.identity import describe_identity
    from agent_run.contracts.copy import text

    profiles = recorded_profile(tx, run["profile_revision"])
    rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
    inputs = _material(tx, rows[0]["material_id"], rows[0]["subject"]) if rows else {}
    if not isinstance(inputs, dict):
        raise QueryError("incompatible_run_state", "Invalid frozen creation inputs")
    bindings = []
    for thread in _all(tx, 'SELECT * FROM threads WHERE run_id=? ORDER BY rowid'):
        profile = recorded_profile(tx, thread["profile_revision"])["profiles"][thread["role"]]
        bindings.append({"thread_identity": thread["id"], "thread_id": thread["backend_id"],
                         "role": thread["role"], "profile_revision": thread["profile_revision"],
                         "model": profile["model"], "reasoning_effort": profile["reasoning_effort"],
                         "reference": profile["reference"]})
    return {"result": "settings", "scope": "run", "run_id": run["id"], "language": run["language"],
            "repository_root": run["repository_root"], "policy": json.loads(run["policy"]),
            "github": describe_identity(inputs["worker_identity"], run["language"], inputs.get("runner_identity"))
                      if inputs.get("worker_identity") is not None else None,
            "notifications": inputs.get("notifications"), "prompt_resources": inputs.get("prompt_resources"),
            "provenance": inputs.get("provenance"),
            "profile": {"preset": profiles.get("preset"), "profiles": profiles["profiles"], "profile_revision": run["profile_revision"]},
            "source": "run_snapshot", "bindings": bindings, "message": text("settings.run_notice", language=run["language"]),
            "unavailable_facts": [key for key in ("worker_identity", "runner_identity", "notifications", "prompt_resources", "provenance")
                                  if inputs.get(key) is None]}

def _native_history(tx: Transaction, subjects: list[dict[str, Any]], *,
                    after: int, limit: int | None) -> HistoryPage:
    events = (_all(tx, 'SELECT * FROM history WHERE run_id=? AND position>? ORDER BY position', (after,)) if limit is None else
        [dict(row) for row in _rows(tx, 'SELECT * FROM history WHERE run_id=? AND position>? ORDER BY position LIMIT ?', (after, limit))])
    identities = list(dict.fromkeys(event["identity"] for event in events))
    for event in events:
        if (type(event["position"]) is not int or type(event["generation"]) is not int or event["generation"] < 1
                or not isinstance(event["kind"], str) or not isinstance(event["identity"], str)):
            raise QueryError("incompatible_run_state", "Invalid history event identity")
        event["created_at"] = _timestamp(event.get("created_at"))
        event["status"] = json.loads(event["status"]) if event["status"] is not None else None
        event["evidence"] = _material(tx, event["material_id"], event["subject"])
    identities.extend(event["identity"].rsplit(":", 1)[0] for event in events if event["kind"] == "worker_active")
    next_after = events[-1]["position"] if events else after
    has_more = bool(_rows(tx, 'SELECT position FROM history WHERE run_id=? AND position>? LIMIT 1', (next_after,)))
    attempt_ids = {row[0] for identity in identities for row in _rows(tx, 'WITH selected_run(id) AS (VALUES(?)) SELECT id FROM attempts WHERE run_id=(SELECT id FROM selected_run) AND id=? UNION SELECT attempt FROM invocations WHERE run_id=(SELECT id FROM selected_run) AND id=?', (identity, identity)) if row[0] is not None}
    attempts = [_attempt(tx, _one(tx, 'SELECT * FROM attempts WHERE run_id=? AND id=?', (identity,))) for identity in sorted(attempt_ids)]
    owners = {job["subject"]: job for job in subjects}
    rounds: dict[str, int] = {}
    for attempt in attempts:
        prefix = {"ticket": "ticket", "parent_only": "parent", "final_run": "run"}[owners[attempt["subject"]]["kind"]]
        key = prefix + "_" + attempt["role"]
        rounds[key] = rounds.get(key, 0) + 1
    return {"events": events, "attempts": attempts, "time_zone": "UTC",
            "effect_failures": [failure for identity in identities for failure in _effect_failures(tx, effect=identity)],
            "effects": [dict(row) for identity in sorted(identities) for row in _rows(tx, 'SELECT id,subject,operation,generation,candidate_sha,attempts,status FROM effects WHERE run_id=? AND id=?', (identity,))],
            "actions": _control_history(tx, identities=identities), "rounds": rounds,
            "after": after, "next_after": next_after, "has_more": has_more}



def read_snapshot(tx: Transaction, path: Path, kind: Literal["status", "history", "settings"], *,
                  after: int = 0, limit: int | None = None, expected_run: str | None = None,
                  expected_repository: str | None = None) -> QueryDocument:
    """Project one caller-owned snapshot; opening and protocol gates stay outside.

    The caller supplies an explicit RunTransaction from the sole native
    protocol. Projection never advances Core, grants control or selects a Run
    from displayed data.
    """
    if after < 0 or (limit is not None and not 1 <= limit <= 128):
        raise QueryError("query_limit", "History requires after >= 0 and an optional limit between 1 and 128")
    if not tx.connection.in_transaction:
        raise QueryError("incompatible_run_state", "Query requires an active database snapshot")
    location = _header(tx, path)
    run = dict(_one(tx, 'SELECT * FROM runs WHERE id=?'))
    if ((expected_run is not None and run["id"] != expected_run)
            or (expected_repository is not None and run["repository"].lower() != expected_repository)):
        raise QueryError("run_locator_conflict", "Selected database no longer matches its Run identity")
    language = run.get("language")
    if language not in {"zh", "en"}:
        raise QueryError("recovery_materials_missing", "Run language is missing or invalid; personal defaults cannot replace it")
    if kind == "settings":
        settings = _frozen_settings(tx, run)
        if len(json.dumps(settings, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
            raise QueryError("query_limit", "Public response exceeds its bounded limit")
        return QueryDocument("settings", language, settings)
    now = time.time()
    boundaries = read_boundaries(tx, now)
    by_subject = {job["subject"]: job for job in boundaries["subjects"]}
    subjects = [_subject(tx, run, job, by_subject[job["id"]]) for job in _all(tx, 'SELECT * FROM subjects WHERE run_id=? ORDER BY id')]
    windows = boundaries["supervision_windows"]
    for subject in subjects:
        subject_windows = [window for window in windows if window["subject"] == subject["subject"]]
        subject["supervision_windows"] = subject_windows
    abandonment = boundaries["abandonment"]
    actions = boundaries["actions"]
    title = _rows(tx, 'SELECT title,body FROM requirement_versions WHERE run_id=? AND issue=? AND revision=?', (run['parent_issue'], run['parent_revision']))
    active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
    complete = boundaries["is_terminal"]
    status = boundaries["status"]
    created = _timestamp(run.get("created_at"))
    closed = _rows(tx, "SELECT max(h.created_at) FROM history h JOIN effects e ON e.run_id=h.run_id AND e.id=h.identity WHERE h.run_id=? AND h.kind='effect_consumed' AND e.operation='close'")
    end = _timestamp(closed[0][0]) if complete and closed else None
    elapsed_end = end if complete else now
    elapsed = elapsed_end - created if created is not None and elapsed_end is not None and elapsed_end >= created else None
    focused: dict[str, Any] | None = next((job for job in subjects if job["subject"] == run["integration_holder"]), None)
    if focused is None:
        if len(active) == 1:
            focused = active[0]
        else:
            focused = next((job for job in subjects if job["kind"] == "final_run" and job["phase"] == "complete"), None)
    data: RunStatus = {"run_id": run["id"], "repository": run["repository"],
        "parent": {"number": run["parent_issue"], "title": title[0][0] if title else None,
                   "body": title[0][1] if title else None, "revision": run["parent_revision"]},
        "status": status, "phase": status if abandonment is not None else _public_phase(active[0]["phase"]) if len(active) == 1 else "parallel" if active else
            "merged" if any(job["kind"] == "final_run" and job["phase"] == "complete" for job in subjects) else status,
        "created_at": created, "elapsed_seconds": elapsed, "repository_root": location["repository_root"],
        "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
        "policy_snapshot": json.loads(run["policy"]),
        "subjects": subjects, "operator_actions": actions,
        "focus_subject": focused["subject"] if focused else None,
        "next_action": _next_action(run, actions, status, boundaries["delivery_cleanup"]),
        "delivery_cleanup": boundaries["delivery_cleanup"], "supervision_windows": windows,
        "latest_resume": None, "resume_waits": [], "abandonment": abandonment, "unavailable_facts": [],
        "diagnostics": [], "notifications": _notification_facts(tx, run, path),
        "executor_control": {"owner": run["executor"], "epoch": run["epoch"], "stopped": bool(run["stopped"]), "blocked": run["blocked"], "live_status": "unknown"},
        "scheduling": {"concurrency": run["concurrency"], "integration_holder": run["integration_holder"],
                       "active": _all(tx, 'SELECT id,subject,invocation FROM leases WHERE run_id=? ORDER BY subject'),
                       "work_fifo": [row["subject"] for row in _all(tx, 'SELECT subject FROM work_queue WHERE run_id=? ORDER BY ready')],
                       "integration_fifo": [row["subject"] for row in _all(tx, 'SELECT subject FROM integration_queue WHERE run_id=? ORDER BY ready')]}}
    control = _control_history(tx)
    data["latest_resume"] = control[0] if control else None
    data["resume_waits"] = _resume_waits(run, subjects, data["scheduling"]["active"])
    data["unavailable_facts"] = ["host_liveness", "host_activity_segments", "notifications.journal_binding"]
    if data["notifications"]["journal_state"] != "available":
        data["unavailable_facts"].append("notifications")
    if data["notifications"]["configuration"] is None:
        data["unavailable_facts"].append("notifications.configuration")
    if kind == "history":
        data["history"] = _native_history(tx, subjects, after=after, limit=limit)
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
        raise QueryError("query_limit", "Public response exceeds its bounded limit")
    return QueryDocument(kind, language, data)


def read(path: Path, kind: Literal["status", "history", "settings"], *, after: int = 0, limit: int | None = None,
         expected_run: str | None = None, expected_repository: str | None = None,
         storage: Storage | None = None) -> QueryDocument:
    """Read native facts without granting control or interpreting other formats."""
    if after < 0 or (limit is not None and not 1 <= limit <= 128):
        raise QueryError("query_limit", "History requires after >= 0 and an optional limit between 1 and 128")
    try:
        # A supplied owner is already an exact selection. A contradictory
        # requested selector is a location conflict, not corrupt stored state.
        if storage is not None and expected_run is not None and storage.run_id != expected_run:
            raise QueryError("run_locator_conflict", "Selected database no longer matches its Run identity")
        if storage is None:
            require_supported_state(path)
        selected = open_run(path, run_id=expected_run, storage=storage)
        kernel = Kernel._from_storage(selected)
        with selected.transaction(write=False) as tx:
            document = read_snapshot(tx, path, kind, after=after, limit=limit,
                                     expected_run=expected_run, expected_repository=expected_repository)
            if kind == "settings":return document
            observation = capture_observation(kernel, tx)
            run = tx.one(*run_statement(tx, 'SELECT id,executor,epoch FROM runs WHERE id=?', (), run_last=True))
            scope = (run['id'], run['executor'], run['epoch'])
        # The public RepositoryRoute and explicit Storage entry share this path.
        # Physical read-only inspection occurs after the SQLite snapshot closes.
        data = dict(document.data)
        data['current_observation'] = current = read_current(kernel, observation, scope)
        if kind == 'status':
            data['executor_control']['live_status'] = current['host']['disposition']
            for job in data['subjects']:
                pending = job['pending_attempt']
                if pending is not None:
                    for invocation in pending['invocations']:
                        match = next((item for item in current['workers'] if item['invocation_id'] == invocation['id']), None)
                        if match is not None:invocation['live_status'] = match['disposition']
        if current['host']['disposition'] != 'unknown':data['unavailable_facts'].remove('host_liveness')
        if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
            raise QueryError("query_limit", "Public response exceeds its bounded limit")
        return QueryDocument(kind, document.language, data)
    except QueryError:
        raise
    except UnsupportedState:
        raise
    except KernelError as error:
        raise _state_error(path, error) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise _state_error(path, error) from error
