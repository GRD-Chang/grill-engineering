from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from agent_run.semantic_attempt import invocation_is_explicitly_resumable
from agent_run.scope_changes import unsupported_scope_change_identity_is_consistent


_OPERATOR_GATE_STATUSES = frozenset(
    {
        "ready_for_human",
        "execution_failed",
        "unsupported_scope_change",
        "deterministic_contradiction",
        "requeue_required",
        "publication_pending",
        "parent_approval_pending",
        "run_approval_pending",
        "supervision_timeout",
        "operator_stopped",
        "abandonment_pending",
    }
)
_MECHANICAL_REVISION_RESTART_REASONS = frozenset(
    {"effective_revision_mismatch", "merged_revision_mismatch"}
)
_OPERATOR_GATE_PHASES = frozenset(
    {"blocked", "ready_for_human", "publication_pending", "ready_for_approval"}
)
_DIAGNOSTIC_ACTION_BY_STATUS = {
    "execution_failed": "execution_failure",
    "deterministic_contradiction": "deterministic_contradiction",
    "blocked": "deterministic_contradiction",
}
_REQUEUE_REASONS = frozenset(
    {
        "ticket_requirements_changed",
        "ticket_base_changed",
        "parent_requirements_changed",
        "parent_base_changed",
        "run_repair_parent_changed",
        "run_repair_graph_changed",
        "run_repair_ticket_completion_changed",
        "run_repair_base_changed",
    }
)
_REQUEUE_GENERATION_KEY_BY_LOCATION = {
    "parent": "parent_generation",
    "run_repair": "repair_generation",
}


def operator_gate_subjects(
    state: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return current top-level work subjects that require operator action."""

    return list(_iter_operator_gate_subjects(state))


def _iter_operator_gate_subjects(
    state: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    local_subjects = list(_iter_local_operator_gate_subjects(state))
    seen = {location for location, _subject in local_subjects}
    yield from local_subjects

    current = _distinct_global_gate_subject(state, seen)
    if current is not None:
        yield current


def _iter_local_operator_gate_subjects(
    state: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    seen: set[str] = set()
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and _subject_requires_operator(active):
        ticket_number = active.get("ticket_number")
        if isinstance(ticket_number, int):
            location = f"ticket:{ticket_number}"
            seen.add(location)
            yield location, active
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        for key, job in ticket_jobs.items():
            location = f"ticket:{key}"
            if (
                isinstance(job, dict)
                and _subject_requires_operator(job)
                and location not in seen
            ):
                seen.add(location)
                yield location, job

    parent = state.get("parent_job")
    if isinstance(parent, dict) and _subject_requires_operator(parent):
        seen.add("parent")
        yield "parent", parent

    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict) and _subject_requires_operator(repair):
            seen.add("run_repair")
            yield "run_repair", repair
        elif _subject_requires_operator(acceptance):
            seen.add("run_acceptance")
            yield "run_acceptance", acceptance

    publication = state.get("run_publication")
    retained_publication_approval = (
        isinstance(acceptance, dict)
        and acceptance.get("phase") == "repairing"
        and isinstance(publication, dict)
        and publication.get("phase") == "ready_for_approval"
    )
    if (
        isinstance(publication, dict)
        and _subject_requires_operator(publication)
        and not retained_publication_approval
    ):
        yield "run_publication", publication


def operator_gate_subject_count(state: dict[str, Any]) -> int:
    """Count distinct current operator-gated work subjects."""

    local_subjects = list(_iter_local_operator_gate_subjects(state))
    subject_count = len(local_subjects)
    if not _status_requires_operator_gate(state):
        return subject_count
    _binding, diagnostic_count = _operator_gate_evidence_summary(state)
    if diagnostic_count:
        return subject_count + diagnostic_count
    seen = {location for location, _subject in local_subjects}
    has_global_subject = _distinct_global_gate_subject(state, seen) is not None
    return subject_count + int(has_global_subject)


def operator_gate_identity_is_consistent(state: dict[str, Any]) -> bool:
    """Whether the current gate matches its top-level status and subject."""

    if state.get("status") == "requeue_required" and _requeue_gate_subject(
        state
    ) is None:
        return False
    if (
        state.get("status") == "unsupported_scope_change"
        and not unsupported_scope_change_identity_is_consistent(state)
    ):
        return False
    local = next(_iter_local_operator_gate_subjects(state), None)
    if local is not None and not _local_gate_matches_top_status(state, local):
        return False
    if not _status_requires_operator_gate(state):
        return True
    binding, count = _operator_gate_evidence_summary(state)
    if count == 0:
        return True
    if count != 1 or binding is None:
        return False
    expected_action = _DIAGNOSTIC_ACTION_BY_STATUS.get(str(state.get("status")))
    if expected_action is None or binding["action_kind"] != expected_action:
        return False
    resolved = _work_subject(state, binding["work_subject"])
    if resolved is None:
        return False
    phase = resolved[1].get("phase")
    if phase is None:
        # A graph-selected Ticket has no Change Job phase until its first
        # Generation; top-level failures bind that current selection as active.
        return binding["phase"] == "active"
    return (
        isinstance(phase, str)
        and phase == binding["phase"]
        and phase not in {"completed", "abandoned"}
    )

def _local_gate_matches_top_status(
    state: dict[str, Any], local: tuple[str, dict[str, Any]]
) -> bool:
    status = state.get("status")
    location, subject = local
    phase = subject.get("phase")
    if status == "operator_stopped":
        return True
    if status in {"waiting_external", "supervision_timeout", "abandonment_pending"}:
        return True
    if status == "ready_for_human":
        return phase in {"blocked", "ready_for_human"}
    if status == "blocked":
        return phase == "blocked"
    if status == "publication_pending":
        return phase == "publication_pending"
    if status == "parent_approval_pending":
        return location == "parent" and phase == "ready_for_approval"
    if status == "run_approval_pending":
        return location == "run_publication" and phase == "ready_for_approval"
    if status == "requeue_required":
        resolved = _requeue_gate_subject(state)
        return resolved is not None and resolved[0] == location
    if status == "unsupported_scope_change":
        # A newly observed graph contradiction invalidates an approval that was
        # current under the accepted graph.  The retained owner is still the
        # accurate Work Subject and preserves its accepted Candidate/PR.
        return phase == "ready_for_approval"
    return False


def has_run_operator_gate(state: dict[str, Any]) -> bool:
    """Whether automatic selection of another top-level work subject must stop."""

    if has_local_operator_gate(state):
        return True
    return _status_requires_operator_gate(state)


def has_local_operator_gate(state: dict[str, Any]) -> bool:
    """Whether a current Work Subject requires an explicit operator action."""

    return next(_iter_local_operator_gate_subjects(state), None) is not None


def has_non_invocation_execution_failure(state: dict[str, Any]) -> bool:
    """Whether execution failed without a resumable Agent Invocation."""

    return state.get("status") == "execution_failed" and not (
        invocation_is_explicitly_resumable(state)
    )


def active_ticket_gate_mirror_is_consistent(state: dict[str, Any]) -> bool:
    """Whether every gate agrees with the current active Ticket identity."""

    subjects = operator_gate_subjects(state)
    active = state.get("active_ticket_job")
    if not isinstance(active, dict):
        return not any(
            location.startswith("ticket:") for location, _subject in subjects
        )
    ticket_number = active.get("ticket_number")
    if not isinstance(ticket_number, int):
        return True
    if not subjects:
        return True
    terminal = active.get("phase") in {"merged", "completed", "abandoned"}
    ticket_subjects = [
        location for location, _subject in subjects if location.startswith("ticket:")
    ]
    if terminal and not ticket_subjects:
        return True
    evidence = operator_gate_evidence(state)
    if (
        active.get("phase") in {"completed", "abandoned"}
        and evidence is not None
        and evidence["work_subject"].startswith("ticket:")
    ):
        # A completed Ticket cannot become the current Change Job again merely
        # because a later top-level diagnostic names it.  The merged phase is
        # intentionally excluded: response-loss recovery may still own the
        # exact, mirrored Ticket while its closeout operation is uncertain.
        return False
    expected_location = f"ticket:{ticket_number}"
    if any(location != expected_location for location, _subject in subjects):
        return False
    jobs = state.get("ticket_jobs")
    mirror = (
        jobs.get(str(ticket_number))
        if isinstance(jobs, dict)
        else None
    )
    return isinstance(mirror, dict) and mirror == active


def has_unresolved_subject_gate(state: dict[str, Any]) -> bool:
    """Whether Controller refresh could bypass an unresolved local decision."""

    return any(
        subject.get("phase") in {"blocked", "ready_for_human"}
        for _location, subject in _iter_operator_gate_subjects(state)
    )


def has_mechanical_revision_restart(state: dict[str, Any]) -> bool:
    """Whether a current Change Job must reach the existing Requeue boundary."""

    jobs: list[dict[str, Any]] = []
    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, dict):
        work_subject = invocation.get("work_subject")
        if isinstance(work_subject, str):
            resolved = _work_subject(state, work_subject)
            if resolved is not None:
                jobs.append(resolved[1])
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        jobs.append(active)
    parent = state.get("parent_job")
    if state.get("delivery_type") == "parent_only" and isinstance(parent, dict):
        jobs.append(parent)
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict) and acceptance.get("phase") == "repairing":
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            jobs.append(repair)
    return any(
        job.get("phase") == "blocked"
        and is_mechanical_revision_restart(job.get("blocked_reason"))
        for job in jobs
    )


def _status_requires_operator_gate(state: dict[str, Any]) -> bool:
    status = state.get("status")
    if status != "requeue_required" and has_mechanical_revision_restart(state):
        return False
    if status in _OPERATOR_GATE_STATUSES:
        return True
    if status == "blocked":
        return state.get("terminal_kind") in {"permanent_blocked", "waiting_human"}
    return status == "progress_exhausted" and state.get("terminal_kind") == "waiting_human"


def _subject_requires_operator(subject: dict[str, Any]) -> bool:
    if subject.get("phase") not in _OPERATOR_GATE_PHASES:
        return False
    # These two local stale markers are mechanically converted to the existing
    # Requeue boundary before the Controller may select another Ticket.
    return not is_mechanical_revision_restart(subject.get("blocked_reason"))


def is_mechanical_revision_restart(reason: object) -> bool:
    """Whether a local stale marker is automatically converted to Requeue."""

    return reason in _MECHANICAL_REVISION_RESTART_REASONS


def operator_gate_evidence(state: dict[str, Any]) -> dict[str, str] | None:
    """Return one durable diagnostic binding for a top-level operator gate."""

    binding, count = _operator_gate_evidence_summary(state)
    return binding if count == 1 else None


def _operator_gate_evidence_summary(
    state: dict[str, Any],
) -> tuple[dict[str, str] | None, int]:
    """Return the first valid binding and the number of bound actions."""

    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, list):
        return None, 0
    binding: dict[str, str] | None = None
    bindings: set[tuple[str, str, str, str]] = set()
    for diagnostic in diagnostics:
        evidence = (
            diagnostic.get("operator_gate")
            if isinstance(diagnostic, dict)
            else None
        )
        if not isinstance(evidence, dict):
            continue
        work_subject = evidence.get("work_subject")
        action_kind = evidence.get("action_kind")
        phase = evidence.get("phase")
        reason = evidence.get("reason")
        if (
            isinstance(work_subject, str)
            and work_subject
            and isinstance(action_kind, str)
            and action_kind
            and isinstance(phase, str)
            and phase
            and isinstance(reason, str)
            and reason
        ):
            bindings.add((work_subject, action_kind, phase, reason))
            if binding is None:
                binding = {
                    "work_subject": work_subject,
                    "action_kind": action_kind,
                    "phase": phase,
                    "reason": reason,
                }
    return binding, len(bindings)


def _global_gate_subject(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    status = state.get("status")
    work_subject: object = None
    evidence = (
        operator_gate_evidence(state)
        if _status_requires_operator_gate(state)
        else None
    )
    if evidence is not None:
        work_subject = evidence["work_subject"]
    elif status in {"execution_failed", "deterministic_contradiction"}:
        invocation = state.get("active_agent_invocation")
        if isinstance(invocation, dict):
            work_subject = invocation.get("work_subject")
    elif status == "requeue_required":
        requeue = state.get("requeue_required")
        if isinstance(requeue, dict):
            work_subject = requeue.get("work_subject")
    if isinstance(work_subject, str):
        resolved = _work_subject(state, work_subject)
        if resolved is not None and not (
            status != "requeue_required"
            and resolved[1].get("phase") == "blocked"
            and is_mechanical_revision_restart(resolved[1].get("blocked_reason"))
        ):
            return resolved
    if status == "parent_approval_pending":
        parent = state.get("parent_job")
        return ("parent", parent) if isinstance(parent, dict) else None
    if status in {"run_approval_pending", "publication_pending"}:
        publication = state.get("run_publication")
        if isinstance(publication, dict):
            return "run_publication", publication
        parent = state.get("parent_job")
        if isinstance(parent, dict):
            return "parent", parent
        active = state.get("active_ticket_job")
        if isinstance(active, dict):
            return f"ticket:{active.get('ticket_number')}", active
    if status == "operator_stopped":
        invocation = state.get("active_agent_invocation")
        work_subject = (
            invocation.get("work_subject")
            if isinstance(invocation, dict)
            else None
        )
        if isinstance(work_subject, str):
            resolved = _work_subject(state, work_subject)
            if resolved is not None:
                return resolved
        active = state.get("active_ticket_job")
        if isinstance(active, dict) and active.get("phase") not in {
            "merged",
            "completed",
            "abandoned",
        }:
            return f"ticket:{active.get('ticket_number')}", active
        parent = state.get("parent_job")
        if isinstance(parent, dict) and parent.get("phase") not in {
            "merged",
            "completed",
            "abandoned",
        }:
            return "parent", parent
        acceptance = state.get("run_acceptance")
        if isinstance(acceptance, dict):
            repair = acceptance.get("repair_job")
            if isinstance(repair, dict) and repair.get("phase") not in {
                "merged",
                "completed",
                "abandoned",
            }:
                return "run_repair", repair
            if acceptance.get("phase") not in {"accepted", "completed"}:
                return "run_acceptance", acceptance
        publication = state.get("run_publication")
        if isinstance(publication, dict):
            return "run_publication", publication
    if status == "supervision_timeout":
        active = state.get("active_ticket_job")
        if isinstance(active, dict):
            return f"ticket:{active.get('ticket_number')}", active
        acceptance = state.get("run_acceptance")
        if isinstance(acceptance, dict):
            repair = acceptance.get("repair_job")
            if isinstance(repair, dict):
                return "run_repair", repair
        publication = state.get("run_publication")
        if isinstance(publication, dict):
            return "run_publication", publication
        parent = state.get("parent_job")
        if isinstance(parent, dict):
            return "parent", parent
        if isinstance(acceptance, dict):
            return "run_acceptance", acceptance
    if status == "abandonment_pending":
        active = state.get("active_ticket_job")
        if isinstance(active, dict) and active.get("phase") not in {
            "merged",
            "completed",
            "abandoned",
        }:
            return f"ticket:{active.get('ticket_number')}", active
        parent = state.get("parent_job")
        if isinstance(parent, dict) and parent.get("phase") not in {
            "merged",
            "completed",
            "abandoned",
        }:
            return "parent", parent
        acceptance = state.get("run_acceptance")
        if isinstance(acceptance, dict):
            repair = acceptance.get("repair_job")
            if isinstance(repair, dict) and repair.get("phase") not in {
                "merged",
                "completed",
                "abandoned",
            }:
                return "run_repair", repair
        publication = state.get("run_publication")
        if isinstance(publication, dict):
            return "run_publication", publication
        if isinstance(acceptance, dict):
            return "run_acceptance", acceptance
    return None


def _distinct_global_gate_subject(
    state: dict[str, Any], seen: set[str]
) -> tuple[str, dict[str, Any]] | None:
    current = _global_gate_subject(state)
    if current is not None:
        return current if current[0] not in seen else None
    if _unbound_status_gate_is_distinct(state, seen):
        # Without durable subject evidence, a top-level failure cannot be
        # assumed to describe a local Human Blocker.
        return "run", state
    return None


def _unbound_status_gate_is_distinct(
    state: dict[str, Any], seen: set[str]
) -> bool:
    if not _status_requires_operator_gate(state):
        return False
    if state.get("status") in {"execution_failed", "deterministic_contradiction"}:
        return True
    return not seen


def _work_subject(
    state: dict[str, Any], work_subject: str
) -> tuple[str, dict[str, Any]] | None:
    if work_subject.startswith("ticket:"):
        key = work_subject.removeprefix("ticket:")
        jobs = state.get("ticket_jobs")
        job = jobs.get(key) if isinstance(jobs, dict) else None
        return (f"ticket:{key}", job) if isinstance(job, dict) else None
    run_id = state.get("run_id")
    if work_subject == f"parent-only:{run_id}":
        parent = state.get("parent_job")
        return ("parent", parent) if isinstance(parent, dict) else None
    acceptance = state.get("run_acceptance")
    if work_subject == f"run-repair:{run_id}" and isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        return ("run_repair", repair) if isinstance(repair, dict) else None
    if work_subject == f"run-acceptance:{run_id}":
        return (
            ("run_acceptance", acceptance)
            if isinstance(acceptance, dict)
            else None
        )
    if work_subject == f"run-publication:{run_id}":
        publication = state.get("run_publication")
        return (
            ("run_publication", publication)
            if isinstance(publication, dict)
            else None
        )
    return None


def _requeue_gate_subject(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    requeue = state.get("requeue_required")
    if not isinstance(requeue, dict):
        return None
    work_subject = requeue.get("work_subject")
    generation = requeue.get("generation")
    reason = requeue.get("reason")
    if (
        not isinstance(work_subject, str)
        or type(generation) is not int
        or generation < 1
        or not isinstance(reason, str)
        or reason not in _REQUEUE_REASONS
    ):
        return None
    resolved = _work_subject(state, work_subject)
    if resolved is None:
        return None
    generation_key = (
        "ticket_branch_generation"
        if resolved[0].startswith("ticket:")
        else _REQUEUE_GENERATION_KEY_BY_LOCATION.get(resolved[0])
    )
    subject_generation = (
        resolved[1].get(generation_key) if generation_key is not None else None
    )
    diagnostics = state.get("diagnostics")
    if (
        type(subject_generation) is not int
        or subject_generation != generation
        or not isinstance(diagnostics, list)
        or len(diagnostics) != 1
    ):
        return None
    diagnostic = diagnostics[0]
    return (
        resolved
        if isinstance(diagnostic, dict)
        and diagnostic.get("code") == reason
        and isinstance(diagnostic.get("message"), str)
        and bool(diagnostic["message"])
        else None
    )
