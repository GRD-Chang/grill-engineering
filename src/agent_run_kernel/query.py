"""Read-only public facts from the canonical store, never a writable Run."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import math
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
    return float(value)


def _header(tx: Transaction, path: Path) -> dict[str, Any]:
    run = dict(tx.one("SELECT * FROM runs"))
    if not isinstance(run["id"], str) or not isinstance(run["repository"], str) or not isinstance(run["parent_issue"], int):
        raise QueryError("incompatible_run_state", "Invalid Run identity")
    root = run.get("repository_root")
    if root is not None and (not isinstance(root, str) or not Path(root).is_absolute()):
        raise QueryError("incompatible_run_state", "Invalid recorded repository root")
    phases = [row[0] for row in tx.rows("SELECT DISTINCT phase FROM subjects")]
    return {"run_id": run["id"], "repository": run["repository"], "parent": run["parent_issue"],
            "repository_root": root, "created_at": _timestamp(run.get("created_at")),
            "phases": phases, "stopped": bool(run["stopped"]), "blocked": run["blocked"],
            "state_dir": str(path.parent), "database": str(path)}


def header(path: Path) -> dict[str, Any]:
    try:
        with Storage(path).transaction(write=False) as tx:
            return _header(tx, path)
    except (KernelError, sqlite3.Error, OSError) as error:
        raise QueryError("incompatible_run_state", str(error)) from error


def discover(root: Path) -> list[dict[str, Any]]:
    """Discover canonical databases without creating a locator or repairing one."""
    if not root.exists():
        return []
    if root.is_symlink():
        raise QueryError("run_locator_invalid", "State directory is a symbolic link")
    paths = [root] if root.is_file() else sorted(root.rglob("kernel.sqlite3"))
    if len(paths) > 256:
        raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    candidates: list[dict[str, Any]] = []
    for path in paths:
        if path.is_symlink():
            raise QueryError("run_locator_conflict", f"Symbolic database path: {path}")
        candidates.append(header(path))
    return candidates


def _invocations(tx: Transaction, subject: str, attempt: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT i.*,t.role,t.model,t.effort,t.backend_id,a.subject FROM invocations i
             JOIN attempts a ON a.id=i.attempt JOIN threads t ON t.id=i.thread WHERE a.subject=?"""
    values: tuple[Any, ...] = (subject,)
    if attempt is not None:
        sql += " AND a.id=?"
        values += (attempt,)
    result = _all(tx, sql + " ORDER BY i.rowid", values)
    for row in result:
        for field in ("allocated_at", "started_at", "returned_at", "exited_at"):
            row[field] = _timestamp(row.get(field))
        row["elapsed_seconds"] = (row["exited_at"] - row["started_at"]
                                  if row["started_at"] is not None and row["exited_at"] is not None
                                  and row["exited_at"] >= row["started_at"] else None)
        row["live_status"] = "unknown"
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
            title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?", (run["parent_issue"], run["parent_revision"]))
            active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
            final_pending = tx.rows("SELECT id FROM final_target")
            complete = not active and not (final_pending and not any(job["kind"] == "final_run" for job in subjects))
            status = "operator_stopped" if run["stopped"] else "blocked" if run["blocked"] else "completed" if complete else "active"
            created = _timestamp(run.get("created_at"))
            closed = tx.rows("SELECT max(h.created_at) FROM history h JOIN effects e ON e.id=h.identity WHERE h.kind='effect_consumed' AND e.operation='close'")
            end = _timestamp(closed[0][0]) if complete and closed else None
            elapsed = max(0.0, (end if complete else time.time()) - created) if created is not None and (not complete or end is not None) else None
            data: dict[str, Any] = {"run_id": run["id"], "repository": run["repository"],
                "parent": {"number": run["parent_issue"], "title": title[0][0] if title else None},
                "status": status, "phase": active[0]["phase"] if len(active) == 1 else "parallel" if active else "complete",
                "created_at": created, "elapsed_seconds": elapsed, "repository_root": location["repository_root"],
                "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "subjects": subjects, "operator_actions": actions, "operator_action": actions[0] if actions else None,
                "next_action": actions[0]["action"] if actions else "observe" if active else None,
                "diagnostics": [], "notifications": None,
                "executor_control": {"owner": run["executor"], "epoch": run["epoch"], "stopped": bool(run["stopped"]), "live_status": "unknown"},
                "scheduling": {"active": _all(tx, "SELECT id,subject,invocation FROM leases ORDER BY subject"),
                               "work_fifo": [row[0] for row in tx.rows("SELECT subject FROM work_queue ORDER BY ready")],
                               "integration_fifo": [row[0] for row in tx.rows("SELECT subject FROM integration_queue ORDER BY ready")]}}
            if kind == "history":
                rows = tx.rows("SELECT * FROM history WHERE position>? ORDER BY position LIMIT ?", (after, limit))
                events = [dict(row) for row in rows]
                for event in events:
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
                             "publication_operation_retries": _all(tx, "SELECT id,subject,operation,attempts,status FROM effects ORDER BY rowid"),
                             "actions": _all(tx, "SELECT id,kind,subject,status FROM actions ORDER BY rowid"),
                             "history_page": {"after": after, "next_after": next_after, "has_more": has_more}})
            if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
                raise QueryError("query_limit", "Public response exceeds its bounded limit")
            return QueryDocument(kind, language, data)
    except QueryError:
        raise
    except (KernelError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise QueryError("incompatible_run_state", str(error)) from error
