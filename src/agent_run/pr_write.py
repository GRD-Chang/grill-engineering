"""Exact PR narrative admission and readback shared by Change and Final Run.

Callers own currentness, retry policy and intent retirement. This module only
decides whether the original operation completed or its prior value remains.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.github import GitHubReadError


@dataclass(frozen=True)
class PRWriteObservation:
    number: int | None
    completed: bool
    base_sha: str | None


def admit_pr_write(
    *, intent: dict[str, Any] | None, save_intent: Callable[[], None] | None,
    number: int | None, prior: dict[str, str] | None, target: dict[str, str],
) -> bool:
    """Save the original identity/text before permitting one exact mutation."""
    if intent is None:
        return prior != target
    if save_intent is None:
        raise ValueError("PR write intent requires a persistence callback")
    if "target_narrative" not in intent:
        intent.update(pr_number=number, prior_narrative=prior, target_narrative=target)
        save_intent()
    if intent["target_narrative"] != target:
        raise GitHubReadError(
            "github_write_outcome_unknown", "Publish narrative differs from the saved PR write intent",
        )
    saved_number = intent.get("pr_number")
    if saved_number is not None and saved_number != number:
        raise GitHubReadError(
            "github_write_outcome_unknown", "Original PR is missing or was replaced; preserving its write intent",
        )
    if prior == target:
        return False
    if prior == intent.get("prior_narrative"):
        return True
    raise GitHubReadError(
        "github_write_outcome_unknown", "PR narrative matches neither the saved prior nor target value",
    )


def observe_pr_write(
    *, github: GitHubPublisher | None, repository: str,
    authority: dict[str, Any], intent: dict[str, Any],
) -> PRWriteObservation:
    """Read the original operation, never mutating or adopting external text."""
    target = intent.get("target_narrative")
    if (
        github is None
        or not all(isinstance(authority.get(key), str) and authority[key] for key in (
            "branch", "head_sha", "base_branch", "base_sha",
        ))
        or not isinstance(target, dict)
        or not all(isinstance(target.get(key), str) for key in ("title", "body"))
    ):
        raise GitHubReadError(
            "github_write_outcome_unknown", "Original PR write requires saved authority, target text and GitHub readback",
        )
    number = intent.get("pr_number")
    if number is None:
        number = github.find_change_pr_number(branch=authority["branch"])
    if number is None:
        if intent.get("prior_narrative") is not None or intent.get("action") == "refresh_final_pr_narrative":
            raise GitHubReadError("github_write_outcome_unknown", "Original PR is missing")
        return PRWriteObservation(None, False, None)
    if type(number) is not int:
        raise GitHubReadError("github_write_outcome_unknown", "Original PR number is invalid")
    live = github.live_pull_request(number)
    if not (
        live.get("state") == "OPEN"
        and live.get("head_branch") == authority["branch"]
        and live.get("head_sha") == authority["head_sha"]
        and live.get("head_repository") == repository
        and live.get("base_branch") == authority["base_branch"]
        and live.get("base_repository") == repository
    ):
        raise GitHubReadError(
            "github_write_outcome_unknown", "Original PR no longer matches its saved write identity",
        )
    narrative = github.read_run_pr_narrative(number)
    completed = narrative == target
    if not completed and narrative != intent.get("prior_narrative"):
        raise GitHubReadError(
            "github_write_outcome_unknown", "Original PR matches neither its saved prior nor target narrative",
        )
    base = live.get("base_sha")
    return PRWriteObservation(number, completed, base if isinstance(base, str) else None)
