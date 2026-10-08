"""Original Final Run authority survives incomplete or conflicting readback."""
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.github import GitHubReadError
from agent_run.run_pr_reconciliation import reconcile_run_pr_write


@pytest.mark.parametrize("observation", [
    "prior", "target", "prior_advanced", "target_advanced", "third", "closed", "foreign_head", "foreign_base",
    "missing_target", "missing_authority", "missing_publisher", "missing_pr", "read_failure", "save_failure",
])
def test_original_write_is_checked_without_remote_mutation(observation: str) -> None:
    intent: dict[str, Any] = {
        "action": "refresh_final_pr_narrative", "pr_number": 7,
        "authority": {"branch": "run/1", "head_sha": "original-head",
                      "base_branch": "main", "base_sha": "old-base"},
        "prior_narrative": {"title": "Prior", "body": "Prior body"},
        "target_narrative": {"title": "Target", "body": "Target body"},
    }
    if observation == "missing_target":
        intent.pop("target_narrative")
    if observation == "missing_authority":
        intent["authority"] = {}
    if observation == "missing_pr":
        intent.pop("pr_number")
    state: dict[str, Any] = {
        "repository": "example/repo", "status": "waiting_external",
        "run_publication": {"phase": "waiting_external", "artifact": {"pr_title": "Target"},
                            "write_intent": intent},
    }
    original = deepcopy(state["run_publication"])
    live = {
        "state": "CLOSED" if observation == "closed" else "OPEN",
        "head_branch": "run/1",
        "head_sha": "foreign" if observation == "foreign_head" else "original-head",
        "base_branch": "foreign" if observation == "foreign_base" else "main",
        "base_sha": "advanced-base" if observation.endswith("_advanced") or observation == "save_failure" else "old-base", "head_repository": "example/repo",
        "base_repository": "example/repo",
    }
    def read(_number: int) -> dict[str, str]:
        if observation == "read_failure":
            raise GitHubReadError("github_timeout", "Read unavailable")
        return intent.get(
            f"{observation.split('_')[0]}_narrative",
            intent["target_narrative"] if observation == "save_failure" else {"title": "External", "body": "External body"},
        )

    publisher = SimpleNamespace(
        live_pull_request=lambda _number: live,
        read_run_pr_narrative=read,
        find_change_pr_number=lambda **_kwargs: None,
    )  # Read methods only: this gate must not admit a new write.
    saves: list[Any] = []

    def save(current: dict[str, Any]) -> None:
        saves.append(deepcopy(current))
        if observation == "save_failure" and len(saves) == 1:
            raise OSError("Readback checkpoint lost")

    settled = reconcile_run_pr_write(
        state, None if observation == "missing_publisher" else publisher,
        save,
    )
    assert settled is (observation in {"prior", "target", "prior_advanced", "target_advanced"})
    publication = state["run_publication"]
    if observation.endswith("_advanced"):
        assert "write_intent" not in publication
        assert publication["phase"] == "stale"
    else:
        assert publication["write_intent"] == original["write_intent"]
    assert publication["artifact"] == original["artifact"]
    if settled:
        assert state["status"] == "waiting_external"
        assert publication["pr_number"] == 7
        assert len(saves) == 1
    else:
        assert state["status"] == "ready_for_human"
        assert publication["pause_resume_phase"] == "waiting_external"
        assert publication["blocked_reason"] == "github_write_outcome_unknown"
        assert len(saves) == (2 if observation == "save_failure" else 1)


@pytest.mark.parametrize("observation", ["absent", "target", "third", "closed", "ambiguous"])
def test_lost_create_uses_unique_original_pr_lookup_before_retiring(observation: str) -> None:
    target = {"title": "Target", "body": "Target body"}
    intent = {
        "action": "create_final_pr",
        "authority": {"branch": "run/1", "head_sha": "original-head",
                      "base_branch": "main", "base_sha": "old-base"},
        "target_narrative": target,
    }
    state: dict[str, Any] = {
        "repository": "example/repo", "status": "waiting_external",
        "run_publication": {"phase": "waiting_external", "write_intent": intent},
    }
    lookups: list[str] = []

    def lookup(*, branch: str) -> int | None:
        lookups.append(branch)
        if observation == "ambiguous":
            raise GitHubReadError("ambiguous_change_pr", "Multiple PRs found")
        return None if observation == "absent" else 7

    publisher = SimpleNamespace(
        find_change_pr_number=lookup,
        live_branch_heads=lambda *_args: ("original-head", "advanced-base"),
        live_pull_request=lambda _number: {
            "state": "CLOSED" if observation == "closed" else "OPEN",
            "head_branch": "run/1", "head_sha": "original-head",
            "base_branch": "main", "base_sha": "advanced-base",
            "head_repository": "example/repo", "base_repository": "example/repo",
        },
        read_run_pr_narrative=lambda _number: (
            {"title": "External", "body": "External body"} if observation == "third" else target
        ),
    )
    saved: list[Any] = []
    settled = reconcile_run_pr_write(state, publisher, lambda current: saved.append(deepcopy(current)))
    publication = state["run_publication"]
    assert lookups == ["run/1"]
    assert len(saved) == 1
    assert settled is (observation in {"absent", "target"})
    if settled:
        assert "write_intent" not in publication
        assert publication["phase"] == "stale"
        assert publication.get("pr_number") == (7 if observation == "target" else None)
    else:
        assert publication["write_intent"] == intent
        assert state["status"] == "ready_for_human"
