"""Canonical bounded retry state for Publisher-owned external operations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


MAX_PUBLICATION_OPERATION_ATTEMPTS = 5


def require_publication_operation_retry(
    value: object, *, location: str = "publication_operation_retry"
) -> dict[str, int]:
    """Return one exact canonical retry object or reject it."""

    if not isinstance(value, dict) or set(value) != {"attempts", "limit"}:
        raise ValueError(f"{location} must contain exactly attempts and limit")
    attempts = value.get("attempts")
    limit = value.get("limit")
    if (
        type(attempts) is not int
        or type(limit) is not int
        or attempts < 1
        or limit != MAX_PUBLICATION_OPERATION_ATTEMPTS
        or attempts > limit
    ):
        raise ValueError(f"{location} has invalid bounded counters")
    return {"attempts": attempts, "limit": limit}


def record_publication_operation_failure(
    owner: dict[str, Any], error: Exception
) -> bool:
    """Increment one canonical retry and report whether it is exhausted."""

    value = owner.get("publication_operation_retry")
    if value is None:
        prior_attempts = 0
    else:
        retry = require_publication_operation_retry(value)
        prior_attempts = retry["attempts"]
    attempts = prior_attempts + 1
    if attempts > MAX_PUBLICATION_OPERATION_ATTEMPTS:
        raise ValueError("Publication Operation Retry is already exhausted")
    owner["publication_operation_retry"] = {
        "attempts": attempts,
        "limit": MAX_PUBLICATION_OPERATION_ATTEMPTS,
    }
    _sync_completed_publication_attempt(owner)
    owner["last_publication_error"] = str(error)
    return attempts >= MAX_PUBLICATION_OPERATION_ATTEMPTS


def begin_publication_operation_attempt(owner: dict[str, Any]) -> None:
    """Archive the prior aggregate before a fresh Publication Attempt."""

    if owner.get("publication_operation_retry") is None:
        return
    _sync_completed_publication_attempt(owner, required=True)
    owner.pop("publication_operation_retry", None)
    owner.pop("last_publication_error", None)


def publication_operation_attempt(
    owner: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the exact Attempt that owns the live retry aggregate."""

    pending = owner.get("pending_semantic_attempt")
    if isinstance(pending, dict) and pending.get("role") == "publication":
        return pending
    current_ordinal = owner.get("publication_attempts")
    history = owner.get("semantic_attempt_history")
    if isinstance(history, list):
        for attempt in reversed(history):
            if (
                isinstance(attempt, dict)
                and attempt.get("role") == "publication"
                and attempt.get("status") == "completed"
                and attempt.get("ordinal") == current_ordinal
            ):
                return attempt
    return None


def _sync_completed_publication_attempt(
    owner: dict[str, Any], *, required: bool = False
) -> None:
    value = owner.get("publication_operation_retry")
    if value is None:
        return
    retry = require_publication_operation_retry(value)
    attempt = publication_operation_attempt(owner)
    if attempt is not None and attempt.get("status") == "completed":
        attempt["publication_operation_retry"] = deepcopy(retry)
        return
    if attempt is not None:
        return
    if required:
        raise ValueError("Publication Operation Retry has no completed Attempt owner")
