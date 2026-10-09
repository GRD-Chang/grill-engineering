"""Settle a sent Change PR write before replacing its publication boundary."""

from __future__ import annotations

from typing import Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.github import GitHubReadError
from agent_run.git_errors import GitError
from agent_run.pr_write import observe_pr_write


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
        observed = observe_pr_write(
            github=stage.github, repository=state["repository"],
            authority=intent, intent=intent,
        )
        number = observed.number
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
