"""Read-only public facts from the canonical store, never a writable Run."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import base64
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal, Mapping, Sequence

from .model import KernelError
from .storage import QUERY_LIMIT, Storage, Transaction


@dataclass(frozen=True)
class QueryDocument:
    kind: Literal["status", "history", "runs"]
    language: str
    data: Mapping[str, Any]


class QueryError(ValueError):
    def __init__(self, code: str, detail: str, *, candidates: Sequence[Mapping[str, Any]] = ()) -> None:
        self.code = code
        self.candidates = list(candidates)
        super().__init__(detail)


def _all(tx: Transaction, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    while True:
        rows = tx.rows(sql + " LIMIT 128 OFFSET ?", (*values, len(result)))
        result.extend(dict(row) for row in rows)
        if len(rows) < 128:
            return result


def _material(tx: Transaction, identity: str | None, subject: str) -> Any:
    if identity is None:
        return None
    body = tx.get(identity, subject)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return {"encoding": "base64", "body": base64.b64encode(body).decode("ascii")}


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise QueryError("incompatible_run_state", "Invalid recorded timestamp")
    try:
        datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise QueryError("incompatible_run_state", "Recorded timestamp is outside the supported calendar range") from error
    return float(value)


def _header(tx: Transaction, path: Path) -> dict[str, Any]:
    run = dict(tx.one("SELECT * FROM runs"))
    if not isinstance(run["id"], str) or not isinstance(run["repository"], str) or not isinstance(run["parent_issue"], int):
        raise QueryError("incompatible_run_state", "Invalid Run identity")
    root = run.get("repository_root")
    if root is not None and (not isinstance(root, str) or not Path(root).is_absolute()):
        raise QueryError("incompatible_run_state", "Invalid recorded repository root")
    owners = _all(tx, "SELECT kind,phase FROM subjects ORDER BY id")
    phases = sorted({row["phase"] for row in owners})
    final_target = bool(tx.rows("SELECT id FROM final_target"))
    terminal = not any(row["phase"] not in {"complete", "abandoned"} for row in owners)
    if final_target and not any(row["kind"] == "final_run" and row["phase"] in {"complete", "abandoned"} for row in owners):
        terminal = False
    return {"run_id": run["id"], "repository": run["repository"], "parent": run["parent_issue"],
            "repository_root": root, "created_at": _timestamp(run.get("created_at")),
            "phases": phases, "is_terminal": terminal, "stopped": bool(run["stopped"]), "blocked": run["blocked"],
            "state_dir": str(path.parent), "database": str(path)}


def header(path: Path) -> dict[str, Any]:
    try:
        with Storage(path).transaction(write=False) as tx:
            return _header(tx, path)
    except KernelError as error:
        raise QueryError(error.code if error.code in {"query_limit", "storage_limit"} else "incompatible_run_state", str(error)) from error
    except (sqlite3.Error, OSError) as error:
        raise QueryError("incompatible_run_state", str(error)) from error


def discover(root: Path) -> list[dict[str, Any]]:
    """Discover canonical databases without creating a locator or repairing one."""
    if not root.exists():
        return []
    if root.is_symlink():
        raise QueryError("run_locator_invalid", "State directory is a symbolic link")
    paths: list[Path] = [root] if root.is_file() else []
    if root.is_dir():
        def unreadable(error: OSError) -> None:
            raise QueryError("run_locator_invalid", f"Cannot read state directory: {error}") from error

        for visited, (directory, _children, files) in enumerate(os.walk(root, followlinks=False, onerror=unreadable)):
            if visited >= 4096:
                raise QueryError("query_limit", "Discovery directory limit; provide --state-dir")
            if "kernel.sqlite3" in files:
                paths.append(Path(directory) / "kernel.sqlite3")
                if len(paths) > 256:
                    raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    candidates: list[dict[str, Any]] = []
    for path in paths:
        if path.is_symlink():
            raise QueryError("run_locator_conflict", f"Symbolic database path: {path}")
        candidates.append(header(path))
    return candidates


def _invocations(tx: Transaction, subject: str, attempt: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT i.*,t.role,t.model,t.effort,t.backend_id,t.subject AS thread_subject,a.subject FROM invocations i
             JOIN attempts a ON a.id=i.attempt LEFT JOIN threads t ON t.id=i.thread WHERE a.subject=?"""
    values: tuple[Any, ...] = (subject,)
    if attempt is not None:
        sql += " AND a.id=?"
        values += (attempt,)
    result = _all(tx, sql + " ORDER BY i.rowid", values)
    for row in result:
        if row.pop("thread_subject") != subject:
            raise QueryError("incompatible_run_state", "Invocation has a missing or foreign Thread binding")
        for field in ("allocated_at", "started_at", "returned_at", "exited_at"):
            row[field] = _timestamp(row.get(field))
        row["process_span_seconds"] = (row["exited_at"] - row["started_at"]
                                  if row["started_at"] is not None and row["exited_at"] is not None
                                  and row["exited_at"] >= row["started_at"] else None)
        row["elapsed_seconds"] = None  # A process interval is not an Agent execution segment.
        row["live_status"] = "unknown"
        row["raw_return"] = _material(tx, row["raw_id"], subject)
        # Host observation and transcript are not copied into the public view.
        row.pop("process_identity", None)
        row.pop("start_evidence", None)
    return result


def _subject(tx: Transaction, job: dict[str, Any]) -> dict[str, Any]:
    subject = job["id"]
    gates = _all(tx, "SELECT * FROM gates WHERE subject=? ORDER BY code", (subject,))
    for gate in gates:
        gate["detail"] = _material(tx, gate["evidence"], subject)
    budgets = _all(tx, "SELECT * FROM budgets WHERE subject=? ORDER BY purpose,window", (subject,))
    pending = None
    if job["current_attempt"]:
        pending = dict(tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (job["current_attempt"], subject)))
        pending["result"] = _material(tx, pending["result_id"], subject)
        pending["invocations"] = _invocations(tx, subject, pending["id"])
        pending.pop("context_id")
    review = None
    if job["review_attempt"]:
        attempt = tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (job["review_attempt"], subject))
        review = {"attempt_id": attempt["id"], "outcome": attempt["outcome"],
                  "artifact": _material(tx, attempt["result_id"], subject)}
    effects = _all(tx, "SELECT id,operation,status,attempts,before_sha,after_sha FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') ORDER BY rowid", (subject,))
    cleanup = tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?", (job["issue"], job["revision"]))
    return {"subject": subject, "kind": job["kind"], "issue": job["issue"],
            "title": title[0][0] if title else None, "phase": job["phase"], "purpose": job["purpose"],
            "generation": job["generation"], "revision": job["revision"],
            "candidate_sha": job["candidate_sha"], "publication_sha": job["publication_sha"],
            "integrated_sha": job["integrated_sha"], "pr_number": job["pr_number"],
            "ci_evidence": _material(tx, job["ci_failure"], subject),
            "gates": gates, "budgets": budgets, "pending_attempt": pending, "review": review,
            "pending_writes": effects, "delivery_cleanup": dict(cleanup[0]) if cleanup else None}


def _operator_actions(run: Mapping[str, Any], subjects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe recorded gates; this does not decide whether a command will apply."""
    result: list[dict[str, Any]] = []
    if run["stopped"] or run["blocked"]:
        result.append({"subject": None, "scope": "run", "reason": "operator_stopped" if run["stopped"] else run["blocked"],
                       "action": "requeue" if run["blocked"] == "requeue_required" else "inspect", "evidence": None})
    for job in subjects:
        for gate in job["gates"]:
            action = ("requeue" if gate["code"] == "requeue_required" else "resume"
                      if gate["code"] in {"human_blocker", "operator_stopped", "execution_failed", "worker_unavailable",
                                          "review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted",
                                          "supervision_timeout", "ci_investigation_paused"} else "inspect")
            result.append({"subject": job["subject"], "issue": job["issue"], "generation": job["generation"],
                           "scope": "ticket" if job["kind"] == "ticket" else "run", "reason": gate["code"],
                           "action": action, "evidence": gate["detail"]})
        if job["phase"] == "approval":
            result.append({"subject": job["subject"], "issue": job["issue"], "generation": job["generation"],
                           "scope": "run", "reason": "final_approval", "action": "approve", "evidence": None})
    return result


def _public_phase(phase: str) -> str:
    return {"development": "developing", "review": "reviewing", "publish": "publication_pending",
            "checks": "waiting_checks", "approval": "ready_for_approval", "merge": "merging",
            "close": "closing", "complete": "completed"}.get(phase, phase)


def _public_status(run: Mapping[str, Any], subjects: list[dict[str, Any]], terminal: bool) -> str:
    """User-facing classification of recorded phases, with no transition authority."""
    if run["stopped"]:
        return "operator_stopped"
    if run["blocked"]:
        return str(run["blocked"])
    codes = {gate["code"] for job in subjects for gate in job["gates"]}
    for code in ("requeue_required", "execution_failed", "supervision_timeout"):
        if code in codes:
            return code
    if "human_blocker" in codes:
        return "ready_for_human"
    if terminal:
        if all(job["phase"] == "abandoned" for job in subjects):
            return "abandoned"
        return "ticket_completed" if all(job["kind"] == "ticket" for job in subjects) else "completed"
    current = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
    if not current and subjects and all(job["kind"] == "ticket" for job in subjects):
        return "ticket_completed"
    if any(job["phase"] == "checks" for job in current):
        return "waiting_checks"
    if len(current) == 1:
        job = current[0]
        if job["phase"] == "approval":
            return "parent_approval_pending" if job["kind"] == "parent_only" else "run_approval_pending"
        if job["kind"] == "parent_only":
            return "parent_delivery_pending"
        if job["kind"] == "final_run":
            return "run_publication_pending" if job["phase"] in {"publish", "publication_commit", "push", "pr_write"} else "run_acceptance_pending"
    return "active"


def _status_fields(run: Mapping[str, Any], subjects: list[dict[str, Any]]) -> dict[str, Any]:
    active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
    selected = next((job for job in subjects if job["subject"] == run["integration_holder"]), None)
    if selected is None:
        selected = active[0] if len(active) == 1 else next((job for job in subjects
            if job["kind"] == "final_run" and job["phase"] == "complete"), None)
    invocation = None
    attempt = None if selected is None else selected["pending_attempt"]
    if attempt and attempt["invocations"]:
        invocation = next((row for row in attempt["invocations"] if row["id"] == attempt["current_invocation"]), None)
    worker = None
    if invocation and attempt and selected and invocation["raw_id"] is None and invocation["exited_at"] is None:
        worker = {"role": attempt["role"], "attempt": attempt["ordinal"], "thread_id": invocation["thread"],
                  "phase": _public_phase(selected["phase"]), "model": invocation["model"], "effort": invocation["effort"],
                  "live_status": "unknown", "dispatch_state": invocation["dispatch_state"]}
    current = [] if selected is None else [budget for budget in selected["budgets"] if budget["current"]]
    ordinary = next((row for row in current if row["purpose"] == ("integration" if selected and selected["kind"] == "final_run" else "requirements")), None)
    integration = next((row for row in current if row["purpose"] == "integration"
                        and row["developments_used"] > 0), None)
    ci = next((row for row in current if row["purpose"] == "ci"), None)
    review_budget = None if ordinary is None else {
        "window": ordinary["window"], "development_attempts": ordinary["developments_used"],
        "development_limit": ordinary["development_limit"], "reviewer_invocations": ordinary["reviews_used"],
        "reviewer_limit": ordinary["review_limit"], "candidate_sha": selected["candidate_sha"] if selected else None,
        "pr_number": selected["pr_number"] if selected else None, "final_ci_fix_used": None, "final_ci_fix_limit": None, "checkpoint_reason": None}
    cleanups = [job["delivery_cleanup"] for job in subjects if job["delivery_cleanup"]]
    cleanup_items = [{"kind": job["kind"], "branch": job["delivery_cleanup"]["source_ref"],
                      "checkout": job["delivery_cleanup"]["checkout"], "status": "cleanup_pending",
                      "last_error": job["delivery_cleanup"]["outcome"],
                      "recovery_action": f"agent-run resume {run['id']}"}
                     for job in subjects if job["delivery_cleanup"] and job["delivery_cleanup"]["state"] != "complete"]
    return {"active_ticket": selected["issue"] if selected and selected["kind"] == "ticket" else None,
            "worker": worker, "run_repair": None, "candidate_sha": selected["candidate_sha"] if selected else None,
            "pr_number": selected["pr_number"] if selected else None, "review_budget": review_budget,
            "integration_resync": None,
            "integration_development": None if integration is None else {"window": integration["window"], "used": integration["developments_used"], "limit": integration["development_limit"], "renewals": None},
            "ci_recovery": None if ci is None else {"window": ci["window"], "developments_used": ci["developments_used"],
                "development_limit": ci["development_limit"], "reviewer_invocations": ci["reviews_used"],
                "review_required": None, "failure_evidence": selected["ci_evidence"] if selected else None},
            "agent_invocation": invocation, "semantic_agent_attempt": attempt,
            "output_attempt": None if invocation is None else {"invocation_started_at": invocation["started_at"], "attempt_count": None, "repairs_used": invocation["output_repairs"]},
            "budget_window": attempt["window"] if attempt else ordinary["window"] if ordinary else None,
            "publication_operation_retry": None, "latest_resume": None, "lifecycle_action": None,
            "scope_change": run["blocked"] if run["blocked"] == "unsupported_scope_change" else None,
            "abandonment": None, "supervision": None,
            "delivery_cleanup": None if not cleanups else {"status": "cleanup_pending" if cleanup_items else "completed",
                "last_error": next((row["outcome"] for row in cleanups if row["outcome"]), None), "items": cleanup_items}}


def _next_action(run: Mapping[str, Any], actions: list[dict[str, Any]], status: str,
                 cleanup: Mapping[str, Any] | None) -> str:
    if actions:
        action = actions[0]
        if action["action"] in {"resume", "approve", "requeue"}:
            command = f"agent-run {action['action']} {run['parent_issue']} --repo {run['repository']}"
            if action["scope"] == "ticket" and action["action"] == "resume":
                command += f" --ticket {action['issue']}"
            return command
        return "Inspect original facts and diagnostics"
    if cleanup and cleanup["status"] == "cleanup_pending":
        return f"agent-run resume {run['id']}"
    if status == "ticket_completed":
        return f"agent-run run {run['parent_issue']}"
    if status in {"completed", "abandoned"}:
        return "无"  # Stable machine copy; human rendering localizes it.
    return f"agent-run status {run['parent_issue']} --repo {run['repository']}"


def _progress(data: Mapping[str, Any]) -> dict[str, Any]:
    subjects = data["subjects"]
    current = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
    job = current[0] if len(current) == 1 else None
    current_object = (f"Ticket #{job['issue']}" if job and job["kind"] == "ticket" else
                      f"Parent Issue #{job['issue']}" if job and job["kind"] == "parent_only" else
                      "Final Run" if job else "Delivery Run")
    tickets = [owner for owner in subjects if owner["kind"] == "ticket"]
    return {"repository": data["repository"], "parent": {"number": data["parent"]["number"], "title": data["parent"]["title"]},
            "status": data["status"], "phase": data["phase"], "current_object": current_object,
            "current_agent": data["worker"], "execution_activity": "unknown",
            "scheduler": {"activity": "unknown", "failure": None}, "elapsed_seconds": data["elapsed_seconds"],
            "ticket_progress": {"completed": sum(owner["phase"] == "complete" for owner in tickets), "total": len(tickets)},
            "round_progress": data["review_budget"], "integration_development": data["integration_development"],
            "integration_resync": data["integration_resync"], "ci_recovery": data["ci_recovery"],
            "run_repair": data["run_repair"], "next_action": data["next_action"],
            "conclusion": None, "findings": [] if job is None or job["review"] is None else job["review"]["artifact"]}


def _history_views(data: Mapping[str, Any], subjects: list[dict[str, Any]], events: list[dict[str, Any]],
                   attempts: list[dict[str, Any]]) -> dict[str, Any]:
    owners = {job["subject"]: job for job in subjects}
    rounds: dict[str, int] = {}
    for attempt in attempts:
        job = owners[attempt["subject"]]
        prefix = {"ticket": "ticket", "parent_only": "parent", "final_run": "run"}[job["kind"]]
        key = prefix + "_" + attempt["role"]
        rounds[key] = rounds.get(key, 0) + 1
    projected = []
    for event in events:
        job = owners[event["subject"]]
        moment = event["created_at"]
        projected.append({"at": datetime.fromtimestamp(moment, UTC).isoformat() if moment is not None else None,
            "kind": event["kind"], "object": f"Ticket #{job['issue']}" if job["kind"] == "ticket" else f"Parent Issue #{job['issue']}" if job["kind"] == "parent_only" else "Final Run",
            "subject": job["subject"], "generation": event["generation"], "identity": event["identity"],
            "phase": None, "status": None, "role": None, "round": None,
            "details": [] if event["evidence"] is None else [event["evidence"]]})
    return {"events": projected, "execution_activity": "unknown",
            "scheduler": {"activity": "unknown", "failure": None}, "multi_ticket": None,
            "summary": {"rounds": rounds, "elapsed_seconds": data["elapsed_seconds"]}, "time_zone": "UTC"}


def read(path: Path, kind: Literal["status", "history"], *, after: int = 0, limit: int = 128,
         expected_run: str | None = None, expected_repository: str | None = None) -> QueryDocument:
    if after < 0 or not 1 <= limit <= 128:
        raise QueryError("query_limit", "History requires after >= 0 and 1 <= limit <= 128")
    try:
        with Storage(path).transaction(write=False) as tx:
            location = _header(tx, path)
            run = dict(tx.one("SELECT * FROM runs"))
            if ((expected_run is not None and run["id"] != expected_run)
                    or (expected_repository is not None and run["repository"].lower() != expected_repository)):
                raise QueryError("run_locator_conflict", "Selected database no longer matches its Run identity")
            language = run.get("language")
            if language not in {"zh", "en"}:
                raise QueryError("recovery_materials_missing", "Run language is missing or invalid; personal defaults cannot replace it")
            subjects = [_subject(tx, job) for job in _all(tx, "SELECT * FROM subjects ORDER BY id")]
            actions = _operator_actions(run, subjects)
            title = tx.rows("SELECT title,body FROM requirement_versions WHERE issue=? AND revision=?", (run["parent_issue"], run["parent_revision"]))
            active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
            complete = location["is_terminal"]
            status = _public_status(run, subjects, complete)
            created = _timestamp(run.get("created_at"))
            closed = tx.rows("SELECT max(h.created_at) FROM history h JOIN effects e ON e.id=h.identity WHERE h.kind='effect_consumed' AND e.operation='close'")
            end = _timestamp(closed[0][0]) if complete and closed else None
            elapsed_end = end if complete else time.time()
            elapsed = elapsed_end - created if created is not None and elapsed_end is not None and elapsed_end >= created else None
            data: dict[str, Any] = {"run_id": run["id"], "repository": run["repository"],
                "parent": {"number": run["parent_issue"], "title": title[0][0] if title else None,
                           "body": title[0][1] if title else None, "revision": run["parent_revision"]},
                "status": status, "phase": _public_phase(active[0]["phase"]) if len(active) == 1 else "parallel" if active else
                    "merged" if any(job["kind"] == "final_run" and job["phase"] == "complete" for job in subjects) else status,
                "created_at": created, "elapsed_seconds": elapsed, "repository_root": location["repository_root"],
                "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "policy_snapshot": json.loads(run["policy"]),
                "subjects": subjects, "operator_actions": actions, "operator_action": actions[0] if actions else None,
                "next_action": actions[0]["action"] if actions else "observe" if active else None,
                "diagnostics": [], "notifications": None,
                "executor_control": {"owner": run["executor"], "epoch": run["epoch"], "stopped": bool(run["stopped"]), "live_status": "unknown"},
                "scheduling": {"active": _all(tx, "SELECT id,subject,invocation FROM leases ORDER BY subject"),
                               "work_fifo": [row[0] for row in tx.rows("SELECT subject FROM work_queue ORDER BY ready")],
                               "integration_fifo": [row[0] for row in tx.rows("SELECT subject FROM integration_queue ORDER BY ready")]}}
            data.update(_status_fields(run, subjects))
            data["next_action"] = _next_action(run, actions, status, data["delivery_cleanup"])
            data["progress"] = _progress(data)
            data["gate"] = "required_checks" if status == "waiting_checks" else None
            data["unavailable_facts"] = ["host_liveness", "notifications", "agent_execution_segments",
                                         "publication_failure_count", "output_attempt_history"]
            if kind == "history":
                rows = tx.rows("SELECT * FROM history WHERE position>? ORDER BY position LIMIT ?", (after, limit))
                events = [dict(row) for row in rows]
                for event in events:
                    if (not isinstance(event["position"], int) or isinstance(event["position"], bool)
                            or not isinstance(event["generation"], int) or event["generation"] < 1
                            or not isinstance(event["kind"], str) or not isinstance(event["identity"], str)):
                        raise QueryError("incompatible_run_state", "Invalid history event identity")
                    event["created_at"] = _timestamp(event.get("created_at"))
                    event["evidence"] = _material(tx, event["material_id"], event["subject"])
                next_after = events[-1]["position"] if events else after
                has_more = bool(tx.rows("SELECT position FROM history WHERE position>? LIMIT 1", (next_after,)))
                attempts = _all(tx, "SELECT * FROM attempts ORDER BY subject,ordinal")
                for attempt in attempts:
                    attempt["artifact"] = _material(tx, attempt["result_id"], attempt["subject"])
                    attempt.pop("context_id")
                data.update({"timeline": events, "timeline_continuation": [],
                             "semantic_agent_attempts": attempts,
                             "agent_invocations": [invocation for job in subjects for invocation in _invocations(tx, job["subject"])],
                             "publication_operation_retries": None,
                             "effect_operations": _all(tx, "SELECT id,subject,operation,attempts,status FROM effects ORDER BY rowid"),
                             "budget_windows": [budget for job in subjects for budget in job["budgets"]],
                             "output_attempts": None, "agent_resumes": None, "resume_audit": None,
                             "actions": _all(tx, "SELECT id,kind,subject,status FROM actions ORDER BY rowid"),
                             "history_page": {"after": after, "next_after": next_after, "has_more": has_more}})
                data.update(_history_views(data, subjects, events, attempts))
            if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
                raise QueryError("query_limit", "Public response exceeds its bounded limit")
            return QueryDocument(kind, language, data)
    except QueryError:
        raise
    except KernelError as error:
        raise QueryError(error.code if error.code in {"query_limit", "storage_limit"} else "incompatible_run_state", str(error)) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise QueryError("incompatible_run_state", str(error)) from error
