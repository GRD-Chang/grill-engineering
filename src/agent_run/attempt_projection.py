"""Read-only Semantic Attempt selection and explicit machine-output filtering."""
from __future__ import annotations

from agent_run.semantic_attempt import semantic_attempt_subjects

def current_semantic_attempt(
    state: dict[str, object], invocation: dict[str, object] | None
) -> dict[str, object] | None:
    invocation_attempt = (
        invocation.get("semantic_attempt") if isinstance(invocation, dict) else None
    )
    invocation_attempt_id = (
        invocation_attempt.get("attempt_id")
        if isinstance(invocation_attempt, dict)
        else None
    )
    pending_attempts = [
        pending
        for subject in semantic_attempt_subjects(state)
        if isinstance((pending := subject.get("pending_semantic_attempt")), dict)
    ]
    if invocation_attempt_id is not None:
        for attempt in pending_attempts:
            if attempt.get("attempt_id") == invocation_attempt_id:
                return attempt
    return pending_attempts[0] if pending_attempts else None



def semantic_attempt_history(
    state: dict[str, object], *, include_history_facts: bool = False
) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    seen: set[str] = set()
    for subject in semantic_attempt_subjects(state):
        history = subject.get("semantic_attempt_history")
        values = history if isinstance(history, list) else []
        pending = subject.get("pending_semantic_attempt")
        if isinstance(pending, dict):
            values = [*values, pending]
        for attempt in values:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id in seen:
                continue
            seen.add(attempt_id)
            attempts.append(history_attempt_view(attempt, include_history_facts))
    # The durable state keeps Attempts on their owning subject.  Accept the
    # compact top-level projection as a read-only legacy/test input as well;
    # this does not change the JSON contract emitted by history.
    projected = state.get("semantic_agent_attempts")
    if isinstance(projected, list):
        for attempt in projected:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id in seen:
                continue
            seen.add(attempt_id)
            attempts.append(history_attempt_view(attempt, include_history_facts))
    return attempts



def history_attempt_view(
    attempt: dict[str, object], include_history_facts: bool
) -> dict[str, object]:
    view = dict(attempt)
    if not include_history_facts:
        # These bounded facts are for the human history projection only.  The
        # machine-readable history keeps the existing Attempt shape.
        view.pop("development_summary", None)
        view.pop("publication", None)
        view.pop("budget_snapshot", None)
        view.pop("history_facts", None)
    return view



def public_invocation_view(
    invocation: dict[str, object] | None,
) -> dict[str, object] | None:
    """Keep private bounded Attempt facts out of machine-facing CLI JSON."""

    if invocation is None:
        return None
    view = dict(invocation)
    semantic_attempt = view.get("semantic_attempt")
    if isinstance(semantic_attempt, dict):
        view["semantic_attempt"] = history_attempt_view(
            semantic_attempt, include_history_facts=False
        )
    return view



