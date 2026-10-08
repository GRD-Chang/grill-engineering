"""Settle a sent Change PR write before replacing its publication boundary."""

from __future__ import annotations

from typing import Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.github import GitHubReadError
from agent_run.git_errors import GitError


def reconcile_pending_pr_write(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any],
) -> bool:
    intent = job.get("ticket_write_intent")
    if not isinstance(intent, dict) or intent.get("action") != "ensure_ticket_pr":
        return True
    # The narrative snapshot is saved before admitting a GitHub write. A bare
    # ref intent has not yet crossed that boundary; ordinary publication owns it.
    if "target_narrative" not in intent:
        return True
    try:
        number = intent.get("pr_number")
        if number is None:
            number = stage.github.find_change_pr_number(branch=str(intent["branch"]))
        if number is None:
            if intent.get("prior_narrative") is not None:
                raise GitHubReadError("change_pr_identity_mismatch", "Original Change PR is missing")
        else:
            if type(number) is not int:
                raise GitHubReadError("github_invalid_response", "Invalid original Change PR number")
            live = stage.github.live_pull_request(number)
            if not (
                live.get("state") == "OPEN"
                and live.get("head_branch") == intent.get("branch")
                and live.get("head_sha") == intent.get("head_sha")
                and live.get("head_repository") == state.get("repository")
                and live.get("base_branch") == intent.get("base_branch")
                and live.get("base_repository") == state.get("repository")
            ):
                raise GitHubReadError(
                    "change_pr_identity_mismatch", "Original Change PR no longer matches its saved write identity",
                )
            # Target advancement cannot authorize a replay of this old write.
            # Only its exact PR and prior/target text can settle its outcome;
            # the normal integration gate observes the new target afterwards.
            narrative = stage.github.read_run_pr_narrative(number)
            if narrative != intent["target_narrative"] and narrative != intent.get("prior_narrative"):
                raise GitHubReadError(
                    "change_pr_narrative_outcome_unknown",
                    "Original Change PR matches neither its saved prior nor target narrative; "
                    "reconcile this write before preparing another publication",
                )
    except (GitHubReadError, GitError, OSError, TimeoutError) as error:
        stage._block(state, job, "change_pr_write_reconciliation_pending", str(error))
        return False

    previous_number = job.get("pr_number")
    had_number = "pr_number" in job
    if number is not None:
        job["pr_number"] = number
    job.pop("ticket_write_intent")
    try:
        stage.save(state)
    except (OSError, TimeoutError):
        job["ticket_write_intent"] = intent
        if had_number:
            job["pr_number"] = previous_number
        else:
            job.pop("pr_number", None)
        raise
    return True
