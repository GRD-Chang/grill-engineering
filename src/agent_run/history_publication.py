"""Human history grouping for confirmed final PR preparation."""
from __future__ import annotations

from typing import Any


def group_final_pr_creation(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        steps = events[index:index + 3]
        if len(steps) == 3:
            branch, create, confirmed = steps
            if (
                _write_intent(branch, "ensure_final_run_ref")
                and _write_intent(create, "create_final_pr")
                and _same_publication(branch, create)
                and _same_publication(create, confirmed)
                and type(confirmed.get("pr_number")) is int
                and confirmed.get("phase") in {"waiting_checks", "ready_for_approval"}
                and confirmed.get("kind") in {"required_checks", "approval"}
            ):
                result.append({
                    **confirmed, "kind": "pr_creation",
                    "at": confirmed["at"], "wait_started_at": branch["at"],
                    "wait_observed_until": confirmed["at"],
                    "creation_steps": [
                        {"at": branch["at"], "action": "准备远端分支"},
                        {"at": create["at"], "action": "创建 PR"},
                    ],
                })
                index += 2
                continue
        result.append(events[index])
        index += 1
    return result


def _write_intent(event: dict[str, Any], action: str) -> bool:
    reason = event.get("external_wait_reason")
    return (
        event.get("kind") == "supervision"
        and event.get("status") == "waiting_external"
        and event.get("github_write_action") == action
        and isinstance(reason, dict) and reason.get("code") == "github_write_pending"
    )


def _same_publication(prior: dict[str, Any], event: dict[str, Any]) -> bool:
    # These intents precede the PR number. The immutable Publication Attempt
    # proves their shared owner; display text or adjacency alone cannot do so.
    attempt = prior.get("semantic_attempt_id")
    return (
        isinstance(attempt, str) and bool(attempt)
        and attempt == event.get("semantic_attempt_id")
        and prior.get("object") == event.get("object")
        and all(
            prior[key] == event[key]
            for key in ("history_work_subject", "history_generation", "history_budget_window", "pr_number")
            if prior.get(key) is not None and event.get(key) is not None
        )
    )
