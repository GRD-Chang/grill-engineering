"""Observe the original Final Run PR write before changing its version boundary."""

from __future__ import annotations

from typing import Any, Callable

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.github import GitHubReadError
from agent_run.git import GitError
from agent_run.error_safety import bounded_error


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
        if github is None:
            raise GitHubReadError("github_write_outcome_unknown", "Original PR write requires GitHub readback")
        authority = intent.get("authority")
        target = intent.get("target_narrative")
        if (
            not isinstance(authority, dict)
            or not all(isinstance(authority.get(key), str) and authority[key] for key in (
                "branch", "head_sha", "base_branch", "base_sha",
            ))
            or not isinstance(target, dict)
            or not all(isinstance(target.get(key), str) for key in ("title", "body"))
        ):
            raise GitHubReadError("github_write_outcome_unknown", "Original PR write is missing its saved authority or target narrative")
        number = intent.get("pr_number", publication.get("pr_number"))
        if number is None:
            number = github.find_change_pr_number(branch=str(authority["branch"]))
        retired = publication.get("phase") == "stale" or (
            current_head_sha is not None and current_head_sha != authority["head_sha"]
        )
        if number is None:
            if intent.get("action") != "create_final_pr":
                raise GitHubReadError("github_write_outcome_unknown", "Original Final Run PR is missing")
            _, base = github.live_branch_heads(str(authority["branch"]), str(authority["base_branch"]))
            retired = retired or base != authority["base_sha"]
        else:
            if type(number) is not int:
                raise GitHubReadError("github_write_outcome_unknown", "Original Final Run PR number is invalid")
            live = github.live_pull_request(number)
            if not (
                live.get("state") == "OPEN"
                and live.get("head_branch") == authority.get("branch")
                and live.get("head_sha") == authority.get("head_sha")
                and live.get("head_repository") == state.get("repository")
                and live.get("base_branch") == authority.get("base_branch")
                and live.get("base_repository") == state.get("repository")
            ):
                raise GitHubReadError("github_write_outcome_unknown", "Original Final Run PR no longer matches its saved write identity")
            # The default branch may advance. It cannot authorize replacing
            # the old intent or treating externally edited text as a new prior.
            narrative = github.read_run_pr_narrative(number)
            if narrative != target and narrative != intent.get("prior_narrative"):
                raise GitHubReadError("github_write_outcome_unknown", "Original Final Run PR matches neither its saved prior nor target narrative")
            retired = retired or live.get("base_sha") != authority["base_sha"]
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
