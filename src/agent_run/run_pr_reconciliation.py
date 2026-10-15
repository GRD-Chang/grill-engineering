"""Observe the original Final Run PR write before changing its version boundary."""

from __future__ import annotations

from typing import Any, Callable

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.github import GitHubReadError
from agent_run.git import GitError
from agent_run.error_safety import bounded_error
from agent_run.pr_write import observe_pr_write


def reconcile_run_pr_write(
    state: dict[str, Any], github: GitHubPublisher | None,
    save: Callable[[dict[str, Any]], Any],
    *, current_head_sha: str | None = None,
) -> bool:
    publication = state.get("run_publication")
    if not isinstance(publication, dict):
        return True
    intent = publication.get("write_intent")
    if not isinstance(intent, dict) or intent.get("action") not in {
        "create_final_pr", "refresh_final_pr_narrative",
    }:
        return True
    try:
        authority = intent.get("authority")
        authority = authority if isinstance(authority, dict) else {}
        observation = observe_pr_write(
            github=github, repository=str(state.get("repository")),
            authority=authority, intent=intent,
        )
        number = observation.number
        retired = publication.get("phase") == "stale" or (
            current_head_sha is not None and current_head_sha != authority["head_sha"]
        )
        if number is None:
            assert github is not None
            _, base = github.live_branch_heads(str(authority["branch"]), str(authority["base_branch"]))
            retired = retired or base != authority["base_sha"]
        else:
            retired = retired or observation.base_sha != authority["base_sha"]
        previous = dict(publication)
        if retired or (number is not None and publication.get("pr_number") is None):
            if number is not None:
                publication["pr_number"] = number
            if retired:
                publication.pop("write_intent")
                publication["phase"] = "stale"
            try:
                save(state)
            except (OSError, TimeoutError):
                publication.clear()
                publication.update(previous)
                raise
        # Only proven prior/target readback permits retiring the old operation.
        # Normal publication consumes an unchanged-boundary intent itself.
        return True
    except (GitHubReadError, GitError, OSError, TimeoutError) as error:
        publication.update(
            phase="ready_for_human", pause_resume_phase="waiting_external",
            blocked_reason="github_write_outcome_unknown",
        )
        state.update(
            status="ready_for_human", terminal_kind="waiting_human",
            diagnostics=[{"code": "github_write_outcome_unknown", "message": bounded_error(str(error))}],
        )
        save(state)
        return False
