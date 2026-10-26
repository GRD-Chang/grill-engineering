"""Scoped read-only identity, operator boundaries and their original evidence.

No public audit expansion, journal, transport, Kernel or mutable Run cache.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
import math
import shlex
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_run.storage.sqlite import Transaction
from agent_run.contracts.copy import text


class QueryError(ValueError):
    def __init__(self, code: str, detail: str, *, candidates: Sequence[Mapping[str, Any]] = (),
                 state_diagnostic: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.candidates = list(candidates)
        self.state_diagnostic = state_diagnostic
        super().__init__(detail)


def action_guidance(operation: str, *, run_id: str, repository: str,
                   ticket: int | None = None, details: bool = False, language: str = "en") -> str:
    """Format guidance using the public CLI's selectors; never grant an action.

    Never substitute a Task selector or emit identity bytes which a display
    sanitizer would remove. Unsupported identities retain their JSON spelling.
    """
    if operation not in {"status", "history", "run", "resume", "stop", "approve", "requeue", "abandon"}:
        raise ValueError("Unsupported guidance command")
    if ticket is not None and operation not in {"resume", "stop"} or details and operation != "history":
        raise ValueError("Unsupported guidance options")
    if any(unicodedata.category(character) in {"Cc", "Cf"}
           for value in (run_id, repository) for character in value):
        identity = json.dumps({"run_id": run_id, "repository": repository}, ensure_ascii=True)
        return text("guidance.unsafe_identity", language=language, identity=identity)
    if operation == "run" or operation not in {"status", "history"} and run_id.isdecimal():
        return text("guidance.exact_action_unavailable", language=language,
                    identity=json.dumps(run_id, ensure_ascii=True))
    argv = ["agent-run", operation, "--repo", repository]
    if ticket is not None:
        argv.extend(("--ticket", str(ticket)))
    if details:
        argv.append("--details")
    return shlex.join([*argv, "--", run_id])


def all_rows(tx: Transaction, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in tx.all(sql, values)]


def material(tx: Transaction, identity: str | None, subject: str) -> Any:
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


def timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise QueryError("incompatible_run_state", "Invalid recorded timestamp")
    try:
        datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise QueryError("incompatible_run_state", "Recorded timestamp is outside the supported calendar range") from error
    return float(value)


def _abandonment(tx: Transaction) -> dict[str, Any] | None:
    rows = tx.rows("SELECT id,status FROM actions WHERE kind='abandon'", limit=1)
    return {"action": rows[0]["id"], "status": rows[0]["status"]} if rows else None


def read_header(tx: Transaction, path: Path | None = None) -> dict[str, Any]:
    run = dict(tx.one("SELECT * FROM runs"))
    if not isinstance(run["id"], str) or not isinstance(run["repository"], str) or not isinstance(run["parent_issue"], int):
        raise QueryError("incompatible_run_state", "Invalid Run identity")
    root = run.get("repository_root")
    if root is not None and (not isinstance(root, str) or not Path(root).is_absolute()):
        raise QueryError("incompatible_run_state", "Invalid recorded repository root")
    owners = all_rows(tx, "SELECT kind,phase FROM subjects ORDER BY id")
    phases = sorted({row["phase"] for row in owners})
    final_target = bool(tx.rows("SELECT id FROM final_target"))
    terminal = not any(row["phase"] not in {"complete", "abandoned"} for row in owners)
    if final_target and not any(row["kind"] == "final_run" and row["phase"] in {"complete", "abandoned"} for row in owners):
        terminal = False
    abandonment = _abandonment(tx)
    if abandonment is not None:
        terminal = abandonment["status"] == "complete"
    result = {"run_id": run["id"], "repository": run["repository"], "parent": run["parent_issue"],
            "repository_root": root, "language": run.get("language"), "created_at": timestamp(run.get("created_at")),
            "phases": phases, "is_terminal": terminal, "abandonment": abandonment,
            "stopped": bool(run["stopped"]), "blocked": run["blocked"]}
    if path is not None:
        result.update(state_dir=str(path.parent), database=str(path))
    return result


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
            timeouts[None] = material(tx, paused[0]["material_id"], paused[0]["subject"])
    for gate in tx.all("SELECT subject,evidence FROM gates WHERE code='supervision_timeout'"):
        timeouts[gate["subject"]] = material(tx, gate["evidence"], gate["subject"])
    rows = all_rows(tx, "SELECT w.*,m.subject AS scope_owner,m.purpose AS scope_purpose,"
                "e.subject AS evidence_owner,e.purpose AS evidence_purpose,s.kind AS delivery_kind,s.issue "
                "FROM waits w JOIN material m ON m.id=w.scope_id LEFT JOIN material e ON e.id=w.evidence "
                "LEFT JOIN subjects s ON s.id=w.subject ORDER BY w.deadline,w.scope_id,w.kind")
    result = []
    for row in rows:
        scope = material(tx, row["scope_id"], row["scope_owner"])
        if row["scope_purpose"] != "supervision_scope" or not isinstance(scope, dict) or scope.get("subject") != row["subject"]:
            raise QueryError("incompatible_run_state", "Invalid recorded supervision scope")
        if row["evidence"] is not None and row["evidence_purpose"] != "github_read_observation":
            raise QueryError("incompatible_run_state", "Invalid recorded supervision observation")
        timeout = timeouts.get(row["subject"])
        timed_out = isinstance(timeout, dict) and timeout.get("scope_id") == row["scope_id"] and timeout.get("kind") == row["kind"]
        command = action_guidance("resume", run_id=run["id"], repository=run["repository"],
            ticket=row["issue"] if row["delivery_kind"] == "ticket" else None, language=run["language"])
        deadline = timestamp(row["deadline"])
        result.append({"subject": row["subject"], "kind": row["kind"], "scope_id": row["scope_id"], "scope": scope,
            "started_at": timestamp(row["started_at"]), "deadline": deadline,
            "remaining_seconds": 0 if timed_out else max(0, int(deadline - now)) if deadline is not None else None,
            "retry_count": row["retry_count"], "next_retry_at": timestamp(row["next_retry_at"]),
            "latest_observation": material(tx, row["evidence"], row["evidence_owner"]),
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
        kind = ("final_approval" if operation == "approve" else "review_budget_checkpoint" if "budget_exhausted" in reason
                else "execution_failure" if reason == "execution_failed" else reason)
        command = (action_guidance(operation, run_id=run["id"], repository=run["repository"],
            ticket=item["issue"] if local and operation == "resume" else None, language=run["language"])
            if operation != "inspect" else "Inspect original facts and diagnostics")
        object_label = f"Ticket #{owner['issue']}" if local and owner else f"Parent Issue #{owner['issue']}" if owner and owner["kind"] == "parent_only" else "Delivery Run"
        item.update({"operation": operation, "scope": "local" if local else "global", "work_subject": item["subject"],
            "location": owner["subject"] if local and owner else "parent" if owner and owner["kind"] == "parent_only" else "run_publication" if owner else "run",
            "phase": public_phase(owner["phase"]) if owner else None,
            "status_code": "parent_approval_pending" if operation == "approve" and owner and owner["kind"] == "parent_only" else
                           "run_approval_pending" if operation == "approve" else reason,
            "action_kind": kind, "type": {"final_approval": "Final Approval", "human_blocker": "Human Blocker",
                "review_budget_checkpoint": "Review Budget Checkpoint", "execution_failure": "Execution Failure",
                "operator_stopped": "Operator Stopped", "supervision_timeout": "Supervision Timeout Pause"}.get(kind, kind),
            "action": {"kind": "command" if operation != "inspect" else "inspect", "command": operation if operation != "inspect" else None,
                       "selector": run["id"], "ticket_number": item.get("issue") if local else None},
            "object": object_label, "role": pending["role"] if pending else None, "run_paused": not local,
            "preserved": "；".join(part for part in (f"Candidate {owner['candidate_sha']}" if owner and owner["candidate_sha"] else "",
                         f"PR #{owner['pr_number']}" if owner and owner["pr_number"] else "") if part) or None,
            "reasons": reasons,
            "evidence": {"operator_gate": {"code": reason, "detail": artifact},
                         "human_blockers": artifact.get("human_blockers") if isinstance(artifact, Mapping) else None,
                         "diagnostics": None, "failure_evidence": artifact if reason != "human_blocker" else None,
                         "execution_failure": artifact if reason == "execution_failed" else None, "blocked_reason": reason},
            "next_action": command})
    return result


def public_phase(phase: str) -> str:
    return {"development": "developing", "review": "reviewing", "publish": "publication_pending",
            "checks": "waiting_checks", "approval": "ready_for_approval", "merge": "merging",
            "close": "closing", "complete": "completed"}.get(phase, phase)


def public_status(run: Mapping[str, Any], subjects: list[dict[str, Any]], terminal: bool, *,
                   abandonment: Mapping[str, Any] | None = None,
                   supervision_windows: Sequence[Mapping[str, Any]] = ()) -> str:
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
    wait_kinds = {window["kind"] for window in supervision_windows if any(
        job["phase"] in {"checks", "merge"} and job["subject"] == window["subject"] for job in current)}
    if "github_convergence" in wait_kinds:
        return "waiting_external"
    if "required_checks" in wait_kinds or any(job["phase"] == "checks" for job in current):
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


def _cleanup(run: Mapping[str, Any], subjects: list[dict[str, Any]]) -> dict[str, Any] | None:
    cleanups = [job["delivery_cleanup"] for job in subjects if job["delivery_cleanup"]]
    if not cleanups:
        return None
    items = [{"kind": job["kind"], "branch": job["delivery_cleanup"]["source_ref"],
              "checkout": job["delivery_cleanup"]["checkout"], "status": "cleanup_pending",
              "last_error": job["delivery_cleanup"]["outcome"],
              "recovery_action": action_guidance("resume", run_id=run["id"], repository=run["repository"],
                                                language=run["language"])}
             for job in subjects if job["delivery_cleanup"] and job["delivery_cleanup"]["state"] != "complete"]
    return {"status": "cleanup_pending" if items else "completed",
            "last_error": next((row["outcome"] for row in cleanups if row["outcome"]), None), "items": items}


def read_boundaries(tx: Transaction, now: float) -> dict[str, Any]:
    """Read current gates and cleanup, leaving audit and receipt expansion to consumers."""
    run = dict(tx.one("SELECT * FROM runs"))
    location = read_header(tx)
    subjects = []
    for job in all_rows(tx, "SELECT id,kind,issue,phase,generation,candidate_sha,pr_number,current_attempt FROM subjects ORDER BY id"):
        subject = job.pop("id")
        gates = all_rows(tx, "SELECT * FROM gates WHERE subject=? ORDER BY code", (subject,))
        for gate in gates:
            gate["detail"] = material(tx, gate["evidence"], subject)
        pending = (dict(tx.one("SELECT role FROM attempts WHERE id=? AND subject=?", (job["current_attempt"], subject)))
                   if job["current_attempt"] else None)
        cleanup = tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))
        subjects.append({**job, "subject": subject, "gates": gates, "pending_attempt": pending,
                         "delivery_cleanup": dict(cleanup[0]) if cleanup else None})
    windows = supervision(tx, now)
    abandonment = location["abandonment"]
    return {"subjects": subjects,
            "actions": _operator_actions(run, subjects, abandonment=abandonment, supervision_windows=windows),
            "supervision_windows": windows,
            "generations": {job["subject"]: job["generation"] for job in subjects},
            "status": public_status(run, subjects, location["is_terminal"], abandonment=abandonment, supervision_windows=windows),
            "is_terminal": location["is_terminal"], "abandonment": abandonment,
            "delivery_cleanup": _cleanup(run, subjects),
            "closures": all_rows(tx, "SELECT e.subject,e.generation,e.id AS effect_id,e.receipt_id,e.pr_number "
                "FROM effects e JOIN subjects s ON s.id=e.subject AND s.generation=e.generation "
                "WHERE s.kind!='ticket' AND e.operation='close' AND e.status='confirmed' ORDER BY e.rowid"),
            "highwater": tx.one("SELECT coalesce(max(position),0) FROM history")[0]}
