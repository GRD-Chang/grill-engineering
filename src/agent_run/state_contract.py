from __future__ import annotations

import re
from typing import Any

from agent_run.ticket_phase import TicketPhase


_CHANGE_JOB_PHASES = frozenset(phase.value for phase in TicketPhase)


class IncompatibleRunStateError(ValueError):
    """A persisted Run predates the one supported Invocation/Generation shape."""


def require_current_run_state(state: dict[str, Any]) -> None:
    """Reject non-canonical persisted Runs before they are read or mutated."""

    if "schema_version" in state:
        raise IncompatibleRunStateError(
            "legacy state is incompatible with the Invocation/Generation contract"
        )
    for key, expected in (
        ("run_id", str),
        ("repository", str),
        ("parent", dict),
        ("base", dict),
        ("ticket_graph", dict),
        ("ticket_jobs", dict),
        ("retired_ticket_generations", dict),
        ("pending_ticket_retirements", dict),
        ("frontier", list),
        ("timeline", list),
        ("agent_invocation_history", list),
        ("diagnostics", list),
        ("status", str),
    ):
        if not isinstance(state.get(key), expected):
            raise IncompatibleRunStateError(
                f"legacy state is missing canonical {key}"
            )
    _require_parent(state["parent"])
    _require_base(state["base"])
    _require_ticket_graph(state["ticket_graph"])
    _require_human_blocker_containers(state)
    if not all(isinstance(ticket, int) for ticket in state["frontier"]):
        raise IncompatibleRunStateError("legacy state has an invalid frontier")
    if not all(isinstance(event, dict) for event in state["timeline"]):
        raise IncompatibleRunStateError("legacy state has an invalid timeline")
    if "active_agent_invocation" not in state:
        raise IncompatibleRunStateError(
            "legacy state is missing canonical active_agent_invocation"
        )
    active = state["active_agent_invocation"]
    if active is not None and not isinstance(active, dict):
        raise IncompatibleRunStateError(
            "legacy state has an invalid active Agent Invocation"
        )
    if isinstance(active, dict):
        _require_invocation(active, "active_agent_invocation", str(state["run_id"]))
        if active["status"] != "completed":
            _require_active_invocation_identity(state, active)
    for index, invocation in enumerate(state["agent_invocation_history"]):
        if not isinstance(invocation, dict):
            raise IncompatibleRunStateError(
                "legacy state has an invalid agent_invocation_history entry"
            )
        _require_invocation(
            invocation, f"agent_invocation_history[{index}]", str(state["run_id"])
        )
    active_ticket = state.get("active_ticket_job")
    if active_ticket is not None and not isinstance(active_ticket, dict):
        raise IncompatibleRunStateError(
            "legacy state has an invalid active Ticket Job"
        )
    for key in (
        "accepted_ticket_graph_revision",
        "accepted_parent_spec_revision",
    ):
        if key not in state:
            raise IncompatibleRunStateError(
                f"legacy state is missing canonical {key}"
            )
    accepted_graph = state["accepted_ticket_graph_revision"]
    accepted_parent = state["accepted_parent_spec_revision"]
    if state.get("base_resolution_pending") is True:
        if accepted_graph is not None or accepted_parent is not None:
            raise IncompatibleRunStateError(
                "initializing state has invalid currentness boundaries"
            )
        return
    if not isinstance(accepted_graph, str) or not isinstance(accepted_parent, str):
        if state.get("currentness_resolution_pending") is True and (
            accepted_graph is None and accepted_parent is None
        ):
            return
        raise IncompatibleRunStateError(
            "legacy state lacks accepted currentness boundaries"
        )
    if not isinstance(state["parent"].get("revision"), str):
        raise IncompatibleRunStateError("legacy state has an invalid parent.revision")
    if not isinstance(state["ticket_graph"].get("revision"), str):
        raise IncompatibleRunStateError(
            "legacy state has an invalid ticket_graph.revision"
        )


def human_blocker_subject_count(state: dict[str, Any]) -> int:
    """Count current top-level Human Blocker subjects without choosing one."""

    subjects: list[dict[str, Any]] = []
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        subjects.extend(job for job in ticket_jobs.values() if isinstance(job, dict))
    for key in ("parent_job", "run_acceptance", "run_publication"):
        value = state.get(key)
        if isinstance(value, dict):
            subjects.append(value)
            if key == "run_acceptance":
                repair = value.get("repair_job")
                if isinstance(repair, dict):
                    subjects.append(repair)
    return sum(1 for subject in subjects if _is_human_blocker(subject))


def _require_human_blocker_containers(state: dict[str, Any]) -> None:
    active = state.get("active_ticket_job")
    if active is not None:
        if not isinstance(active, dict) or not isinstance(active.get("ticket_number"), int):
            raise IncompatibleRunStateError(
                "legacy state has an invalid active_ticket_job"
            )
    for subject in _human_blocker_subjects(state):
        _require_human_blocker(subject)


def _human_blocker_subjects(state: dict[str, Any]) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = []
    subjects.extend(
        job for job in state["ticket_jobs"].values() if isinstance(job, dict)
    )
    for key in ("parent_job", "run_acceptance", "run_publication"):
        value = state.get(key)
        if isinstance(value, dict):
            subjects.append(value)
            if key == "run_acceptance" and isinstance(value.get("repair_job"), dict):
                subjects.append(value["repair_job"])
    return subjects


def _require_human_blocker(subject: dict[str, Any]) -> None:
    reason = subject.get("blocked_reason")
    blockers = subject.get("human_blockers")
    if subject.get("phase") not in {"blocked", "ready_for_human"}:
        return
    if reason not in {"agent_requires_human", "reviewer_requires_human"} and not isinstance(
        blockers, list
    ):
        return
    if reason is not None and reason not in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        raise IncompatibleRunStateError("legacy state has an invalid Human Blocker phase")
    if not isinstance(blockers, list) or not 1 <= len(blockers) <= 8 or not all(
        isinstance(blocker, str) and blocker and len(blocker) <= 2000
        for blocker in blockers
    ):
        raise IncompatibleRunStateError("legacy state has invalid Human Blocker strings")


def _require_parent(parent: dict[str, Any]) -> None:
    if not isinstance(parent.get("number"), int):
        raise IncompatibleRunStateError("legacy state has an invalid parent.number")
    for key in ("title", "revision"):
        if parent.get(key) is not None and not isinstance(parent.get(key), str):
            raise IncompatibleRunStateError(f"legacy state has an invalid parent.{key}")


def _require_base(base: dict[str, Any]) -> None:
    for key in ("branch", "sha"):
        if not isinstance(base.get(key), str):
            raise IncompatibleRunStateError(f"legacy state has an invalid base.{key}")


def _require_ticket_graph(graph: dict[str, Any]) -> None:
    if graph.get("revision") is not None and not isinstance(graph.get("revision"), str):
        raise IncompatibleRunStateError("legacy state has an invalid ticket_graph.revision")
    ordered = graph.get("ordered_ticket_numbers")
    if not isinstance(ordered, list) or not all(isinstance(item, int) for item in ordered):
        raise IncompatibleRunStateError(
            "legacy state has an invalid ticket_graph.ordered_ticket_numbers"
        )
    if not isinstance(graph.get("tickets"), dict):
        raise IncompatibleRunStateError("legacy state has an invalid ticket_graph.tickets")


def _require_invocation(
    invocation: dict[str, Any], location: str, run_id: str
) -> None:
    for key in (
        "work_subject",
        "role",
        "phase",
        "mode",
        "input_fingerprint",
        "started_at",
    ):
        if not isinstance(invocation.get(key), str):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )
    if not isinstance(invocation.get("generation"), int) or isinstance(
        invocation.get("generation"), bool
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.generation"
        )
    if not isinstance(invocation.get("currentness_boundary"), dict):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.currentness_boundary"
        )
    if invocation.get("status") not in {"running", "failed", "completed", "resuming"}:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.status"
        )
    role = invocation["role"]
    phase = invocation["phase"]
    work_subject = invocation["work_subject"]
    allowed_phases = {
        "development": {"developing", "repairing"},
        "fresh_acceptance": {"reviewing"},
        "publication": {"publication"},
        "reviewer": {"run_acceptance"},
        "final_publication": {"run_publication"},
    }
    if role not in allowed_phases or phase not in allowed_phases[role]:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} role or phase"
        )
    if invocation["mode"] not in {"fresh", "resume", "new-thread"}:
        raise IncompatibleRunStateError(f"legacy state has an invalid {location}.mode")
    change_subject = (
        re.fullmatch(r"ticket:[1-9][0-9]*", work_subject) is not None
        or work_subject in {f"parent-only:{run_id}", f"run-repair:{run_id}"}
    )
    if (
        role in {"development", "fresh_acceptance", "publication"}
        and not change_subject
    ) or (
        role == "reviewer" and work_subject != f"run-acceptance:{run_id}"
    ) or (
        role == "final_publication" and work_subject != f"run-publication:{run_id}"
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.work_subject"
        )
    if not isinstance(invocation.get("attempt_count"), int) or isinstance(
        invocation.get("attempt_count"), bool
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.attempt_count"
        )
    for key in ("requested_thread_id", "reported_thread_id", "ended_at", "error"):
        if invocation.get(key) is not None and not isinstance(invocation.get(key), str):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )
    for key in ("return_code", "signal"):
        if invocation.get(key) is not None and (
            not isinstance(invocation.get(key), int)
            or isinstance(invocation.get(key), bool)
        ):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )


def _require_active_invocation_identity(
    state: dict[str, Any], invocation: dict[str, Any]
) -> None:
    subject = invocation["work_subject"]
    generation = invocation["generation"]
    run_id = str(state["run_id"])
    if subject.startswith("ticket:"):
        ticket_number = int(subject.removeprefix("ticket:"))
        jobs = state["ticket_jobs"]
        job = jobs.get(str(ticket_number))
        if (
            not isinstance(job, dict)
            or job.get("ticket_number") != ticket_number
            or job.get("ticket_branch_generation") != generation
            or not _change_owner_phase_is_current(invocation, job)
        ):
            raise IncompatibleRunStateError(
                "legacy state has a stale active Ticket Invocation"
            )
        return
    if subject == f"parent-only:{run_id}":
        job = state.get("parent_job")
        current_generation = job.get("parent_generation", 1) if isinstance(job, dict) else None
    elif subject == f"run-repair:{run_id}":
        acceptance = state.get("run_acceptance")
        job = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        current_generation = job.get("repair_generation") if isinstance(job, dict) else None
    elif subject == f"run-acceptance:{run_id}":
        acceptance = state.get("run_acceptance")
        current_generation = _reviewer_generation(acceptance, invocation["status"])
    elif subject == f"run-publication:{run_id}":
        acceptance = state.get("run_acceptance")
        publication = state.get("run_publication")
        if not isinstance(publication, dict) or publication.get("phase") != "publishing":
            raise IncompatibleRunStateError(
                "legacy state has an active Final Publication without a valid owner"
            )
        current_generation = _final_publication_generation(acceptance)
    else:
        raise IncompatibleRunStateError("legacy state has an invalid active Invocation")
    if (
        type(current_generation) is not int
        or current_generation != generation
        or subject in {f"parent-only:{run_id}", f"run-repair:{run_id}"}
        and not _change_owner_phase_is_current(invocation, job)
    ):
        raise IncompatibleRunStateError("legacy state has a stale active Invocation")


def _acceptance_generation(acceptance: object) -> int | None:
    if not isinstance(acceptance, dict):
        return None
    generation = acceptance.get("acceptance_generation")
    return generation if type(generation) is int and generation > 0 else None


def _reviewer_generation(acceptance: object, invocation_status: str) -> int | None:
    generation = _acceptance_generation(acceptance)
    if not isinstance(acceptance, dict):
        return None
    validation_attempts = acceptance.get("validation_attempts")
    if type(validation_attempts) is not int or validation_attempts < 1:
        return None
    expected_phase = "pending" if invocation_status == "resuming" else "reviewing"
    if acceptance.get("phase") != expected_phase:
        return None
    return generation


def _final_publication_generation(acceptance: object) -> int | None:
    generation = _acceptance_generation(acceptance)
    if not isinstance(acceptance, dict) or acceptance.get("phase") != "accepted":
        return None
    return generation


def _change_owner_phase_is_current(
    invocation: dict[str, Any], job: object
) -> bool:
    if not isinstance(job, dict):
        return False
    role = invocation["role"]
    phase = invocation["phase"]
    if role == "development":
        return isinstance(phase, str) and job.get("phase") == phase
    if role == "fresh_acceptance":
        return job.get("phase") == "reviewing"
    if role == "publication":
        # Publication generation begins while the accepted Candidate is still
        # intact.  ``publishing`` is written only after the Agent returned a
        # valid artifact and its commit was created.
        return job.get("phase") == "accepted"
    return False


def _is_human_blocker(subject: dict[str, Any]) -> bool:
    return subject.get("phase") in {"blocked", "ready_for_human"} and (
        subject.get("blocked_reason")
        in {"agent_requires_human", "reviewer_requires_human"}
        or isinstance(subject.get("human_blockers"), list)
    )
