"""Final Run merge outcome reconciliation with real local Git commits."""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication import RunPublicationEngine
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run


def _advance_default(publisher: FixtureGitHubPublisher) -> None:
    git = publisher.git
    advanced = subprocess.run(
        ["git", "commit-tree", git.resolve("main^{tree}"), "-p", git.resolve("main"),
         "-m", "External default branch advance"],
        cwd=git.root, text=True, capture_output=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", advanced], cwd=git.root, check=True)
    publisher.data["default_head_sha"] = advanced
    publisher.data["delivery"]["published_branches"]["main"] = advanced
    publisher._save()


@pytest.mark.parametrize("observation", [
    "merged", "open", "open_after_another_advance", "merged_during_revalidation",
])
def test_merge_readback_keeps_the_original_approval_after_the_base_advances(
    git_repo: Path, observation: str,
) -> None:
    state, states, git, _ = _accepted_run(git_repo)

    class LostMergeResponse(FixtureGitHubPublisher):
        def normal_merge(self, **authority: Any) -> str:
            super().normal_merge(**authority)
            raise OSError("merge applied before response")

    publisher = LostMergeResponse(git_repo / "github.json", git)
    engine = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])
    engine.publish(run_id)
    waiting = engine.approve(run_id)
    assert waiting["status"] == "waiting_external"
    pending = states.load_current_run(run_id)
    original = deepcopy(pending["run_publication"])
    assert original["approval_grant"]["base_sha"] != git.resolve("main")

    if observation != "merged":
        if observation == "merged_during_revalidation":
            assert not engine._approval()._revalidate_final_run_pr_before_merge(
                pending, pending["run_publication"], original["pr_number"],
                original["record"]["run_head_sha"],
            )
            lagging = states.load_current_run(run_id)
        else:
            publisher.data["delivery"]["merged_live_pull_request_state_overrides"] = {
                "final_run": ["OPEN"],
            }
            if observation == "open_after_another_advance":
                _advance_default(publisher)
            publisher._save()
            lagging = RunPublicationEngine(
                git=git, states=states, agents=RunPublicationAgents(), github=publisher,
                default_branch="main", default_head_sha=git.resolve("main"),
            ).approve(run_id)
        assert lagging["status"] == "waiting_external"
        assert lagging["run_publication"]["approval_grant"] == original["approval_grant"]
        assert lagging["run_publication"]["merge_intent"] == original["merge_intent"]
        assert lagging["run_publication"].get("approval_update") is None
        assert lagging["run_acceptance"] == pending["run_acceptance"]
        assert lagging["ticket_jobs"] == pending["ticket_jobs"]
        assert lagging["agent_invocation_history"] == pending["agent_invocation_history"]
        assert 1 not in publisher.data["delivery"]["closed_issues"]

    resumed = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_branch="main", default_head_sha=git.resolve("main"),
    ).approve(run_id)
    assert resumed["status"] == "completed"
    assert resumed["run_publication"]["approval_grant"] == original["approval_grant"]
    assert resumed["run_publication"]["merge_intent"]["attempts"] == 1
    assert resumed["run_acceptance"] == pending["run_acceptance"]


def test_unapplied_merge_with_external_default_advance_enters_update(git_repo: Path) -> None:
    state, states, git, _ = _accepted_run(git_repo)

    class UnappliedMerge(FixtureGitHubPublisher):
        def normal_merge(self, **authority: Any) -> str:
            raise OSError("merge response unavailable, request not applied")

    publisher = UnappliedMerge(git_repo / "github.json", git)
    engine = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])
    engine.publish(run_id)
    waiting = engine.approve(run_id)
    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["merge_intent"]["attempts"] == 1
    _advance_default(publisher)

    updated = engine.approve(run_id)

    assert updated["status"] == "run_acceptance_pending"
    assert updated["run_publication"].get("approval_grant") is None
    assert updated["run_publication"].get("merge_intent") is None
    assert updated["run_publication"]["approval_update"]["target_sha"] == git.resolve("main")
    assert updated["ticket_jobs"] == waiting["ticket_jobs"]
    assert 1 not in publisher.data["delivery"]["closed_issues"]
