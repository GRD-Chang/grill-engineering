"""Continue paused work from durable facts, independently of failure labels."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.operator_gate import operator_gate_subjects
from agent_run.ticket_phase import TicketPhase, sync_active_ticket_job


def paused_work(state: dict[str, Any], ticket_number: int | None = None
                ) -> list[tuple[str, dict[str, Any]]]:
    if state.get("status") in {
        "completed", "abandoned", "abandonment_pending", "unsupported_scope_change",
        "requeue_required", "run_approval_pending", "parent_approval_pending",
    }:
        return []
    return [
        (location, subject) for location, subject in operator_gate_subjects(state)
        if subject.get("phase") in {"blocked", "ready_for_human"}
        and (ticket_number is None or location == f"ticket:{ticket_number}")
    ]


def resume_message_owner(state: dict[str, Any], ticket_number: int | None = None
                         ) -> tuple[str, dict[str, Any], int] | None:
    """Resolve context between Agent calls using the current delivery object."""
    from agent_run.resume_intent import is_ticket_execution_recovery
    executor_recovery = ticket_number is None and is_ticket_execution_recovery(state, state.get("status"))
    paused = [] if executor_recovery else paused_work(state, ticket_number)
    if len(paused) > 1:
        raise ValueError("resume message requires an unambiguous work subject; use --ticket")
    acceptance = state.get("run_acceptance", {})
    owner: object
    if executor_recovery:
        from agent_run.ticket_queue import next_ticket
        number = next_ticket(state, state.get("frontier", []))
        if number is None:
            return None
        location = f"ticket:{number}"
        owner = state["ticket_jobs"].get(str(number), {"ticket_number": number})
    elif paused:
        location, owner = paused[0]
    elif ticket_number is not None:
        location, owner = f"ticket:{ticket_number}", state.get("ticket_jobs", {}).get(str(ticket_number))
    elif isinstance(state.get("parent_job"), dict):
        location, owner = "parent", state["parent_job"]
    elif isinstance(state.get("active_ticket_job"), dict) and state["active_ticket_job"].get("phase") != "completed":
        owner = state["active_ticket_job"]
        location = f"ticket:{state['active_ticket_job']['ticket_number']}"
    elif isinstance(acceptance.get("repair_job"), dict) and acceptance.get("phase") == "repairing":
        location, owner = "run_repair", acceptance["repair_job"]
    elif isinstance(state.get("run_publication"), dict) and acceptance.get("phase") == "accepted":
        location, owner = "run_publication", state["run_publication"]
    else:
        location, owner = "run_acceptance", state.get("run_acceptance")
    if not isinstance(owner, dict):
        return None
    prefix = {"parent": "parent-only", "run_repair": "run-repair",
              "run_acceptance": "run-acceptance", "run_publication": "run-publication"}.get(location)
    subject = location if prefix is None else f"{prefix}:{state['run_id']}"
    generation_key = {"parent": "parent_generation", "run_repair": "repair_generation",
                      "run_acceptance": "acceptance_generation"}.get(location, "ticket_branch_generation")
    generation = owner.get(generation_key, 1)
    if location == "run_publication":
        generation = owner.get("human_response_generation", acceptance.get("acceptance_generation", 1))
    return subject, owner, int(generation)


def continuation_phase(location: str, subject: dict[str, Any]) -> str:
    """Require a saved operation or pending role; never initialize missing work."""
    for key in ("execution_resume_phase", "pause_resume_phase", "human_blocker_phase"):
        phase = subject.get(key)
        if isinstance(phase, str) and phase not in {"blocked", "ready_for_human"}:
            allowed = ({"pending", "reviewing", "repairing", "accepted"} if location == "run_acceptance"
                       else {"pending", "publishing", "waiting_checks", "merging", "merged"} if location == "run_publication"
                       else {p.value for p in TicketPhase} - {"blocked", "completed", "publication_pending"})
            if phase not in allowed:
                raise ValueError(f"{location}: invalid saved continuation phase {phase}")
            return phase
    attempt = subject.get("pending_semantic_attempt")
    if isinstance(attempt, dict) and attempt.get("status") == "pending":
        role = attempt.get("role")
        if role == "development":
            if location == "run_acceptance":
                return "pending"
            if any(isinstance(subject.get(key), dict) for key in (
                "integration_conflict", "parent_integration_conflict", "run_repair_integration_conflict",
            )):
                return "candidate" if subject.get("attempt_kind") == "ci_repair" else "accepted"
            return "repairing" if subject.get("repair_source") else "developing"
        if role == "reviewer":
            return "reviewing"
        if role == "publication":
            return "publishing" if location == "run_publication" else "accepted"
    # Completed Agent work is consumed by the normal deterministic stage.
    if location not in {"run_acceptance", "run_publication"}:
        if subject.get("candidate_sha"):
            return "accepted" if isinstance(subject.get("acceptance_record"), dict) else "candidate"
    raise ValueError(f"{location}: missing saved continuation phase, pending work or Candidate; "
                     "verify external results and supply the missing work facts before resume")


def resume_paused_work(state: dict[str, Any], *, ticket_number: int | None) -> bool:
    subjects = paused_work(state, ticket_number)
    if not subjects:
        return False
    if len(subjects) != 1:
        raise ValueError("resume 暂停对象不唯一；请使用 --ticket 指定对象")
    location, subject = subjects[0]
    phase = continuation_phase(location, subject)
    subject.setdefault("pause_history", []).append(deepcopy({
        key: subject.get(key) for key in (
            "phase", "blocked_reason", "execution_failure", "failure_evidence",
            "human_blockers", "execution_resume_phase", "pause_resume_phase",
        )
    }) | {"diagnostics": deepcopy(state.get("diagnostics", []))})
    subject["phase"] = phase
    subject.pop("blocked_reason", None)
    subject.pop("execution_resume_phase", None)
    subject.pop("pause_resume_phase", None)
    if location.startswith("ticket:"):
        number = subject["ticket_number"]
        state["active_ticket_job"] = subject
        state["active_agent_invocation"] = state.get("ticket_agent_invocations", {}).get(str(number))
        if phase in {"accepted", "ready_for_integration", "publishing", "waiting_checks",
                     "waiting_merge", "merging", "merged"}:
            from agent_run.ticket_queue import enqueue
            if phase != "ready_for_integration" and state["ticket_integration_holder"] != number:
                subject["integration_resume_phase"] = phase
            enqueue(state, subject)
        sync_active_ticket_job(state)
        status = "active"
    elif location == "parent":
        status = "parent_delivery_pending"
    elif location == "run_publication":
        status = "run_publication_pending"
    else:
        status = "run_acceptance_pending"
        if location == "run_repair":
            state["run_acceptance"]["phase"] = "repairing"
    state.update(status=status, terminal_kind=None, diagnostics=[])
    return True
