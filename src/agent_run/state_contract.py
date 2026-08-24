from __future__ import annotations

import re
from typing import Any

from agent_run.review_budget import (
    RUN_POLICY,
    TICKET_POLICY,
    ReviewBudgetPolicy,
    ensure_budget,
)
from agent_run.integration_record_contract import (
    require_completed_ticket_integration_records as require_completed_ticket_integration_records,
)
from agent_run.state_errors import (
    IncompatibleRunStateError as IncompatibleRunStateError,
)
from agent_run.ticket_phase import TicketPhase
from agent_run.ticket_publication_contract import (
    require_active_ticket_publication_authorization as require_active_ticket_publication_authorization,
)
from agent_run.semantic_attempt import (
    require_controller_reprepare_intent,
    require_semantic_attempt,
    require_semantic_attempt_record,
)


_CHANGE_JOB_PHASES = frozenset(phase.value for phase in TicketPhase)
_ACTIVE_RUN_REPAIR_PHASES = frozenset(
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
        "escalating",
        "merging",
        "merged",
        "blocked",
    }
)
_RESUME_AUDIT_KINDS = frozenset(
    {
        "agent_invocation",
        "budget_checkpoint",
        "github_refresh_retry",
        "human_blocker",
        "supervision_timeout",
    }
)
_MAX_RESUME_AUDIT_EVENTS = 64
_INTEGRATED_REVALIDATION_MERGE_PHASES = frozenset(
    {
        "developing",
        "repairing",
        "committing_candidate",
        "candidate",
        "reviewing",
        "accepted",
        "escalating",
        "merged",
        "blocked",
    }
)
_CANDIDATE_ACCEPTANCE_HISTORY_KEYS = frozenset(
    {
        "candidate_sha",
        "repair_base_run_head_sha",
        "default_base_sha",
        "candidate_tree",
        "expected_merge_tree",
        "parent_revision",
        "ticket_graph_revision",
        "ticket_completion_records_fingerprint",
        "reviewer_thread_id",
        "development_thread_id",
        "pr_number",
        "integrated_sha",
        "repair_source",
        "outcome",
    }
)
_INTEGRATED_REVALIDATION_MERGE_KEYS = frozenset(
    {
        "base_sha",
        "default_base_sha",
        "candidate_sha",
        "publication_sha",
    }
)
def require_current_run_state(state: dict[str, Any]) -> None:
    """Reject non-canonical persisted Runs before they are read or mutated."""

    if "schema_version" in state:
        raise IncompatibleRunStateError(
            "legacy state is incompatible with the Invocation/Generation contract"
        )
    if type(state.get("branch_authority_protocol")) is not int or state.get(
        "branch_authority_protocol"
    ) != 2:
        raise IncompatibleRunStateError(
            "legacy state has an incompatible branch authority protocol"
        )
    if state.get("review_budget_protocol") != 1:
        raise IncompatibleRunStateError(
            "legacy state has an incompatible Review Budget protocol"
        )
    if (
        type(state.get("semantic_attempt_protocol")) is not int
        or state.get("semantic_attempt_protocol") != 1
    ):
        raise IncompatibleRunStateError(
            "legacy state has an incompatible Semantic Agent Attempt protocol"
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
        ("resume_audit", dict),
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
    require_completed_ticket_integration_records(state)
    _require_review_budget_windows(state)
    active_ticket = state.get("active_ticket_job")
    if isinstance(active_ticket, dict):
        require_active_ticket_publication_authorization(active_ticket)
    _require_human_blocker_containers(state)
    _require_candidate_acceptance_histories(state)
    _require_active_run_repair_mode(state)
    _require_integrated_revalidation_merge(state)
    _require_semantic_attempt_owners(state)
    _require_resume_audit(state["resume_audit"])
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
    _require_resume_audit_invocation_links(state)
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


def require_candidate_acceptance_history(
    value: object, location: str
) -> list[dict[str, Any]]:
    """Validate the durable, non-nested Candidate Acceptance audit shape."""

    if not isinstance(value, list):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} history"
        )
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _CANDIDATE_ACCEPTANCE_HISTORY_KEYS:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}[{index}] audit snapshot"
            )
        for key in (
            "candidate_sha",
            "repair_base_run_head_sha",
            "default_base_sha",
            "candidate_tree",
            "expected_merge_tree",
            "parent_revision",
            "ticket_graph_revision",
            "ticket_completion_records_fingerprint",
            "reviewer_thread_id",
            "repair_source",
            "outcome",
        ):
            if not isinstance(item[key], str):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}[{index}].{key}"
                )
        for key in ("development_thread_id", "integrated_sha"):
            if item[key] is not None and not isinstance(item[key], str):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}[{index}].{key}"
                )
        if item["pr_number"] is not None and (
            type(item["pr_number"]) is not int or item["pr_number"] < 1
        ):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}[{index}].pr_number"
            )
        if item["outcome"] not in {"accepted", "finding", "blocked"}:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}[{index}].outcome"
            )
        validated.append(dict(item))
    return validated


def _require_candidate_acceptance_histories(state: dict[str, Any]) -> None:
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return
    history = acceptance.get("candidate_acceptance_history")
    if history is not None:
        require_candidate_acceptance_history(
            history, "run_acceptance.candidate_acceptance"
        )
    repair = acceptance.get("repair_job")
    if isinstance(repair, dict):
        repair_history = repair.get("candidate_acceptance_history")
        if repair_history is not None:
            require_candidate_acceptance_history(
                repair_history, "run_acceptance.repair_job.candidate_acceptance"
            )


def _require_active_run_repair_mode(state: dict[str, Any]) -> None:
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return
    repair = acceptance.get("repair_job")
    if not isinstance(repair, dict) or repair.get("phase") not in _ACTIVE_RUN_REPAIR_PHASES:
        return
    if repair.get("repair_mode") not in {"squash", "merge_resolution"}:
        raise IncompatibleRunStateError(
            "legacy state has an invalid run_acceptance.repair_job.repair_mode"
        )


def _require_integrated_revalidation_merge(state: dict[str, Any]) -> None:
    """Validate the persisted authority snapshot used after a merged repair drifts."""

    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return
    repair = acceptance.get("repair_job")
    if not isinstance(repair, dict) or "integrated_revalidation_merge" not in repair:
        return
    marker = repair["integrated_revalidation_merge"]
    location = "run_acceptance.repair_job.integrated_revalidation_merge"
    if not isinstance(marker, dict):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} object"
        )
    if set(marker) != _INTEGRATED_REVALIDATION_MERGE_KEYS:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} field set"
        )
    if repair.get("phase") not in _INTEGRATED_REVALIDATION_MERGE_PHASES:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} phase"
        )
    if repair.get("repair_mode") != "squash":
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location} repair mode"
        )
    for key in _INTEGRATED_REVALIDATION_MERGE_KEYS:
        value = marker[key]
        if not isinstance(value, str) or not value.strip():
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )


def _require_review_budget_windows(state: dict[str, Any]) -> None:
    """Reject Jobs that cannot prove the canonical bounded-budget shape."""

    subjects: list[tuple[str, dict[str, Any], ReviewBudgetPolicy]] = []
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        subjects.extend(
            (f"ticket_jobs[{key}]", job, TICKET_POLICY)
            for key, job in ticket_jobs.items()
            if isinstance(job, dict) and _looks_like_materialized_job(job)
        )
    for key in ("parent_job", "run_acceptance"):
        value = state.get(key)
        if isinstance(value, dict):
            subjects.append((key, value, RUN_POLICY))
            if key == "run_acceptance" and isinstance(value.get("repair_job"), dict):
                subjects.append(("run_acceptance.repair_job", value["repair_job"], RUN_POLICY))
    for location, job, policy in subjects:
        try:
            ensure_budget(job, policy)
        except (TypeError, ValueError) as error:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid canonical {location}.review_budget"
            ) from error


def _looks_like_materialized_job(value: dict[str, Any]) -> bool:
    """Exclude frontier selection placeholders from nested Job validation."""

    return any(
        key in value for key in ("phase", "review_budget", "modification_attempts")
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


def _require_resume_audit(audit: dict[str, Any]) -> None:
    total = audit.get("total")
    compacted = audit.get("compacted")
    history = audit.get("history")
    digest = audit.get("rolling_digest")
    if type(total) is not int or total < 0:
        raise IncompatibleRunStateError("legacy state has an invalid resume_audit.total")
    if type(compacted) is not int or compacted < 0:
        raise IncompatibleRunStateError(
            "legacy state has an invalid resume_audit.compacted"
        )
    if not isinstance(history, list) or len(history) > _MAX_RESUME_AUDIT_EVENTS:
        raise IncompatibleRunStateError(
            "legacy state has an invalid resume_audit.history"
        )
    if total != compacted + len(history):
        raise IncompatibleRunStateError(
            "legacy state has inconsistent Resume audit counters"
        )
    if (total == 0 and digest is not None) or (
        total > 0
        and (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        )
    ):
        raise IncompatibleRunStateError(
            "legacy state has an invalid resume_audit.rolling_digest"
        )
    for index, event in enumerate(history):
        location = f"resume_audit.history[{index}]"
        if not isinstance(event, dict):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}"
            )
        expected_sequence = compacted + index + 1
        if event.get("sequence") != expected_sequence:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.sequence"
            )
        for key in ("resume_id", "requested_at", "source_status"):
            value = event.get(key)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}.{key}"
                )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", event["resume_id"]) is None:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.resume_id"
            )
        if event.get("kind") not in _RESUME_AUDIT_KINDS:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.kind"
            )
        for key in ("new_thread", "human_response_supplied"):
            if not isinstance(event.get(key), bool):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}.{key}"
                )
        for key in (
            "failure_code",
            "work_subject",
            "semantic_attempt_id",
            "source_invocation_started_at",
            "source_invocation_status",
            "thread_id",
            "successor_invocation_started_at",
        ):
            value = event.get(key)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 512
            ):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}.{key}"
                )
        generation = event.get("generation")
        if generation is not None and (
            type(generation) is not int or generation < 1
        ):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.generation"
            )


def _require_resume_audit_invocation_links(state: dict[str, Any]) -> None:
    """Require retained Resume events and successor Invocations to agree."""

    audit = state["resume_audit"]
    history = audit["history"]
    events_by_id: dict[str, dict[str, Any]] = {}
    for event in history:
        resume_id = event["resume_id"]
        if resume_id in events_by_id:
            raise IncompatibleRunStateError(
                "legacy state has duplicate Resume audit identities"
            )
        events_by_id[resume_id] = event

    invocations = state["agent_invocation_history"]
    for event in history:
        successor_started_at = event.get("successor_invocation_started_at")
        if successor_started_at is None:
            continue
        attempt_id = event.get("semantic_attempt_id")
        if not isinstance(attempt_id, str):
            raise IncompatibleRunStateError(
                "legacy state has a Resume successor without an Attempt identity"
            )
        matching = [
            invocation
            for invocation in invocations
            if invocation.get("resume_id") == event["resume_id"]
        ]
        if len(matching) != 1:
            raise IncompatibleRunStateError(
                "legacy state has an invalid Resume successor relationship"
            )
        invocation = matching[0]
        semantic_attempt = invocation.get("semantic_attempt")
        if (
            invocation.get("started_at") != successor_started_at
            or invocation.get("resume_sequence") != event["sequence"]
            or not isinstance(semantic_attempt, dict)
            or semantic_attempt.get("attempt_id") != attempt_id
        ):
            raise IncompatibleRunStateError(
                "legacy state has an inconsistent Resume successor relationship"
            )

    retained_ids = set(events_by_id)
    compacted = audit["compacted"]
    linked_invocations = list(invocations)
    active = state.get("active_agent_invocation")
    if isinstance(active, dict):
        linked_invocations.append(active)
    for invocation in linked_invocations:
        resume_id = invocation.get("resume_id")
        if resume_id is None:
            continue
        if resume_id not in retained_ids:
            resume_sequence = invocation.get("resume_sequence")
            if (
                type(resume_sequence) is not int
                or resume_sequence < 1
                or resume_sequence > compacted
            ):
                raise IncompatibleRunStateError(
                    "legacy state has an Invocation bound to an unknown Resume"
                )
            continue
        event = events_by_id[resume_id]
        semantic_attempt = invocation.get("semantic_attempt")
        if (
            event.get("successor_invocation_started_at")
            != invocation.get("started_at")
            or event.get("sequence") != invocation.get("resume_sequence")
            or not isinstance(semantic_attempt, dict)
            or event.get("semantic_attempt_id")
            != semantic_attempt.get("attempt_id")
        ):
            raise IncompatibleRunStateError(
                "legacy state has an Invocation bound to the wrong Resume"
            )


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
    semantic_attempt = invocation.get("semantic_attempt")
    if not isinstance(semantic_attempt, dict):
        raise IncompatibleRunStateError(
            f"legacy state is missing canonical {location}.semantic_attempt"
        )
    if invocation.get("status") not in {"running", "failed", "completed", "resuming"}:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.status"
        )
    resume_id = invocation.get("resume_id")
    if resume_id is not None and (
        not isinstance(resume_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", resume_id) is None
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.resume_id"
        )
    resume_sequence = invocation.get("resume_sequence")
    if (resume_id is None and resume_sequence is not None) or (
        resume_id is not None
        and (type(resume_sequence) is not int or resume_sequence < 1)
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.resume_sequence"
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
    semantic_role = (
        "development"
        if role == "development"
        else "reviewer" if role in {"fresh_acceptance", "reviewer"} else "publication"
    )
    try:
        require_semantic_attempt(
            semantic_attempt,
            role=semantic_role,
            work_subject=str(work_subject),
            generation=int(invocation["generation"]),
            currentness_boundary=invocation["currentness_boundary"],
            budget_window=semantic_attempt.get("budget_window"),
        )
    except (TypeError, ValueError) as error:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.semantic_attempt: {error}"
        ) from error
    if semantic_role in {"development", "reviewer"}:
        if type(semantic_attempt.get("budget_window")) is not int:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.semantic_attempt budget window"
            )
    elif semantic_attempt.get("budget_window") is not None:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.semantic_attempt budget window"
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
    for key in (
        "binding_id",
        "binding_role",
        "profile_role",
        "invocation_role",
        "model",
        "reasoning_effort",
    ):
        value = invocation.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )
    for key in ("binding_role", "profile_role", "invocation_role"):
        role_value = invocation.get(key)
        if role_value is not None and role_value not in {
            "development",
            "review",
            "publication",
        }:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.{key}"
            )
    revision = invocation.get("profile_revision")
    if revision is not None and (
        not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.profile_revision"
        )
    binding = invocation.get("thread_execution_binding")
    if binding is not None and not isinstance(binding, dict):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}.thread_execution_binding"
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
        _require_pending_invocation_attempt(job, invocation)
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
    if subject == f"run-acceptance:{run_id}":
        owner = state.get("run_acceptance")
    elif subject == f"run-publication:{run_id}":
        owner = state.get("run_publication")
    else:
        owner = job
    _require_pending_invocation_attempt(owner, invocation)


def _require_semantic_attempt_owners(state: dict[str, Any]) -> None:
    """Reject owner-local Attempt contradictions even without an active pointer."""

    run_id = str(state["run_id"])
    owners: list[tuple[str, dict[str, Any], set[str], str, int]] = []
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for key, value in jobs.items():
            if isinstance(value, dict) and type(value.get("ticket_number")) is int:
                owners.append(
                    (
                        f"ticket_jobs[{key}]",
                        value,
                        {"development", "reviewer", "publication"},
                        f"ticket:{value['ticket_number']}",
                        int(value.get("ticket_branch_generation", 1)),
                    )
                )
    active_ticket = state.get("active_ticket_job")
    if (
        isinstance(active_ticket, dict)
        and type(active_ticket.get("ticket_number")) is int
    ):
        owners.append(
            (
                "active_ticket_job",
                active_ticket,
                {"development", "reviewer", "publication"},
                f"ticket:{active_ticket['ticket_number']}",
                int(active_ticket.get("ticket_branch_generation", 1)),
            )
        )
    parent = state.get("parent_job")
    if isinstance(parent, dict):
        owners.append(
            (
                "parent_job",
                parent,
                {"development", "reviewer", "publication"},
                f"parent-only:{run_id}",
                int(parent.get("parent_generation", 1)),
            )
        )
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        owners.append(
            (
                "run_acceptance",
                acceptance,
                {"reviewer"},
                f"run-acceptance:{run_id}",
                int(acceptance.get("acceptance_generation", 1)),
            )
        )
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            owners.append(
                (
                    "run_acceptance.repair_job",
                    repair,
                    {"development", "reviewer", "publication"},
                    f"run-repair:{run_id}",
                    int(repair.get("repair_generation", 1)),
                )
            )
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        owners.append(
            (
                "run_publication",
                publication,
                {"publication"},
                f"run-publication:{run_id}",
                int(
                    acceptance.get("acceptance_generation", 1)
                    if isinstance(acceptance, dict)
                    else 1
                ),
            )
        )
    retired = state.get("retired_semantic_attempt_owners", [])
    if not isinstance(retired, list) or len(retired) > 64:
        raise IncompatibleRunStateError(
            "legacy state has invalid retired Semantic Agent Attempt owners"
        )
    for index, owner in enumerate(retired):
        if (
            not isinstance(owner, dict)
            or owner.get("owner_kind") != "run_repair"
            or owner.get("work_subject") != f"run-repair:{run_id}"
            or type(owner.get("generation")) is not int
            or int(owner["generation"]) < 1
            or owner.get("pending_semantic_attempt") is not None
        ):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid retired Semantic Agent Attempt owner at index {index}"
            )
        owners.append(
            (
                f"retired_semantic_attempt_owners[{index}]",
                owner,
                {"development", "reviewer", "publication"},
                f"run-repair:{run_id}",
                int(owner["generation"]),
            )
        )
    _require_controller_reprepare_state(state, acceptance)
    records_by_id: dict[str, dict[str, Any]] = {}
    for location, owner, roles, work_subject, generation in owners:
        pending = owner.get("pending_semantic_attempt")
        if pending is not None:
            if not isinstance(pending, dict):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}.pending_semantic_attempt"
                )
            _require_owner_attempt(
                pending,
                location=f"{location}.pending_semantic_attempt",
                status="pending",
                roles=roles,
                work_subject=work_subject,
                maximum_generation=generation,
                owner=owner,
                require_current=True,
            )
            _require_unique_attempt_record(records_by_id, pending, location)
        history = owner.get("semantic_attempt_history", [])
        if not isinstance(history, list):
            raise IncompatibleRunStateError(
                f"legacy state has an invalid {location}.semantic_attempt_history"
            )
        for index, attempt in enumerate(history):
            if not isinstance(attempt, dict):
                raise IncompatibleRunStateError(
                    f"legacy state has an invalid {location}.semantic_attempt_history[{index}]"
                )
            item_location = f"{location}.semantic_attempt_history[{index}]"
            _require_owner_attempt(
                attempt,
                location=item_location,
                status="completed",
                roles=roles,
                work_subject=work_subject,
                maximum_generation=generation,
                owner=owner,
                require_current=False,
            )
            _require_unique_attempt_record(records_by_id, attempt, item_location)


def _require_owner_attempt(
    attempt: dict[str, Any],
    *,
    location: str,
    status: str,
    roles: set[str],
    work_subject: str,
    maximum_generation: int,
    owner: dict[str, Any],
    require_current: bool,
) -> None:
    try:
        require_semantic_attempt_record(attempt, expected_status=status)
    except ValueError as error:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid {location}: {error}"
        ) from error
    generation = int(attempt["generation"])
    if (
        attempt["role"] not in roles
        or attempt["work_subject"] != work_subject
        or generation > maximum_generation
        or (require_current and generation != maximum_generation)
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an owner mismatch at {location}"
        )
    budget_window = attempt.get("budget_window")
    if attempt["role"] == "publication":
        if budget_window is not None:
            raise IncompatibleRunStateError(
                f"legacy state has an invalid Publication Budget Window at {location}"
            )
        return
    budget = owner.get("review_budget")
    current_window = budget.get("window") if isinstance(budget, dict) else None
    if (
        type(current_window) is not int
        or type(budget_window) is not int
        or budget_window > current_window
        or (require_current and budget_window != current_window)
    ):
        raise IncompatibleRunStateError(
            f"legacy state has an invalid Review Budget Window at {location}"
        )


def _require_unique_attempt_record(
    records: dict[str, dict[str, Any]], attempt: dict[str, Any], location: str
) -> None:
    attempt_id = str(attempt["attempt_id"])
    previous = records.get(attempt_id)
    if previous is not None and previous != attempt:
        raise IncompatibleRunStateError(
            f"legacy state has conflicting Semantic Agent Attempt mirrors at {location}"
        )
    records[attempt_id] = attempt


def _require_pending_invocation_attempt(
    owner: object, invocation: dict[str, Any]
) -> None:
    if not isinstance(owner, dict):
        raise IncompatibleRunStateError(
            "legacy state has an active Invocation without an Attempt owner"
        )
    pending = owner.get("pending_semantic_attempt")
    bound = invocation.get("semantic_attempt")
    if (
        not isinstance(pending, dict)
        or not isinstance(bound, dict)
        or pending.get("attempt_id") != bound.get("attempt_id")
    ):
        raise IncompatibleRunStateError(
            "legacy state has inconsistent pending Semantic Agent Attempt and active Invocation"
        )
    budget_window = pending.get("budget_window")
    if budget_window is not None:
        budget = owner.get("review_budget")
        if not isinstance(budget, dict) or budget.get("window") != budget_window:
            raise IncompatibleRunStateError(
                "legacy state has a pending Semantic Agent Attempt in the wrong Budget Window"
            )


def _require_controller_reprepare_state(
    state: dict[str, Any], acceptance: object
) -> None:
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    marker_owners: list[object] = [state.get("active_ticket_job"), state.get("parent_job")]
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        marker_owners.extend(jobs.values())
    for owner in marker_owners:
        if isinstance(owner, dict) and "controller_candidate_reprepare" in owner:
            raise IncompatibleRunStateError(
                "legacy state has Controller Candidate reprepare authority outside Run Repair"
            )
    if not isinstance(repair, dict):
        return
    marker_present = "controller_candidate_reprepare" in repair
    pending = repair.get("pending_semantic_attempt")
    if repair.get("phase") == "committing_candidate" and pending is None:
        if not marker_present:
            raise IncompatibleRunStateError(
                "legacy state is missing Controller Candidate reprepare authority"
            )
    if not marker_present:
        return
    if pending is not None:
        raise IncompatibleRunStateError(
            "legacy state mixes Controller Candidate reprepare with a pending Semantic Agent Attempt"
        )
    active = state.get("active_agent_invocation")
    if (
        isinstance(active, dict)
        and active.get("status") in {"running", "failed", "resuming"}
        and active.get("work_subject") == f"run-repair:{state['run_id']}"
    ):
        raise IncompatibleRunStateError(
            "legacy state mixes Controller Candidate reprepare with an active Agent Invocation"
        )
    try:
        require_controller_reprepare_intent(repair)
    except ValueError as error:
        raise IncompatibleRunStateError(
            f"legacy state has an invalid Controller Candidate reprepare intent: {error}"
        ) from error


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
