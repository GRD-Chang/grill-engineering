"""Interpret retained work, independently of its operator pause or container.

These are read-only projections of the existing phase, Semantic Attempt and
integration scene. They never become a second persisted lifecycle.
"""
from __future__ import annotations

from typing import Any


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
