"""Interpret retained work, independently of its operator pause or container.

These are read-only projections of the existing phase, Semantic Attempt and
integration scene. They never become a second persisted lifecycle.
"""
from __future__ import annotations

from typing import Any, NamedTuple


class InvocationOwner(NamedTuple):
    owner: dict[str, Any]
    publication_mirror: dict[str, Any] | None = None


def locate_invocation_owner(
    state: dict[str, Any], invocation: dict[str, Any],
) -> InvocationOwner:
    """Borrow the exact current owner of an Invocation's bound work.

    This does not select work, check its phase or authorize continuation. The
    Invocation's original Attempt version need not equal the current version.
    Only Ticket Publication exposes a distinct selected mirror for its caller.
    """
    subject = invocation.get("work_subject")
    generation = invocation.get("generation")
    role = invocation.get("role")
    run_id = state.get("run_id")
    if role == "reviewer":
        if subject != f"run-acceptance:{run_id}":
            raise ValueError("Run Acceptance Invocation work_subject is invalid")
        owner = state.get("run_acceptance")
        if not isinstance(owner, dict):
            raise ValueError("current Run Acceptance job is missing")
        if type(generation) is not int or generation < 1:
            raise ValueError("current Agent Invocation generation is invalid")
        current_generation = owner.get("acceptance_generation")
    else:
        publication = role in {"publication", "final_publication"}
        if publication:
            if not isinstance(subject, str) or not subject.strip():
                raise ValueError("current Publication Invocation work_subject is missing")
            if type(generation) is not int or generation < 1:
                raise ValueError("current Publication Invocation generation is invalid")
        elif not isinstance(subject, str) or type(generation) is not int or generation < 1:
            raise ValueError("current Change Job Invocation identity is invalid")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Delivery Run ID is missing")
        family = "Publication" if publication else "Change Job"
        label = "Publication job" if publication else "Change Job"
        if role == "final_publication":
            if subject != f"run-publication:{run_id}":
                raise ValueError("Final Publication Invocation work_subject is invalid")
            owner = state.get("run_publication")
            if not isinstance(owner, dict):
                raise ValueError("current Final Publication job is missing")
            acceptance = state.get("run_acceptance")
            current_generation = (
                acceptance.get("acceptance_generation", 1) if isinstance(acceptance, dict) else None
            )
        elif subject.startswith("ticket:"):
            ticket_text = subject.removeprefix("ticket:")
            if not ticket_text.isdigit() or str(int(ticket_text)) != ticket_text:
                prefix = "Ticket Publication" if publication else "current Ticket Change Job"
                raise ValueError(f"{prefix} Invocation work_subject is invalid")
            jobs = state.get("ticket_jobs")
            owner = jobs.get(ticket_text) if isinstance(jobs, dict) else None
            if not isinstance(owner, dict) or owner.get("ticket_number") != int(ticket_text):
                raise ValueError(f"current Ticket {label} is missing")
            current_generation = owner.get("ticket_branch_generation")
        elif subject == f"parent-only:{run_id}":
            owner = state.get("parent_job")
            if not isinstance(owner, dict):
                raise ValueError(f"current Parent-only {label} is missing")
            current_generation = owner.get("parent_generation", 1)
        elif subject == f"run-repair:{run_id}":
            acceptance = state.get("run_acceptance")
            owner = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
            if not isinstance(owner, dict):
                raise ValueError(f"current Run Repair {label} is missing")
            current_generation = owner.get("repair_generation")
        elif subject == f"run-integration:{run_id}" and role == "development":
            owner = state.get("run_acceptance")
            if not isinstance(owner, dict):
                raise ValueError("current Run Acceptance job is missing")
            current_generation = owner.get("acceptance_generation")
        else:
            raise ValueError(f"{family} Invocation work_subject is invalid")
    if type(current_generation) is not int or generation != current_generation:
        raise ValueError("Publication Invocation generation is stale")
    if role == "publication" and subject.startswith("ticket:"):
        active = state.get("active_ticket_job")
        if (
            isinstance(active, dict)
            and active.get("ticket_number") == owner["ticket_number"]
            and active is not owner
        ):
            return InvocationOwner(owner, active)
    return InvocationOwner(owner)


class TicketDispatchJob(dict[str, Any]):
    """Read-only scheduling fields and readiness derived by this query.

    Readiness does not retain the necessary returns that its query validated.
    This view carries no revision or baseline granting permission to save or
    execute, and never materializes omitted histories.
    """

    def __init__(self, fields: dict[str, Any], *, program_ready: bool) -> None:
        super().__init__(fields)
        self.program_ready = program_ready


INTEGRATION_SCENE_KEYS = (
    "integration_conflict", "parent_integration_conflict",
    "run_repair_integration_conflict", "run_integration_conflict",
)


def integration_scene(subject: dict[str, Any]) -> dict[str, Any] | None:
    return next((subject[key] for key in INTEGRATION_SCENE_KEYS
                 if isinstance(subject.get(key), dict)), None)


def work_phase(subject: dict[str, Any]) -> str | None:
    """A gate is not business progress; retain its saved continuation phase."""
    phase = subject.get("phase")
    if phase in {"blocked", "ready_for_human"}:
        for key in ("execution_resume_phase", "pause_resume_phase", "human_blocker_phase"):
            saved = subject.get(key)
            if isinstance(saved, str) and saved not in {"blocked", "ready_for_human"}:
                return saved
    return phase if isinstance(phase, str) else None


def work_role(subject: dict[str, Any], *, location: str | None = None) -> str | None:
    """Resolve pending work before considering a stage's usual successor."""
    scene = integration_scene(subject)
    attempt = subject.get("pending_semantic_attempt")
    if isinstance(attempt, dict) and attempt.get("status") == "pending":
        returned = attempt.get("returned_result")
        if isinstance(returned, dict):
            from agent_run.agent_return import agent_return_requires_human
            if not agent_return_requires_human(returned):
                return None
        role = attempt.get("role")
        if role == "development" and scene is not None and scene.get("development_summary"):
            return None
        if role in {"development", "reviewer", "publication"}:
            return str(role)
    if scene is not None:
        # Returned Development belongs to deterministic Git continuation.
        return None if scene.get("development_summary") else "development"
    phase = work_phase(subject)
    if subject.get("blocked_reason") == "reviewer_requires_human":
        return "reviewer"
    if phase in {"developing", "repairing", "committing_candidate"}:
        return "development"
    if phase in {"candidate", "reviewing", "validating"}:
        return "reviewer"
    if location == "run_publication":
        # A returned artifact continues through GitHub reconciliation, checks
        # and approval without another Publication Agent invocation.
        if phase in {"pending", "publishing"} and not isinstance(subject.get("artifact"), dict):
            return "publication"
        return None
    if phase in {"publishing", "publication_pending"}:
        return "publication"
    if location == "run_acceptance" and phase == "pending":
        return "reviewer"
    if location == "run_repair" and phase == "accepted":
        return None
    if phase == "accepted" or (
        phase == "pending" and subject.get("phase") in {"blocked", "ready_for_human"}
        and subject.get("human_blocker_phase") == "pending"
    ):
        return "publication"
    return None


def work_attempt(subject: dict[str, Any], role: str | None) -> int | None:
    if role is None:
        return None
    attempt = subject.get("pending_semantic_attempt")
    if isinstance(attempt, dict) and attempt.get("status") == "pending" and attempt.get("role") == role:
        ordinal = attempt.get("ordinal")
        if isinstance(ordinal, int):
            return ordinal
    if role == "development":
        value = (subject.get("integration_conflict_agent_ordinal")
                 if integration_scene(subject) is not None
                 else subject.get("pending_attempt", subject.get("modification_attempts")))
    elif role == "reviewer":
        value = subject.get("validation_attempts")
    elif role == "publication":
        value = subject.get("publication_attempts")
    else:
        return None
    return value if isinstance(value, int) else None


def work_identity(state: dict[str, Any], location: str, subject: dict[str, Any]) -> str:
    """The actual work identity may differ from its storage location."""
    if location == "run_acceptance" and isinstance(subject.get("run_integration_conflict"), dict):
        return f"run-integration:{state['run_id']}"
    prefix = {"parent": "parent-only", "run_repair": "run-repair",
              "run_acceptance": "run-acceptance", "run_publication": "run-publication"}.get(location)
    return location if location.startswith("ticket:") else f"{prefix or location}:{state['run_id']}"


def ticket_program_ready(job: dict[str, Any]) -> bool:
    """Whether saved Ticket results need deterministic consumption first.

    This does not choose the next role or bypass a gate. The Ticket stage
    still owns currentness validation and the allowed business successor.
    """
    if isinstance(job, TicketDispatchJob):
        return job.program_ready
    from agent_run.agent_return import has_returned_agent_result

    return (
        has_returned_agent_result(job)
        or job.get("phase") == "committing_candidate"
        or isinstance(job.get("pending_review_result"), dict)
    )
