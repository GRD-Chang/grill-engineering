from __future__ import annotations

"""Persist and reconcile the exact narrative boundary of a Change PR write."""

from collections.abc import Callable
from typing import Any

from agent_run.github import GitHubReadError


def admit_change_pr_write(
    *, intent: dict[str, Any] | None, save_intent: Callable[[], None] | None,
    number: int | None, prior: dict[str, str] | None, target: dict[str, str],
) -> bool:
    if intent is None:
        return prior != target
    if save_intent is None:
        raise ValueError("Change PR write intent requires a persistence callback")
    if "target_narrative" not in intent:
        intent.update({
            "pr_number": number, "prior_narrative": prior,
            "target_narrative": target,
        })
        save_intent()
    if intent["target_narrative"] != target:
        raise GitHubReadError(
            "change_pr_narrative_intent_changed",
            "Publish narrative no longer matches the saved Change PR write intent",
        )
    saved_number = intent.get("pr_number")
    if saved_number is not None and saved_number != number:
        raise GitHubReadError(
            "change_pr_identity_mismatch",
            "Original Change PR is missing or was replaced; preserving its write intent",
        )
    if prior == target:
        return False
    if prior == intent.get("prior_narrative"):
        return True
    raise GitHubReadError(
        "change_pr_narrative_outcome_unknown",
        "Change PR narrative matches neither the saved prior nor target value; "
        "preserving the write intent without overwriting external changes",
    )
