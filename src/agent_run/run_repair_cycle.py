from __future__ import annotations

"""Repair Cycle state, counters, and terminal transitions."""

from typing import Any


_ACTIVE_CHECKOUT_PHASES = frozenset(
    {
        "developing",
        "repairing",
        "committing_candidate",
        "candidate",
        "reviewing",
        "accepted",
        "publishing",
        "publication_pending",
        "waiting_checks",
        "waiting_merge",
        "merging",
    }
)


def start_repair_cycle(run: dict[str, Any], generation: int) -> None:
    run["modification_attempts"] = 0
    run["code_modification_attempts"] = 0
    run["repair_cycle"] = {
        "generation": generation,
        "code_modification_attempts": 0,
        "validation_attempts": 0,
        "status": "active",
    }


def repair_checkout_is_active(job: dict[str, Any]) -> bool:
    phase = job.get("phase")
    if phase in _ACTIVE_CHECKOUT_PHASES:
        return True
    return phase == "blocked" and job.get("blocked_reason") in {
        "agent_requires_human",
        "reviewer_requires_human",
    }


def sync_repair_cycle_counters(
    run: dict[str, Any], job: dict[str, Any]
) -> None:
    cycle = run.get("repair_cycle")
    if not isinstance(cycle, dict):
        return
    modifications = int(job.get("modification_attempts", 0))
    validations = int(job.get("validation_attempts", 0))
    cycle.update(
        {
            "code_modification_attempts": modifications,
            "validation_attempts": validations,
        }
    )
    thread_id = job.get("development_thread_id")
    if isinstance(thread_id, str):
        cycle["development_thread_id"] = thread_id
    checkout = job.get("repair_checkout")
    if isinstance(checkout, str):
        cycle["worktree"] = checkout
    run["code_modification_attempts"] = modifications
    run["modification_attempts"] = modifications


def rotate_repair_job(
    run: dict[str, Any],
    job: dict[str, Any],
    *,
    candidate_sha: str,
    base_sha: str,
    repair_branch: str,
    repair_job_attempt: int,
    modification_attempt: int,
    repair_checkout: str,
) -> dict[str, Any]:
    """Replace an integrated Job while preserving its active Repair Cycle."""

    completed_repairs = run.setdefault("completed_repair_jobs", [])
    if not isinstance(completed_repairs, list):
        raise ValueError("completed_repair_jobs must be a list")
    completed_repairs.append(
        {
            "phase": "completed",
            "repair_branch": job["repair_branch"],
            "pr_number": job["pr_number"],
            "integrated_sha": job["integrated_sha"],
            "candidate_sha": job["candidate_sha"],
            "acceptance_state": "revalidation_finding",
        }
    )
    del completed_repairs[:-32]

    rotated = dict(job)
    trigger = rotated.get("repair_trigger")
    if isinstance(trigger, dict):
        trigger = dict(trigger)
        trigger["head_sha"] = base_sha
        rotated["repair_trigger"] = trigger
    for key in (
        "acceptance_record",
        "publication",
        "publication_sha",
        "published_sha",
        "integrated_sha",
        "integrated_publication_sha",
        "pr_number",
        "merge_intent",
        "merge_reconciliation_history",
        "ticket_write_intent",
        "linked_branch_display",
        "pending_attempt",
    ):
        rotated.pop(key, None)
    rotated.update(
        {
            "phase": "candidate",
            "repair_attempt": int(job["repair_attempt"]) + 1,
            "repair_job_attempt": repair_job_attempt,
            "repair_branch": repair_branch,
            "base_sha": base_sha,
            "repair_base_run_head_sha": base_sha,
            "candidate_sha": candidate_sha,
            "repair_source": "acceptance",
            "repair_input_artifact": _mapping(job, "acceptance_artifact"),
            "modification_attempts": modification_attempt,
            "code_modification_attempts": modification_attempt,
            "publication_attempts": 0,
            "repair_checkout": repair_checkout,
            "repair_job_rotation": {
                "current_branch": str(job["repair_branch"]),
                "next_branch": repair_branch,
                "candidate_sha": candidate_sha,
            },
        }
    )
    run["repair_job"] = rotated
    sync_repair_cycle_counters(run, rotated)
    return rotated


def escalate_repair(
    state: dict[str, Any], run: dict[str, Any], job: dict[str, Any], code: str
) -> None:
    job["blocked_reason"] = code
    if code == "modification_budget_exhausted":
        cycle = run.get("repair_cycle")
        if isinstance(cycle, dict):
            cycle.update({"status": "budget_exhausted", "ended_reason": code})
    run.update({"phase": "ready_for_human", "blocked_reason": code})
    state["status"] = "ready_for_human"
    state["terminal_kind"] = "waiting_human"
    state["diagnostics"] = [
        {
            "code": code,
            "message": "Run Repair requires explicit human intervention",
            "change_job": f"run-repair-{job['repair_attempt']}",
        }
    ]


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
