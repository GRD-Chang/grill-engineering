"""Generation-local immutable Human Response context."""

from __future__ import annotations

from typing import Any


def append_human_response(
    subject: dict[str, Any],
    blockers: list[str],
    response: str | None,
    *,
    generation: int,
) -> None:
    """Append an operator response to the subject's current generation only."""
    if response is None:
        return
    history = subject.get("human_response_history")
    if history is None:
        history = []
        subject["human_response_history"] = history
    if not isinstance(history, list):
        raise ValueError("human_response_history must be an array")
    if subject.get("human_response_generation") != generation:
        # A replacement Job Generation must never receive an older generation's
        # maintainer context.  Histories are immutable within their generation.
        history = []
        subject["human_response_history"] = history
        subject["human_response_generation"] = generation
    history.append(
        {
            "generation": generation,
            "human_blockers": list(blockers),
            "response": response,
        }
    )


def current_human_response_history(
    subject: dict[str, Any], *, generation: int
) -> list[dict[str, Any]] | None:
    """Return the immutable response sequence only when it matches `generation`."""
    if subject.get("human_response_generation") != generation:
        return None
    history = subject.get("human_response_history")
    if not isinstance(history, list):
        return None
    if not all(
        isinstance(entry, dict) and entry.get("generation") == generation
        for entry in history
    ):
        raise ValueError("human_response_history has mixed Job Generations")
    return history
