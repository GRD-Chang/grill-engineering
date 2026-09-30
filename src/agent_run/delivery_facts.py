"""Shared read-only roles, work ownership and delivery progress. No persisted state."""
from __future__ import annotations

from typing import Any

_TERMINAL_CHANGE_JOB_PHASES = {"completed", "merged", "abandoned"}

_TERMINAL_RUN_ACCEPTANCE_PHASES = {"accepted", "completed"}

_CURRENT_PUBLICATION_GATE_PHASES = {
    "blocked",
    "ready_for_human",
    "publication_pending",
}


def _ticket_execution_activity(invocation: object, executor_activity: str) -> str:
    if not isinstance(invocation, dict) or invocation.get("status") not in {
        "running", "resuming",
    }:
        return "not_running"
    return "interrupted" if executor_activity == "not_running" else executor_activity


def ticket_phase_execution_activity(
    state: dict[str, Any], audit: dict[str, Any], activity: str,
) -> str:
    """Describe an interrupted Ticket preparation before an Invocation exists."""
    if activity != "not_running" or state.get("status") != "active":
        return activity
    current = current_work_subject(state)
    if (
        current is None
        or not current[0].startswith("ticket:")
        or current[1].get("phase") not in {"developing", "repairing"}
    ):
        return activity
    control = audit.get("executor_control")
    observed = control.get("activity") if isinstance(control, dict) else "unknown"
    if observed == "not_running":
        return "interrupted"
    return "unknown" if observed == "unknown" else activity


def ticket_progress(state: dict[str, Any]) -> dict[str, int]:
    graph = state.get("ticket_graph")
    tickets = graph.get("tickets") if isinstance(graph, dict) else None
    total = len(tickets) if isinstance(tickets, dict) else 0
    jobs = state.get("ticket_jobs")
    graph_numbers = set(tickets) if isinstance(tickets, dict) else set()
    completed = (
        sum(
            1
            for number, job in jobs.items()
            if (not graph_numbers or str(number) in graph_numbers)
            if isinstance(job, dict)
            and job.get("phase") == "completed"
        )
        if isinstance(jobs, dict)
        else 0
    )
    return {"completed": completed, "total": total}


def multi_ticket_progress(
    state: dict[str, Any], *, executor_activity: str = "unknown"
) -> dict[str, Any] | None:
    """Project the persisted Ticket queue and local Job facts for status only."""

    if type(state.get("multi_ticket_protocol")) is not int or state["multi_ticket_protocol"] != 4:
        return None
    if state.get("delivery_type") == "parent_only":
        return None
    graph = state.get("ticket_graph")
    tickets = graph.get("tickets") if isinstance(graph, dict) else None
    jobs = state.get("ticket_jobs")
    invocations = state.get("ticket_agent_invocations")
    queue = state.get("ticket_integration_queue")
    ticket_map = tickets if isinstance(tickets, dict) else {}
    job_map = jobs if isinstance(jobs, dict) else {}
    invocation_map = invocations if isinstance(invocations, dict) else {}
    queue_items = queue if isinstance(queue, list) else []
    queued = {
        item.get("ticket_number"): index + 1
        for index, item in enumerate(queue_items)
        if isinstance(item, dict) and type(item.get("ticket_number")) is int
    }
    holder = state.get("ticket_integration_holder")
    work_queue = state.get("ticket_work_queue", [])
    work_waiters = [item["ticket_number"] for item in work_queue if item["lease_id"] is None]
    working = {item["ticket_number"] for item in work_queue if item["lease_id"] is not None}
    details = []
    for key in sorted(ticket_map, key=lambda value: int(value)):
        ticket = ticket_map[key]
        job = job_map.get(key)
        ticket_data = ticket if isinstance(ticket, dict) else {}
        job_data = job if isinstance(job, dict) else {}
        blockers = ticket_data.get("blocked_by")
        unresolved = [
            item.get("number") for item in blockers
            if isinstance(item, dict)
            and type(item.get("number")) is int
            and (
                (
                    isinstance(job_map.get(str(item["number"])), dict)
                    and job_map[str(item["number"])].get("phase") != "completed"
                )
                or (
                    str(item["number"]) not in ticket_map
                    and item.get("state") != "CLOSED"
                )
                or (
                    str(item["number"]) in ticket_map
                    and str(item["number"]) not in job_map
                )
            )
        ] if isinstance(blockers, list) else []
        number = int(key)
        phase = job_data.get("phase") or "pending"
        reason = job_data.get("blocked_reason")
        if not reason and unresolved:
            reason = "waiting_for_dependencies"
        review = job_data.get("review_budget")
        ci = job_data.get("ci_recovery")
        resync = job_data.get("integration_resync")
        job_policy = job_data.get("policy_snapshot")
        policy = job_policy if isinstance(job_policy, dict) else state.get("policy_snapshot")
        policy_data = policy if isinstance(policy, dict) else {}
        review_rounds = policy_data.get("ticket_review_rounds")
        usage = {
            "development_attempts": review.get("development_attempts") if isinstance(review, dict) else None,
            "development_limit": review_rounds + 1 if type(review_rounds) is int else None,
            "reviewer_invocations": review.get("reviewer_invocations") if isinstance(review, dict) else None,
            "review_limit": review_rounds,
            "ci_rounds_used": ci.get("rounds_used") if isinstance(ci, dict) else None,
            "ci_rounds_limit": policy_data.get("ci_recovery_rounds"),
            "ci_repairs_used": ci.get("repairs_used") if isinstance(ci, dict) else None,
            "ci_repairs_limit": (
                policy_data.get("ci_repairs_per_round")
                if isinstance(ci, dict) and ci.get("review_required") is True else 1
            ),
            "integration_resync_used": resync.get("used") if isinstance(resync, dict) else None,
            "integration_resync_limit": resync.get("limit") if isinstance(resync, dict) else policy_data.get("integration_resync_limit"),
        }
        action = None
        if phase == "publication_pending":
            action = "resume"
        elif phase == "blocked":
            if reason in {
                "effective_revision_mismatch", "merged_revision_mismatch"
            }:
                action = "requeue"
            elif reason in {
                "agent_requires_human", "reviewer_requires_human",
                "modification_budget_exhausted", "review_budget_exhausted",
                "ci_inner_budget_exhausted", "ci_outer_budget_exhausted",
                "integration_resync_exhausted",
                "ticket_execution_failed", "ticket_supervision_timeout",
                "ticket_paused",
            }:
                action = "resume"
        parent = state.get("parent")
        parent_number = parent.get("number") if isinstance(parent, dict) else None
        repository = state.get("repository")
        if action is not None and type(parent_number) is int and isinstance(repository, str):
            action = (
                f"agent-run {action} {parent_number} --repo {repository} "
                f"--ticket {number}"
                if action == "resume"
                else f"agent-run requeue {parent_number} --repo {repository}"
            )
        details.append({
            "ticket_number": number,
            "phase": phase,
            "queue_position": queued.get(number),
            "resume_accepted": isinstance(job_data.get("last_ticket_resume"), dict) and number in work_waiters,
            "work_queue_position": work_waiters.index(number) + 1 if number in work_waiters else None,
            "has_work_slot": number in working,
            "holds_integration": number == holder,
            "required_checks": job_data.get("required_checks_evidence"),
            "supervision": _ticket_supervision_snapshot(state, job_data),
            "blocked_reason": reason,
            "pause_requested": number in {
                int(item) for item in state.get("ticket_pause_requests", {})
                if isinstance(item, str) and item.isdecimal()
            },
            "stop_wait_reason": (
                "agent_exit" if number in working else
                "remote_reconciliation" if job_data.get("ticket_write_intent") is not None
                or phase in {"merging", "merged", "waiting_merge"} else "local_boundary"
            ) if str(number) in state.get("ticket_pause_requests", {}) else None,
            "blocked_by": unresolved,
            "usage": usage,
            "recovery_action": action,
            "execution_activity": _ticket_execution_activity(
                invocation_map.get(key), executor_activity
            ),
            "invocation": {
                "role": invocation_map[key].get("role"),
                "status": invocation_map[key].get("status"),
                "started_at": invocation_map[key].get("started_at"),
            } if isinstance(invocation_map.get(key), dict) else None,
        })
    snapshot = state.get("policy_snapshot")
    return {
        "executor_activity": executor_activity,
        "work_concurrency_limit": snapshot.get("ticket_development_workers") if isinstance(snapshot, dict) else None,
        "work_slots_active": len(working) if executor_activity == "running" else 0 if executor_activity == "not_running" else None,
        "integration_queue": [dict(item) for item in queue_items if isinstance(item, dict)],
        "integration_holder": holder,
        "tickets": details,
    }



def role_family(role: str) -> str | None:
    if role == "development":
        return "development"
    if role in {"review", "reviewer", "fresh_acceptance"}:
        return "review"
    if role in {"publication", "final_publication"}:
        return "publication"
    return None



def current_work_subject(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Select the one Work Subject represented by the public status view."""

    active = state.get("active_ticket_job")
    if (
        isinstance(active, dict)
        and active.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        ticket_number = active.get("ticket_number")
        locator = (
            f"ticket:{ticket_number}"
            if isinstance(ticket_number, int)
            else "ticket:unknown"
        )
        return locator, active

    parent = state.get("parent_job")
    if (
        isinstance(parent, dict)
        and parent.get("phase") not in _TERMINAL_CHANGE_JOB_PHASES
    ):
        return "parent", parent

    acceptance = state.get("run_acceptance")
    publication = state.get("run_publication")
    if (
        isinstance(publication, dict)
        and publication.get("phase") in _CURRENT_PUBLICATION_GATE_PHASES
    ):
        return "run_publication", publication

    if (
        isinstance(acceptance, dict)
        and acceptance.get("phase") not in _TERMINAL_RUN_ACCEPTANCE_PHASES
    ):
        repair = acceptance.get("repair_job")
        if acceptance.get("phase") == "repairing" and isinstance(repair, dict):
            return "run_repair", repair
        return "run_acceptance", acceptance

    if isinstance(publication, dict):
        return "run_publication", publication
    if isinstance(acceptance, dict):
        return "run_acceptance", acceptance
    return None



def delivery_object_label(
    state: dict[str, Any],
    locator: str,
    *,
    ticket_number: object = None,
    include_parent_for_run: bool = False,
) -> str:
    """Render one stable label for a delivery work subject or state location."""

    if locator.startswith("ticket:"):
        number = ticket_number or locator.split(":", 1)[1]
        return f"Ticket #{number}"
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if locator == "parent" or locator.startswith("parent-only:"):
        return f"Parent Issue #{parent_number}"
    if locator in {"run_acceptance", "run_repair"} or locator.startswith(
        ("run-acceptance:", "run-repair:")
    ):
        return "Run Acceptance"
    if locator == "run_publication" or locator.startswith("run-publication:"):
        return "Run Publication"
    if include_parent_for_run:
        return f"Delivery Run（Parent Issue #{parent_number}）"
    return "Delivery Run"



def active_ticket_job(state: dict[str, object]) -> dict[str, object] | None:
    active = state.get("active_ticket_job")
    if not isinstance(active, dict):
        return None
    if active.get("phase") in {"merged", "completed", "abandoned"}:
        return None
    return active



def current_delivery_identity(state: dict[str, object]) -> dict[str, object]:
    """Return the Candidate/PR boundary operators need for diagnosis."""

    active = active_ticket_job(state)
    if active is not None:
        return {
            "candidate_sha": active.get("candidate_sha"),
            "pr_number": active.get("pr_number"),
        }
    parent = state.get("parent_job")
    if isinstance(parent, dict) and parent.get("phase") not in {
        "completed",
        "merged",
        "abandoned",
    }:
        return {
            "candidate_sha": parent.get("candidate_sha"),
            "pr_number": parent.get("pr_number"),
        }
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            return {
                "candidate_sha": repair.get("candidate_sha"),
                "pr_number": repair.get("pr_number"),
            }
        candidate = acceptance.get("reviewed_head_sha") or acceptance.get(
            "candidate_sha"
        )
        publication = state.get("run_publication")
        pr_number = publication.get("pr_number") if isinstance(publication, dict) else None
        if candidate is not None or pr_number is not None:
            return {"candidate_sha": candidate, "pr_number": pr_number}
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        record = publication.get("record")
        return {
            "candidate_sha": (
                record.get("run_head_sha")
                if isinstance(record, dict)
                else publication.get("head_sha")
            ),
            "pr_number": publication.get("pr_number"),
        }
    return {"candidate_sha": None, "pr_number": None}


def _ticket_supervision_snapshot(state: dict[str, Any], job: dict[str, Any]) -> dict[str, object] | None:
    from agent_run.external_supervision import public_supervision_snapshot

    wait = job.get("external_wait")
    if not isinstance(wait, dict) and not isinstance(job.get("supervision_wait"), dict):
        return None
    projected = {**state, **(wait or {}), "active_ticket_job": job,
                 "supervision_wait": job.get("supervision_wait")}
    return public_supervision_snapshot(projected)
