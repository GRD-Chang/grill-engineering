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

from agent_run.task_identity import TaskKey
from agent_run.notification_outbox import read_notifications

from .execution_facts import activity, recorded_profile
from .notification_events import resume_facts
from .model import KernelError, Role, artifact
from .storage import QUERY_LIMIT, Storage, Transaction
from .task_location import read_task, task_path


@dataclass(frozen=True)
class QueryDocument:
    kind: Literal["status", "history", "runs", "settings"]
    language: str
    data: Mapping[str, Any]


class QueryError(ValueError):
    def __init__(self, code: str, detail: str, *, candidates: Sequence[Mapping[str, Any]] = ()) -> None:
        self.code = code
        self.candidates = list(candidates)
        super().__init__(detail)


def _all(tx: Transaction, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in tx.all(sql, values)]


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


def _abandonment(tx: Transaction) -> dict[str, Any] | None:
    rows = tx.rows("SELECT id,status FROM actions WHERE kind='abandon'", limit=1)
    return {"action": rows[0]["id"], "status": rows[0]["status"]} if rows else None


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
    abandonment = _abandonment(tx)
    if abandonment is not None:
        terminal = abandonment["status"] == "complete"
    return {"run_id": run["id"], "repository": run["repository"], "parent": run["parent_issue"],
            "repository_root": root, "language": run.get("language"), "created_at": _timestamp(run.get("created_at")),
            "phases": phases, "is_terminal": terminal, "abandonment": abandonment,
            "stopped": bool(run["stopped"]), "blocked": run["blocked"],
            "state_dir": str(path.parent), "database": str(path)}


def header(path: Path) -> dict[str, Any]:
    try:
        with Storage(path).transaction(write=False) as tx:
            return _header(tx, path)
    except KernelError as error:
        raise QueryError(error.code if error.code in {"query_limit", "storage_limit"} else "incompatible_run_state", str(error)) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise QueryError("incompatible_run_state", str(error)) from error


def task_binding(task: TaskKey) -> tuple[str, Path] | None:
    """Read an exact Task publication without creating its admission lock."""
    try:
        return read_task(task_path(task))
    except (KernelError, OSError) as error:
        raise QueryError("run_locator_invalid", f"Cannot read Task publication: {error}") from error


def task_published(candidate: Mapping[str, Any]) -> bool:
    """Check the exact Task pointer; enumeration alone does not publish a Run."""
    if candidate["repository_root"] is None:
        return False
    task = TaskKey(Path(candidate["repository_root"]), candidate["repository"], candidate["parent"])
    binding = task_binding(task)
    return binding == (candidate["run_id"], Path(candidate["database"]).resolve())


def discover(root: Path, *, published: Mapping[Path, str] | None = None) -> list[dict[str, Any]]:
    """Discover canonical databases without creating a locator or repairing one."""
    if not root.exists() and published is None:
        return []
    if root.is_symlink():
        raise QueryError("run_locator_invalid", "State directory is a symbolic link")
    paths: list[Path] = [root] if root.is_file() else []
    if published is not None:
        paths.extend(path for path in published if path.is_relative_to(root.resolve()) and path not in paths)
    if root.is_dir():
        def unreadable(error: OSError) -> None:
            raise QueryError("run_locator_invalid", f"Cannot read state directory: {error}") from error

        for visited, (directory, _children, files) in enumerate(os.walk(root, followlinks=False, onerror=unreadable)):
            if visited >= 4096:
                raise QueryError("query_limit", "Discovery directory limit; provide --state-dir")
            if "kernel.sqlite3" in files:
                path = Path(directory) / "kernel.sqlite3"
                if path not in paths:
                    paths.append(path)
                if published is None and len(paths) > 256:
                    raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    candidates: list[dict[str, Any]] = []
    for path in paths:
        expected = None if published is None else published.get(path.resolve())
        if path.is_symlink():
            if published is not None and expected is None:
                continue
            raise QueryError("run_locator_conflict", f"Symbolic database path: {path}")
        try:
            candidate = header(path)
        except QueryError:
            if published is not None and expected is None:
                continue
            raise
        if expected is not None and candidate["run_id"] != expected:
            raise QueryError("run_locator_conflict", f"Task publication names a different Run: {path}")
        candidates.append(candidate)
        if published is None and len(candidates) > 256:
            raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    return candidates


def _invocations(tx: Transaction, subject: str, attempt: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT i.*,t.role,t.profile_revision,t.backend_id,t.subject AS thread_subject,a.subject FROM invocations i
             JOIN attempts a ON a.id=i.attempt LEFT JOIN threads t ON t.id=i.thread WHERE a.subject=?"""
    values: tuple[Any, ...] = (subject,)
    if attempt is not None:
        sql += " AND a.id=?"
        values += (attempt,)
    result = _all(tx, sql + " ORDER BY i.rowid", values)
    for row in result:
        if row.pop("thread_subject") != subject:
            raise QueryError("incompatible_run_state", "Invocation has a missing or foreign Thread binding")
        profile = recorded_profile(tx, row["profile_revision"])["profiles"][row["role"]]
        row.update({"model": profile["model"], "effort": profile["reasoning_effort"], "profile_reference": profile["reference"]})
        row["allocated_at"] = _timestamp(row["allocated_at"])
        outputs = _all(tx, "SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (row["id"],))
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
        row["output_repairs"] = len(outputs) - 1
        row["output_ordinal"] = outputs[-1]["ordinal"]
        for field in ("raw_id", "raw_return", "dispatch_state", "started_at", "returned_at", "exited_at", "exit_code", "process_span_seconds"):
            row[field] = outputs[-1][field]
        row["elapsed_seconds"] = (sum(output["elapsed_seconds"] for output in outputs)
                                  if all(output["elapsed_seconds"] is not None for output in outputs) else None)
        row["live_status"] = "unknown"
    return result


def _effect_failures(tx: Transaction, subject: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT f.*,e.subject,e.operation,e.status FROM effect_failures f JOIN effects e ON e.id=f.effect"
    values = () if subject is None else (subject,)
    rows = _all(tx, sql + (" WHERE e.subject=?" if subject is not None else "") + " ORDER BY f.observed_at,f.effect,f.dispatch_serial", values)
    for row in rows:
        row["observed_at"] = _timestamp(row["observed_at"])
        row["evidence"] = _material(tx, row["evidence"], row["subject"])
    return rows


def _resync(tx: Transaction, subject: str) -> dict[str, Any]:
    allowance = tx.one("SELECT * FROM resync_allowances WHERE subject=? AND current=1", (subject,))
    snapshots = tx.rows("SELECT * FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
    snapshot = dict(snapshots[0]) if snapshots else None
    return {"generation": allowance["generation"], "used": allowance["used"], "limit": allowance["allowance"],
            "window": snapshot["window"] if snapshot else None, "open": bool(snapshot and snapshot["open"]),
            "source_sha": snapshot["source_sha"] if snapshot else None, "target_sha": snapshot["target_sha"] if snapshot else None}


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
                  "artifact": artifact(Role.REVIEW, tx.get(attempt["result_id"], subject)) if attempt["result_id"] else None}
    effects = _all(tx, "SELECT id,operation,status,attempts,before_sha,after_sha FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') ORDER BY rowid", (subject,))
    cleanup = tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))
    checks = None
    if job["checks_id"]:
        check = dict(tx.one("SELECT * FROM checks WHERE id=? AND subject=?", (job["checks_id"], subject)))
        check["evidence"] = _material(tx, check["material_id"], subject)
        checks = check
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?", (job["issue"], job["revision"]))
    return {"subject": subject, "kind": job["kind"], "issue": job["issue"],
            "title": title[0][0] if title else None, "phase": job["phase"], "purpose": job["purpose"],
            "generation": job["generation"], "revision": job["revision"],
            "candidate_sha": job["candidate_sha"], "publication_sha": job["publication_sha"],
            "integrated_sha": job["integrated_sha"], "pr_number": job["pr_number"],
            "ci_evidence": _material(tx, job["ci_failure"], subject),
            "gates": gates, "budgets": budgets, "pending_attempt": pending, "review": review, "required_checks": checks,
            "integration_resync": _resync(tx, subject), "effect_failures": _effect_failures(tx, subject),
            "pending_writes": effects, "delivery_cleanup": dict(cleanup[0]) if cleanup else None}


def supervision(tx: Transaction, now: float) -> list[dict[str, Any]]:
    """Read canonical wait scopes; only a recorded Core gate proves timeout."""
    run = tx.one("SELECT * FROM runs")
    timeouts: dict[str | None, Any] = {}
    if run["blocked"] == "supervision_timeout":
        paused = tx.rows("SELECT h.material_id,h.subject FROM history h JOIN material m ON m.id=h.material_id "
                         "JOIN waits w ON w.subject IS NULL AND w.scope_id=json_extract(CAST(m.body AS TEXT),'$.scope_id') "
                         "AND w.kind=json_extract(CAST(m.body AS TEXT),'$.kind') "
                         "WHERE h.kind='paused' AND h.identity='supervision_timeout' "
                         "AND m.purpose='supervision_timeout' ORDER BY h.position DESC LIMIT 1")
        if paused:
            timeouts[None] = _material(tx, paused[0]["material_id"], paused[0]["subject"])
    for gate in tx.all("SELECT subject,evidence FROM gates WHERE code='supervision_timeout'"):
        timeouts[gate["subject"]] = _material(tx, gate["evidence"], gate["subject"])
    rows = _all(tx, "SELECT w.*,m.subject AS scope_owner,m.purpose AS scope_purpose,"
                "e.subject AS evidence_owner,e.purpose AS evidence_purpose,s.kind AS delivery_kind,s.issue "
                "FROM waits w JOIN material m ON m.id=w.scope_id LEFT JOIN material e ON e.id=w.evidence "
                "LEFT JOIN subjects s ON s.id=w.subject ORDER BY w.deadline,w.scope_id,w.kind")
    result = []
    for row in rows:
        scope = _material(tx, row["scope_id"], row["scope_owner"])
        if row["scope_purpose"] != "supervision_scope" or not isinstance(scope, dict) or scope.get("subject") != row["subject"]:
            raise QueryError("incompatible_run_state", "Invalid recorded supervision scope")
        if row["evidence"] is not None and row["evidence_purpose"] != "github_read_observation":
            raise QueryError("incompatible_run_state", "Invalid recorded supervision observation")
        timeout = timeouts.get(row["subject"])
        timed_out = isinstance(timeout, dict) and timeout.get("scope_id") == row["scope_id"] and timeout.get("kind") == row["kind"]
        command = f"agent-run resume {run['parent_issue']} --repo {run['repository']}"
        if row["delivery_kind"] == "ticket":
            command += f" --ticket {row['issue']}"
        deadline = _timestamp(row["deadline"])
        result.append({"subject": row["subject"], "kind": row["kind"], "scope_id": row["scope_id"], "scope": scope,
            "started_at": _timestamp(row["started_at"]), "deadline": deadline,
            "remaining_seconds": 0 if timed_out else max(0, int(deadline - now)) if deadline is not None else None,
            "retry_count": row["retry_count"], "next_retry_at": _timestamp(row["next_retry_at"]),
            "latest_observation": _material(tx, row["evidence"], row["evidence_owner"]),
            "timed_out": timed_out, "timeout": timeout if timed_out else None,
            "head_sha": None, "base_sha": None,
            "next_action": command, "timeout_resume_action": command, "resume_action": command})
    return result


def _operator_actions(run: Mapping[str, Any], subjects: list[dict[str, Any]], *,
                      abandonment: Mapping[str, Any] | None = None,
                      supervision_windows: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Describe recorded gates; this does not decide whether a command will apply."""
    if abandonment is not None:
        return []  # The permanent fence supersedes ordinary business gates.
    result: list[dict[str, Any]] = []
    if run["stopped"] or run["blocked"]:
        result.append({"subject": None, "scope": "run", "reason": "operator_stopped" if run["stopped"] else run["blocked"],
                       "action": "resume" if run["stopped"] or run["blocked"] == "supervision_timeout" else "requeue" if run["blocked"] == "requeue_required" else "inspect", "evidence": None})
    for job in subjects:
        for gate in job["gates"]:
            action = ("requeue" if gate["code"] == "requeue_required" else "resume"
                      if gate["code"] in {"human_blocker", "operator_stopped", "execution_failed", "worker_unavailable",
                                          "review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted",
                                          "supervision_timeout", "ci_investigation_paused", "integration_resync_exhausted"} else "inspect")
            result.append({"subject": job["subject"], "issue": job["issue"], "generation": job["generation"],
                           "scope": "ticket" if job["kind"] == "ticket" else "run", "reason": gate["code"],
                           "action": action, "evidence": gate["detail"]})
        if job["phase"] == "approval":
            result.append({"subject": job["subject"], "issue": job["issue"], "generation": job["generation"],
                           "scope": "run", "reason": "final_approval", "action": "approve", "evidence": None})
    owners = {job["subject"]: job for job in subjects}
    for item in result:
        owner = owners.get(item["subject"])
        local = item["scope"] == "ticket"
        operation = item["action"]
        reason = item["reason"]
        artifact = item["evidence"]
        if reason == "supervision_timeout":
            waits = [window for window in supervision_windows if window["subject"] == item["subject"] and window["timed_out"]]
            if waits:
                artifact = {**(artifact if isinstance(artifact, Mapping) else {}), "supervision_windows": waits}
        reasons = artifact.get("human_blockers", [reason]) if reason == "human_blocker" and isinstance(artifact, Mapping) else [reason]
        if reason == "supervision_timeout" and isinstance(artifact, Mapping):
            for window in artifact.get("supervision_windows", []):
                observation = window["latest_observation"]
                if isinstance(observation, Mapping) and isinstance(observation.get("code"), str) and isinstance(observation.get("message"), str):
                    reasons.append(f"{observation['code']}: {observation['message']}")
        pending = owner["pending_attempt"] if owner else None
        invocation = pending["invocations"][-1] if pending and pending["invocations"] else None
        kind = ("final_approval" if operation == "approve" else "review_budget_checkpoint" if "budget_exhausted" in reason
                else "execution_failure" if reason == "execution_failed" else reason)
        command = f"agent-run {operation} {run['parent_issue']} --repo {run['repository']}"
        if local and operation == "resume":
            command += f" --ticket {item['issue']}"
        object_label = f"Ticket #{owner['issue']}" if local and owner else f"Parent Issue #{owner['issue']}" if owner and owner["kind"] == "parent_only" else "Delivery Run"
        item.update({"operation": operation, "scope": "local" if local else "global", "work_subject": item["subject"],
            "location": owner["subject"] if local and owner else "parent" if owner and owner["kind"] == "parent_only" else "run_publication" if owner else "run",
            "phase": _public_phase(owner["phase"]) if owner else None,
            "status_code": "parent_approval_pending" if operation == "approve" and owner and owner["kind"] == "parent_only" else
                           "run_approval_pending" if operation == "approve" else reason,
            "action_kind": kind, "type": {"final_approval": "Final Approval", "human_blocker": "Human Blocker",
                "review_budget_checkpoint": "Review Budget Checkpoint", "execution_failure": "Execution Failure",
                "operator_stopped": "Operator Stopped", "supervision_timeout": "Supervision Timeout Pause"}.get(kind, kind),
            "action": {"kind": "command" if operation != "inspect" else "inspect", "command": operation if operation != "inspect" else None,
                       "selector": run["id"], "ticket_number": item.get("issue") if local else None},
            "object": object_label, "role": pending["role"] if pending else None, "run_paused": not local,
            "trigger_invocation": invocation if reason == "human_blocker" else None,
            "preserved": "；".join(part for part in (f"Candidate {owner['candidate_sha']}" if owner and owner["candidate_sha"] else "",
                         f"PR #{owner['pr_number']}" if owner and owner["pr_number"] else "") if part) or None,
            "reasons": reasons,
            "evidence": {"operator_gate": {"code": reason, "detail": artifact},
                         "human_blockers": artifact.get("human_blockers") if isinstance(artifact, Mapping) else None,
                         "diagnostics": None, "failure_evidence": artifact if reason != "human_blocker" else None,
                         "execution_failure": artifact if reason == "execution_failed" else None, "blocked_reason": reason},
            "next_action": command if operation != "inspect" else "Inspect original facts and diagnostics"})
    return result


def _public_phase(phase: str) -> str:
    return {"development": "developing", "review": "reviewing", "publish": "publication_pending",
            "checks": "waiting_checks", "approval": "ready_for_approval", "merge": "merging",
            "close": "closing", "complete": "completed"}.get(phase, phase)


def _public_status(run: Mapping[str, Any], subjects: list[dict[str, Any]], terminal: bool, *,
                   abandonment: Mapping[str, Any] | None = None) -> str:
    """User-facing classification of recorded phases, with no transition authority."""
    if abandonment is not None:
        return "abandoned" if abandonment["status"] == "complete" else "abandonment_pending"
    if run["stopped"]:
        return "operator_stopped"
    if run["blocked"]:
        return str(run["blocked"])
    unfinished = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
    if (len(unfinished) > 1 and all(job["kind"] == "ticket" for job in unfinished)
            and any(job["gates"] for job in unfinished) and any(not job["gates"] for job in unfinished)):
        return "active"
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
    current = unfinished
    if not current and subjects and all(job["kind"] == "ticket" for job in subjects):
        return "run_acceptance_pending"
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
        completed_final = next((job for job in subjects
            if job["kind"] == "final_run" and job["phase"] == "complete"), None)
        selected = active[0] if len(active) == 1 else completed_final
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
            "integration_resync": selected["integration_resync"] if selected else None,
            "integration_development": None if integration is None else {"window": integration["window"], "used": integration["developments_used"], "limit": integration["development_limit"], "renewals": None},
            "ci_recovery": None if ci is None else {"window": ci["window"], "developments_used": ci["developments_used"],
                "development_limit": ci["development_limit"], "reviewer_invocations": ci["reviews_used"],
                "review_required": bool(ci["review_required"]), "failure_evidence": selected["ci_evidence"] if selected else None},
            "agent_invocation": invocation, "semantic_agent_attempt": attempt,
            "output_attempt": None if invocation is None else {"invocation_started_at": invocation["started_at"], "attempt_count": len(invocation["outputs"]), "repairs_used": invocation["output_repairs"]},
            "budget_window": attempt["window"] if attempt else ordinary["window"] if ordinary else None,
            "publication_operation_retry": selected["effect_failures"] if selected else [], "latest_resume": None, "lifecycle_action": None,
            "scope_change": run["blocked"] if run["blocked"] == "unsupported_scope_change" else None,
            "abandonment": None, "supervision": None,
            "delivery_cleanup": None if not cleanups else {"status": "cleanup_pending" if cleanup_items else "completed",
                "last_error": next((row["outcome"] for row in cleanups if row["outcome"]), None), "items": cleanup_items}}


def _next_action(run: Mapping[str, Any], actions: list[dict[str, Any]], status: str,
                 cleanup: Mapping[str, Any] | None) -> str:
    if status == "abandonment_pending":
        return f"agent-run abandon {run['parent_issue']} --repo {run['repository']}"
    if status == "abandoned":
        return "无"
    if actions:
        action = actions[0]
        return str(action["next_action"])
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
            "conclusion": None, "findings": [] if job is None or job["review"] is None else review_findings(job["review"]["artifact"])}


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
        snapshot = event["status"]
        event_status = None
        if snapshot is not None and event["phase"] is not None:
            observed = {"kind": job["kind"], "phase": event["phase"],
                        "gates": [{"code": code} for code in snapshot["gates"]]}
            event_status = _public_status(snapshot, [observed], event["phase"] in {"complete", "abandoned"})
        projected.append({"at": datetime.fromtimestamp(moment, UTC).isoformat() if moment is not None else None,
            "kind": event["kind"], "object": f"Ticket #{job['issue']}" if job["kind"] == "ticket" else f"Parent Issue #{job['issue']}" if job["kind"] == "parent_only" else "Final Run",
            "subject": job["subject"], "generation": event["generation"], "identity": event["identity"],
            "phase": _public_phase(event["phase"]) if event["phase"] is not None else None,
            "purpose": event["purpose"], "status": event_status, "status_snapshot": snapshot, "role": None, "round": None,
            "details": [] if event["evidence"] is None else [event["evidence"]]})
    return {"events": projected, "execution_activity": "unknown",
            "scheduler": {"activity": "unknown", "failure": None}, "multi_ticket": data["multi_ticket"],
            "summary": {"rounds": rounds, "elapsed_seconds": data["elapsed_seconds"]}, "time_zone": "UTC"}


def _multi_ticket(tx: Transaction, run: Mapping[str, Any], subjects: list[dict[str, Any]],
                  scheduling: Mapping[str, Any]) -> dict[str, Any] | None:
    tickets = [job for job in subjects if job["kind"] == "ticket"]
    if not tickets:
        return None
    active = {row["subject"] for row in scheduling["active"]}
    work_positions = {key: position for position, key in enumerate(scheduling["work_fifo"], 1)}
    integration_positions = {key: position for position, key in enumerate(scheduling["integration_fifo"], 1)}
    views = []
    for job in tickets:
        budgets = {row["purpose"]: row for row in job["budgets"] if row["current"]}
        required = budgets.get("requirements", {})
        ci = budgets.get("ci", {})
        integration = budgets.get("integration", {})
        checks = job["required_checks"]
        blockers = _all(tx, "SELECT s.issue FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=? AND s.phase!='complete' ORDER BY s.issue", (job["subject"],))
        views.append({"ticket_number": job["issue"], "phase": _public_phase(job["phase"]),
            "blocked_by": [row["issue"] for row in blockers], "blocked_reason": job["gates"][0]["code"] if job["gates"] else None,
            "execution_activity": "unknown", "has_work_slot": job["subject"] in active,
            "holds_integration": run["integration_holder"] == job["subject"],
            "work_queue_position": work_positions.get(job["subject"]), "queue_position": integration_positions.get(job["subject"]),
            "invocation": job["pending_attempt"]["invocations"][-1] if job["pending_attempt"] and job["pending_attempt"]["invocations"] else None,
            "pending_write": job["pending_writes"][0] if job["pending_writes"] else None,
            "required_checks": None if checks is None else {"head_sha": checks["head_sha"], "pr_number": checks["pr_number"],
                "result": checks["result"], "evidence": checks["evidence"]},
            "usage": {"development_attempts": required.get("developments_used"), "development_limit": required.get("development_limit"),
                "reviewer_invocations": required.get("reviews_used"), "review_limit": required.get("review_limit"),
                "ci_developments_used": ci.get("developments_used"), "ci_development_limit": ci.get("development_limit"),
                "integration_development_used": integration.get("developments_used"), "integration_development_limit": integration.get("development_limit"),
                "integration_resync_used": job["integration_resync"]["used"], "integration_resync_limit": job["integration_resync"]["limit"]},
            "pause_requested": None, "resume_accepted": None, "stop_wait_reason": None,
            "supervision": job.get("supervision"), "supervision_windows": job.get("supervision_windows", []),
            "recovery_action": None})
    return {"executor_activity": "unknown", "work_concurrency_limit": run["concurrency"],
            "work_slots_active": len(active), "integration_holder": run["integration_holder"],
            "integration_queue": scheduling["integration_fifo"], "tickets": views}


def _control_history(tx: Transaction, *, latest_resume_only: bool = False) -> list[dict[str, Any]]:
    sql = ("SELECT * FROM actions WHERE kind='resume' ORDER BY rowid DESC LIMIT 1" if latest_resume_only
           else "SELECT * FROM actions ORDER BY rowid")
    actions = _all(tx, sql)
    first_owner = tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
    for action in actions:
        owner = action["subject"] or first_owner
        action["payload"] = _material(tx, action["payload_id"], owner)
        action["receipt"] = _material(tx, action["receipt_id"], owner)
    return actions


def _resume_history(tx: Transaction, control: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display original acceptance and the shared exact-target outcome facts."""
    resumes: list[dict[str, Any]] = []
    applications = []
    for action in control:
        if action["kind"] != "resume":
            continue
        successors = _all(tx, "SELECT id FROM invocations WHERE action_id=? ORDER BY rowid", (action["id"],))
        started = tx.one("SELECT min(o.started_at) FROM invocation_outputs o JOIN invocations i "
                         "ON i.id=o.invocation WHERE i.action_id=?", (action["id"],))[0]
        receipt = action["receipt"]
        application = tx.rows("SELECT position,generation FROM history WHERE kind='control_applied' "
                              "AND identity=? AND material_id=? ORDER BY position LIMIT 1",
                              (action["id"], action["receipt_id"]))
        if application:
            applications.append(application[0])
        resumes.append({"action_id": action["id"], "subject": action["subject"], "status": action["status"],
                        "payload": action["payload"], "receipt": receipt,
                        "applied": isinstance(receipt, Mapping) and receipt.get("applied") is True,
                        "successor_invocations": [row["id"] for row in successors],
                        "started_at": _timestamp(started), "progress": None})
    if not resumes or not applications:
        return resumes
    by_action = {row["action_id"]: row for row in resumes}
    # This is a candidate read, not a second Resume interpretation. Only the
    # shared reader decides whether a proof belongs to the original Action.
    # Archived status bodies/phase copy are never needed by that reader.
    sql = ("SELECT position,kind,identity,subject,generation,material_id FROM history WHERE position>? "
           "AND kind IN ('worker_started','effect_consumed','checks_observed','role_consumed','paused')")
    values: tuple[Any, ...] = (min(row["position"] for row in applications),)
    if len(resumes) == 1 and resumes[0]["subject"] is not None:
        sql += " AND subject=? AND generation=?"
        values += (resumes[0]["subject"], applications[0]["generation"])
    for row in tx.all(sql + " ORDER BY position", values):
        facts = resume_facts(tx, dict(row))
        if facts and facts["action_id"] in by_action:
            by_action[facts["action_id"]]["progress"] = facts
    return resumes


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
    from agent_run.github_identity import describe_identity
    from agent_run.messages import text

    profiles = recorded_profile(tx, run["profile_revision"])
    rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
    inputs = _material(tx, rows[0]["material_id"], rows[0]["subject"]) if rows else {}
    if not isinstance(inputs, dict):
        raise QueryError("incompatible_run_state", "Invalid frozen creation inputs")
    bindings = []
    for thread in _all(tx, "SELECT * FROM threads ORDER BY rowid"):
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

def notification_boundaries(tx: Transaction, path: Path) -> dict[str, Any]:
    """Project recorded operator gates in the observer's existing read snapshot.

    This is the same display classification as status, never command admission.
    No journal, transport, Host probe or business mutation belongs to this read.
    """
    run = dict(tx.one("SELECT * FROM runs"))
    subjects = [_subject(tx, job) for job in _all(tx, "SELECT * FROM subjects ORDER BY id")]
    windows = supervision(tx, time.time())
    location = _header(tx, path)
    abandonment = location["abandonment"]
    closures = _all(tx, "SELECT e.subject,e.generation,e.id AS effect_id,e.receipt_id,e.pr_number "
        "FROM effects e JOIN subjects s ON s.id=e.subject AND s.generation=e.generation "
        "WHERE s.kind!='ticket' AND e.operation='close' AND e.status='confirmed' ORDER BY e.rowid")
    for closure in closures:
        closure["receipt"] = _material(tx, closure["receipt_id"], closure["subject"])
    return {"actions": _operator_actions(run, subjects, abandonment=abandonment, supervision_windows=windows),
            "supervision_windows": windows,
            "generations": {job["subject"]: job["generation"] for job in subjects},
            "status": _public_status(run, subjects, location["is_terminal"], abandonment=abandonment),
            "is_terminal": location["is_terminal"], "abandonment": abandonment,
            "delivery_cleanup": _status_fields(run, subjects)["delivery_cleanup"], "closures": closures,
            "highwater": tx.one("SELECT coalesce(max(position),0) FROM history")[0]}


def read(path: Path, kind: Literal["status", "history", "settings"], *, after: int = 0, limit: int | None = None,
         expected_run: str | None = None, expected_repository: str | None = None) -> QueryDocument:
    if after < 0 or (limit is not None and not 1 <= limit <= 128):
        raise QueryError("query_limit", "History requires after >= 0 and an optional limit between 1 and 128")
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
            if kind == "settings":
                settings = _frozen_settings(tx, run)
                if len(json.dumps(settings, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
                    raise QueryError("query_limit", "Public response exceeds its bounded limit")
                return QueryDocument("settings", language, settings)
            subjects = [_subject(tx, job) for job in _all(tx, "SELECT * FROM subjects ORDER BY id")]
            windows = supervision(tx, time.time())
            for subject in subjects:
                subject_windows = [window for window in windows if window["subject"] == subject["subject"]]
                subject["supervision_windows"] = subject_windows
                subject["supervision"] = subject_windows[0] if len(subject_windows) == 1 else None
            abandonment = location["abandonment"]
            actions = _operator_actions(run, subjects, abandonment=abandonment, supervision_windows=windows)
            title = tx.rows("SELECT title,body FROM requirement_versions WHERE issue=? AND revision=?", (run["parent_issue"], run["parent_revision"]))
            active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
            complete = location["is_terminal"]
            status = _public_status(run, subjects, complete, abandonment=abandonment)
            created = _timestamp(run.get("created_at"))
            closed = tx.rows("SELECT max(h.created_at) FROM history h JOIN effects e ON e.id=h.identity WHERE h.kind='effect_consumed' AND e.operation='close'")
            end = _timestamp(closed[0][0]) if complete and closed else None
            elapsed_end = end if complete else time.time()
            elapsed = elapsed_end - created if created is not None and elapsed_end is not None and elapsed_end >= created else None
            data: dict[str, Any] = {"run_id": run["id"], "repository": run["repository"],
                "parent": {"number": run["parent_issue"], "title": title[0][0] if title else None,
                           "body": title[0][1] if title else None, "revision": run["parent_revision"]},
                "status": status, "phase": status if abandonment is not None else _public_phase(active[0]["phase"]) if len(active) == 1 else "parallel" if active else
                    "merged" if any(job["kind"] == "final_run" and job["phase"] == "complete" for job in subjects) else status,
                "created_at": created, "elapsed_seconds": elapsed, "repository_root": location["repository_root"],
                "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "policy_snapshot": json.loads(run["policy"]),
                "subjects": subjects, "operator_actions": actions, "operator_action": actions[0] if actions else None,
                "next_action": actions[0]["action"] if actions else "observe" if active else None,
                "diagnostics": [], "notifications": _notification_facts(tx, run, path),
                "executor_control": {"owner": run["executor"], "epoch": run["epoch"], "stopped": bool(run["stopped"]), "live_status": "unknown"},
                "scheduling": {"active": _all(tx, "SELECT id,subject,invocation FROM leases ORDER BY subject"),
                               "work_fifo": [row["subject"] for row in _all(tx, "SELECT subject FROM work_queue ORDER BY ready")],
                               "integration_fifo": [row["subject"] for row in _all(tx, "SELECT subject FROM integration_queue ORDER BY ready")]}}
            data.update(_status_fields(run, subjects))
            run_windows = [window for window in windows if window["subject"] is None]
            data["supervision"] = run_windows[0] if len(run_windows) == 1 else windows[0] if len(windows) == 1 else None
            data["supervision_windows"] = windows
            control = _control_history(tx, latest_resume_only=kind == "status")
            resumes = _resume_history(tx, control)
            data["latest_resume"] = resumes[-1] if resumes else None
            data["resume_waits"] = _resume_waits(run, subjects, data["scheduling"]["active"])
            data["abandonment"] = abandonment
            data["multi_ticket"] = _multi_ticket(tx, run, subjects, data["scheduling"])
            data["next_action"] = _next_action(run, actions, status, data["delivery_cleanup"])
            data["progress"] = _progress(data)
            data["gate"] = "required_checks" if status == "waiting_checks" else None
            data["unavailable_facts"] = ["host_liveness", "host_activity_segments", "notifications.journal_binding"]
            if data["notifications"]["journal_state"] != "available":
                data["unavailable_facts"].append("notifications")
            if data["notifications"]["configuration"] is None:
                data["unavailable_facts"].append("notifications.configuration")
            if kind == "history":
                events = (_all(tx, "SELECT * FROM history WHERE position>? ORDER BY position", (after,)) if limit is None else
                          [dict(row) for row in tx.rows("SELECT * FROM history WHERE position>? ORDER BY position LIMIT ?", (after, limit))])
                for event in events:
                    if (not isinstance(event["position"], int) or isinstance(event["position"], bool)
                            or not isinstance(event["generation"], int) or event["generation"] < 1
                            or not isinstance(event["kind"], str) or not isinstance(event["identity"], str)):
                        raise QueryError("incompatible_run_state", "Invalid history event identity")
                    event["created_at"] = _timestamp(event.get("created_at"))
                    event["status"] = json.loads(event["status"]) if event["status"] is not None else None
                    event["evidence"] = _material(tx, event["material_id"], event["subject"])
                next_after = events[-1]["position"] if events else after
                has_more = bool(tx.rows("SELECT position FROM history WHERE position>? LIMIT 1", (next_after,)))
                attempts = _all(tx, "SELECT * FROM attempts ORDER BY subject,ordinal")
                for attempt in attempts:
                    attempt["artifact"] = _material(tx, attempt["result_id"], attempt["subject"])
                    attempt.pop("context_id")
                    attempt.update({"attempt_id": attempt["id"], "work_subject": attempt["subject"], "budget_window": attempt["window"]})
                all_invocations = [invocation for job in subjects for invocation in _invocations(tx, job["subject"])]
                data.update({"timeline": events, "timeline_continuation": [],
                             "semantic_agent_attempts": attempts,
                             "agent_invocations": all_invocations,
                             "publication_operation_retries": _effect_failures(tx),
                             "effect_operations": _all(tx, "SELECT id,subject,operation,attempts,status FROM effects ORDER BY rowid"),
                             "budget_windows": [budget for job in subjects for budget in job["budgets"]],
                             "output_attempts": [dict(output, subject=invocation["subject"]) for invocation in all_invocations for output in invocation["outputs"]], "agent_resumes": resumes, "resume_audit": resumes,
                             "actions": control,
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
