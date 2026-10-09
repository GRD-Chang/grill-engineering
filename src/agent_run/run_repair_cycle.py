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
    }
)


def uses_merge_resolution(job: dict[str, Any]) -> bool:
    mode = job.get("repair_mode")
    if not isinstance(mode, str) or mode not in {"merge_resolution", "squash"}:
        raise ValueError("invalid Run Repair mode")
    return mode == "merge_resolution"


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
    return phase in _ACTIVE_CHECKOUT_PHASES or (
        phase == "blocked"
        and (job.get("blocked_reason") in {"ci_investigation_paused", "run_branch_write_failed", "run_branch_write_unknown"} or (
            job.get("blocked_reason") == "agent_requires_human"
            and (
                job.get("human_blocker_phase") in {"developing", "repairing"}
                or (
                    job.get("human_blocker_phase") == "accepted"
                    and isinstance(job.get("run_repair_integration_conflict"), dict)
                )
            )
        ))
    )


def pause_human_blocked_repair_cycle(
    run: dict[str, Any], job: dict[str, Any]
) -> None:
    """Pause the current Cycle until the same blocked Attempt is resumed."""

    reason = job.get("blocked_reason")
    if job.get("phase") != "blocked" or reason not in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        return
    cycle = run.get("repair_cycle")
    if isinstance(cycle, dict):
        cycle.update({"status": "human_blocked", "ended_reason": reason})
    run["phase"] = "ready_for_human"


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


def escalate_repair(
    state: dict[str, Any], run: dict[str, Any], job: dict[str, Any], code: str
) -> None:
    job["blocked_reason"] = code
    if code in {
        "modification_budget_exhausted",
        "ci_development_budget_exhausted",
    }:
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


def remove_repair_job(run: dict[str, Any]) -> None:
    """Remove repair work while retaining the Parent's fixed budgets."""
    run.pop("repair_job", None)
