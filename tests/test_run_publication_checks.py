from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import GhGitHubPublisher
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine

from run_acceptance_test_support import (
    _passing_artifact,
    _sync_completed_ticket_integrated_sha,
)

from run_publication_test_support import RunPublicationAgents, _accepted_run

def test_final_check_failure_enters_shared_run_repair(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["fail"]
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    failed_checks = engine.publish(str(state["run_id"]))

    assert failed_checks["status"] == "run_acceptance_pending"
    assert (
        failed_checks["run_acceptance"]["repair_request"]["repair_source"]
        == "required_checks"
    )

@pytest.mark.parametrize(
    "check",
    [
        {
            "name": "cancelled",
            "workflow": "ci",
            "bucket": "cancel",
            "state": "CANCELLED",
            "description": "cancelled",
            "link": "https://example.invalid/checks/cancelled",
        },
        {
            "name": "platform",
            "workflow": "ci",
            "bucket": "fail",
            "state": "TIMED_OUT",
            "description": "runner timeout",
            "link": "https://example.invalid/checks/platform",
        },
        {
            "name": "unknown",
            "workflow": "ci",
            "bucket": "fail",
            "description": "missing conclusion",
            "link": "https://example.invalid/checks/unknown",
        },
    ],
    ids=("cancelled", "platform", "unknown"),
)
def test_final_non_repairable_check_failure_is_supervised_without_candidate(
    git_repo: Path, check: dict[str, str]
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"].update(
        {
            "required_checks": ["fail"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [check],
            },
        }
    )
    before_run = deepcopy(state["run_acceptance"])

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["supervision_window"]["kind"] == "github_convergence"
    assert waiting["run_acceptance"] == before_run
    assert "repair_request" not in waiting["run_acceptance"]

def test_final_check_evidence_for_a_new_head_cannot_repair_the_approved_head(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    publisher.data["delivery"]["required_checks"] = ["fail"]
    publisher._save()

    class HeadDriftEvidencePublisher(FixtureGitHubPublisher):
        repository = "example/project"

        def _checks(self, _pr_number: int, _fields: str) -> list[object]:
            return [
                {
                    "name": "quality",
                    "workflow": "CI",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "description": "Tests failed on a replacement head.",
                    "link": (
                        "https://github.com/example/project/actions/runs/22/job/33"
                    ),
                }
            ]

        def _json(self, *_arguments: str, **_kwargs: object) -> object:
            return {
                "id": 33,
                "head_sha": "b" * 40,
                "name": "quality",
                "workflow_name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Run tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "number": 6,
                    }
                ],
            }

        required_check_evidence = GhGitHubPublisher.required_check_evidence

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=HeadDriftEvidencePublisher(git_repo / "github.json", git),
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert "repair_request" not in waiting["run_acceptance"]
    assert "repair_job" not in waiting["run_acceptance"]
    assert waiting["run_acceptance"]["modification_attempts"] == published[
        "run_acceptance"
    ]["modification_attempts"]

@pytest.mark.parametrize("mergeable", [False, None], ids=("false", "unknown"))
def test_final_mergeability_hint_with_clean_local_merge_starts_fresh_acceptance(
    git_repo: Path, mergeable: bool | None
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["mergeable"] = False
    if mergeable is None:
        original_live_pull_request = publisher.live_pull_request

        def live_pull_request(pr_number: int) -> dict[str, Any]:
            live = original_live_pull_request(pr_number)
            live["mergeable"] = None
            return live

        publisher.live_pull_request = live_pull_request  # type: ignore[method-assign]
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert published["status"] == "run_approval_pending"

    refreshed = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["run_acceptance"]["phase"] == "pending"
    assert "repair_request" not in refreshed["run_acceptance"]
    assert "repair_job" not in refreshed["run_acceptance"]

def test_final_merge_preview_git_failure_does_not_start_integration_repair(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["mergeable"] = False
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    class FailingPreviewGit(GitRepository):
        def prepare_expected_merge_checkout(
            self,
            *,
            default_head_sha: str,
            run_head_sha: str,
            checkout: Path,
        ) -> None:
            del default_head_sha, run_head_sha, checkout
            raise GitError("worktree creation failed")

    with pytest.raises(GitError, match="worktree creation failed"):
        RunPublicationEngine(
            git=FailingPreviewGit(git_repo),
            states=states,
            agents=RunPublicationAgents(),
            github=publisher,
            default_branch="main",
            default_head_sha=git.resolve("main"),
        ).approve(str(state["run_id"]))

    preserved = states.load_run(str(state["run_id"]))
    assert preserved is not None
    assert preserved["status"] == published["status"]
    assert "repair_request" not in preserved["run_acceptance"]
    assert "repair_job" not in preserved["run_acceptance"]

def test_final_real_merge_conflict_enters_shared_run_repair(git_repo: Path) -> None:
    accepted, states, git, publisher = _accepted_run(git_repo)
    run_worktree = git_repo.parent / "conflicting-run"
    subprocess.run(
        ["git", "worktree", "add", str(run_worktree), str(accepted["run_branch"])],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    try:
        (run_worktree / "shared.txt").write_text("run side\n", encoding="utf-8")
        subprocess.run(["git", "add", "shared.txt"], cwd=run_worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add run-side content"],
            cwd=run_worktree,
            check=True,
            capture_output=True,
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(run_worktree)],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
    _sync_completed_ticket_integrated_sha(
        accepted, git, git.resolve(str(accepted["run_branch"]))
    )
    states.save_run(str(accepted["run_id"]), accepted)

    class ConflictRunReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("conflict-run-reviewer", _passing_artifact())

    reviewer = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ConflictRunReviewer(),
        github=publisher,
        default_head_sha=git.resolve("main"),
    )
    refreshed = reviewer.accept(str(accepted["run_id"]))
    if refreshed["run_acceptance"]["phase"] != "accepted":
        refreshed = reviewer.accept(str(accepted["run_id"]))
    assert refreshed["run_acceptance"]["phase"] == "accepted"
    accepted = refreshed
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    engine.publish(str(accepted["run_id"]))
    conflict_path = "shared.txt"
    target = git_repo / conflict_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("default side\n", encoding="utf-8")
    subprocess.run(["git", "add", conflict_path], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add conflicting default content"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    publisher.data["default_head_sha"] = git.resolve("main")
    publisher._save()

    conflicted = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(accepted["run_id"]))

    assert conflicted["status"] == "run_acceptance_pending"
    request = conflicted["run_acceptance"]["repair_request"]
    assert request["repair_source"] == "merge_conflict"
    assert conflict_path in request["merge_conflict_evidence"]

def test_approve_recovers_a_merge_that_succeeded_before_state_save(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    engine.publish(str(state["run_id"]))
    publisher.data["delivery"]["crash_after_normal_merge_once"] = True

    waiting = engine.approve(str(state["run_id"]))
    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"

    recovered = engine.approve(str(state["run_id"]))
    assert recovered["status"] == "completed"
    assert recovered["run_publication"]["phase"] == "merged"
    assert recovered["run_publication"]["merge_intent"]["attempts"] == 1

def test_approve_rejects_external_merge_without_persisted_final_run_intent(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    published = engine.publish(str(state["run_id"]))
    pr_number = int(published["run_publication"]["pr_number"])
    publisher.normal_merge(pr_number=pr_number, expected_head_sha=git.resolve(str(state["run_branch"])))

    with pytest.raises(GitHubReadError, match="persisted merge intent"):
        engine.approve(str(state["run_id"]))

    assert publisher.data["parent"].get("state") != "CLOSED"

def test_pending_check_that_later_fails_enters_shared_run_repair(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["pending", "fail"]
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))
    assert waiting["status"] == "waiting_checks"
    repaired = engine.publish(str(state["run_id"]))

    assert repaired["status"] == "run_acceptance_pending"
    assert (
        repaired["run_acceptance"]["repair_request"]["repair_source"]
        == "required_checks"
    )

def test_unknown_final_required_checks_enters_supervised_wait(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["unknown"]

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert waiting["supervision_window"]["kind"] == "github_convergence"

@pytest.mark.parametrize("bucket", ["skipping", "neutral"])
def test_fixture_non_failure_required_check_buckets_are_pass(
    git_repo: Path, bucket: str
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = [bucket]

    result = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert result["status"] == "run_approval_pending"
