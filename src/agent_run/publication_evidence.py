"""Applicability of Publication results to their original delivery facts."""

from __future__ import annotations

from typing import Any

from agent_run.semantic_attempt import canonical_fingerprint


def publication_is_current(
    owner: dict[str, Any],
    artifact: object,
    current_boundary: dict[str, Any],
    *,
    recorded_boundary: object = None,
) -> bool:
    """Use the existing Attempt snapshot, never an unchanged tree alone.

    A pending result belongs to the pending Publication Attempt. Completed
    results must match both that Attempt's result and its input facts. A
    pre-existing record can supply the boundary only when there is no
    Publication Attempt; it cannot override a stale or different result.
    """
    if not isinstance(artifact, dict):
        return False
    pending = owner.get("pending_semantic_attempt")
    if isinstance(pending, dict) and pending.get("role") == "publication":
        return pending.get("currentness_boundary_fingerprint") == canonical_fingerprint(current_boundary)
    history = owner.get("semantic_attempt_history", [])
    if not isinstance(history, list):
        return False
    for attempt in reversed(history):
        if isinstance(attempt, dict) and attempt.get("role") == "publication":
            return (
                attempt.get("outcome") == "publication_artifact"
                and attempt.get("publication") == artifact
                and attempt.get("currentness_boundary_fingerprint") == canonical_fingerprint(current_boundary)
            )
    return isinstance(recorded_boundary, dict) and recorded_boundary == current_boundary
