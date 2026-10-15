"""Small real Attempt and Invocation facts for relational storage tests."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt


def attempt(
    subject: str = "ticket:2", *, context: dict[str, Any] | None = None,
    role: str = "reviewer", ordinal: int = 1, completed: bool = True,
) -> dict[str, Any]:
    owner: dict[str, Any] = {}
    boundary = {"fixture": deepcopy(context or {}), "ordinal": ordinal}
    record = allocate_semantic_attempt(owner, role=role, work_subject=subject, generation=1,
                                       currentness_boundary=boundary, ordinal=ordinal,
                                       budget_window=None if role == "publication" else 1)
    if context is not None:
        record["review_context"] = deepcopy(context)
    if completed:
        close_semantic_attempt(owner, record, outcome="fixture_result")
        return owner["semantic_attempt_history"][0]
    return record


def invocation(
    subject: str = "ticket:2", started: str = "current", *,
    context: dict[str, Any] | None = None, status: str = "completed",
    role: str = "fresh_acceptance", phase: str = "reviewing",
) -> dict[str, Any]:
    semantic_role = {"fresh_acceptance": "reviewer", "reviewer": "reviewer",
                     "final_publication": "publication", "publication": "publication"}.get(role, "development")
    record = attempt(subject, context=context, role=semantic_role, completed=False)
    return {"work_subject": subject, "started_at": started, "status": status,
            "generation": 1, "role": role, "phase": phase, "mode": "fresh",
            "input_fingerprint": "fixture-input", "currentness_boundary": {"fixture": deepcopy(context or {}), "ordinal": 1},
            "semantic_attempt": record}
