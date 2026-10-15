"""Independent Job conditions and their current dictionary projection."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_change_job_codec import present
from agent_run.sqlite_change_job_schema import JOB_FIELDS
from agent_run.sqlite_encoding import encode_json


CURRENTNESS_REASONS = frozenset({
    "change_pr_head_changed_externally", "change_pr_base_changed_externally",
    "change_pr_closed_or_merged_externally", "change_pr_base_unknown",
    "candidate_or_acceptance_inconsistent",
})
_PHASES = frozenset({"blocked", "paused", "ready_for_human", "execution_failed", "publication_pending"})
_RESUME_FIELDS = ("execution_resume_phase", "pause_resume_phase", "human_blocker_phase")


def gate(job: Mapping[str, Any]) -> tuple[str, str | None, str, str | None, str | None, int] | None:
    phase = job.get("phase")
    if phase not in _PHASES:
        return None
    reason = job.get("blocked_reason")
    reason = reason if isinstance(reason, str) else None
    kind: str
    resume: str | None
    priority: int
    # A retained continuation describes work, not the identity of a newly
    # observed condition. Classify concrete reasons before legacy fields.
    if phase == "publication_pending":
        kind, resume, priority = "publication", "execution_resume_phase" if "execution_resume_phase" in job else None, 40
    elif reason in CURRENTNESS_REASONS:
        kind, resume, priority = "currentness", "pause_resume_phase", 50
    elif reason in {"agent_requires_human", "reviewer_requires_human", "publication_requires_human"}:
        kind, resume, priority = "human", "human_blocker_phase", 30
    elif reason and ("budget" in reason or "exhausted" in reason):
        kind, resume, priority = "budget", next((key for key in _RESUME_FIELDS if key in job), None), 20
    elif reason in {"ticket_paused", "ci_investigation_paused"} or phase == "paused":
        kind, resume, priority = "pause", "pause_resume_phase", 10
    elif "execution_resume_phase" in job or reason == "execution_failure" or phase == "execution_failed":
        kind, resume, priority = "execution", "execution_resume_phase", 40
    elif "human_blocker_phase" in job:
        kind, resume, priority = "human", "human_blocker_phase", 30
    else:
        kind, resume, priority = "condition", None, 15
    pending = job.get("pending_semantic_attempt")
    attempt = pending.get("attempt_id") if isinstance(pending, Mapping) and kind in {"execution", "human"} else None
    return kind, reason, phase, resume, attempt if isinstance(attempt, str) else None, priority


def save_gate(c: sqlite3.Connection, run_id: str, job_id: str, job: Mapping[str, Any], old: Mapping[str, Any], stage: Any) -> Any:
    before, after = gate(old), gate(job)
    if before is not None and after is None:
        # Removing the visible condition grants no authority over a hidden
        # condition, including another condition of this same kind.
        c.execute("DELETE FROM work_job_gates WHERE run_id=? AND job_id=? AND gate_kind=? AND reason IS ? AND attempt_id IS ?",
                  (run_id, job_id, before[0], before[1], before[4]))
    if after is not None:
        identity = hashlib.sha256(encode_json(after[:2] + (after[4],)).encode()).hexdigest()
        c.execute("INSERT INTO work_job_gates VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,job_id,gate_id) "
                  "DO UPDATE SET public_phase=excluded.public_phase,resume_field=excluded.resume_field,priority=excluded.priority",
                  (run_id, job_id, identity, *after))
        for key in (after[3], *_RESUME_FIELDS):
            saved = job.get(key) if key is not None else None
            if isinstance(saved, str) and saved not in _PHASES:
                return saved
        return stage
    if c.execute("SELECT 1 FROM work_job_gates WHERE run_id=? AND job_id=? LIMIT 1", (run_id, job_id)).fetchone():
        return stage
    return job.get("phase", stage)


def load_gate(c: sqlite3.Connection, run_id: str, job_id: str, job: dict[str, Any], stage: Any) -> None:
    row = c.execute("SELECT reason,public_phase,resume_field FROM work_job_gates WHERE run_id=? AND job_id=? "
                    "ORDER BY priority DESC,gate_id LIMIT 1", (run_id, job_id)).fetchone()
    if row is None:
        return
    if row[1] not in _PHASES or row[2] is not None and row[2] not in _RESUME_FIELDS:
        raise ValueError("Invalid Change Job gate phase or continuation")
    job["phase"] = row[1]
    if row[0] is not None:
        job["blocked_reason"] = row[0]
    if row[2] is not None and isinstance(stage, str) and stage not in _PHASES:
        job[row[2]] = stage
    elif isinstance(stage, str) and stage not in _PHASES:
        job["pause_resume_phase"] = stage


def gate_projection(c: sqlite3.Connection, run_id: str, job_id: str) -> dict[str, Any]:
    """Read one owner's stage and highest condition without any evidence body."""
    row = c.execute("SELECT presence,stage FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None:
        raise ValueError("Missing Change Job gate owner")
    result = {"phase": row[1]} if "phase" in present(row[0], JOB_FIELDS) else {}
    load_gate(c, run_id, job_id, result, row[1])
    return result


def retains_run_control(state: Mapping[str, Any]) -> bool:
    return state.get("status") in {
        "operator_stopped", "execution_failed", "supervision_timeout", "deterministic_contradiction",
        "unsupported_scope_change", "requeue_required", "abandonment_pending", "abandoned", "completed",
    }


def reconcile_selected_gate(state: dict[str, Any], changed_owners: set[int]) -> set[str]:
    """Mirror a revealed gate only for the selected owner, preserving Run control."""
    if not changed_owners or retains_run_control(state):
        return set()
    active = state.get("active_ticket_job")
    selected: dict[str, Any] | None
    if isinstance(active, dict):
        selected = active
    else:
        acceptance = state.get("run_acceptance")
        repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        candidates = [state.get("parent_job"), repair, acceptance, state.get("run_publication")]
        selected = next((owner for owner in candidates if isinstance(owner, dict) and id(owner) in changed_owners
                         and owner.get("phase") in _PHASES), None)
    if not isinstance(selected, dict) or id(selected) not in changed_owners or selected.get("phase") not in _PHASES:
        return set()
    if selected["phase"] == "publication_pending":
        state.update(status="publication_pending", terminal_kind="publication_pending")
        return {"status", "terminal_kind"}
    reason = selected.get("blocked_reason")
    state.update(status="ready_for_human" if selected["phase"] == "ready_for_human" else "blocked",
                 terminal_kind="waiting_human" if selected["phase"] == "ready_for_human" else None,
                 diagnostics=[{"code": reason or "job_gate_retained", "message": "Retained Job condition requires explicit recovery"}])
    return {"status", "terminal_kind", "diagnostics"}
