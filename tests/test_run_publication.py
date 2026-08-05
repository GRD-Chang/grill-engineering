from __future__ import annotations

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine

from test_run_acceptance import _completed_run, _passing_artifact
from test_cli import run_cli, stdout_json


class RunPublicationAgents:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def run_publication(self, request: dict[str, Any]) -> dict[str, str]:
        self.requests.append(request)
        return {
            "commit_message": "feat(run): publish the completed delivery",
            "pr_title": "feat(run): publish the completed delivery",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe accepted Run needs a human merge boundary.\n\n"
                "## Why This Change Was Made\n\nIt makes the completed delivery reviewable.\n\n"
                "## User Impact\n\nMaintainers can inspect and approve one final PR.\n\n"
                "## Evidence\n\nFresh Run Acceptance passed.\n\n"
                "## Completed Tickets\n\n#2 was accepted and integrated.\n\n"
                "## Known Limitations\n\nNone known.\n\n"
                "## Validation Results\n\nThe fixture acceptance lanes all passed."
            ),
        }


class PassingRunReviewer:
    def review(self, request: dict[str, Any]) -> ReviewResult:
        del request
        return ReviewResult("run-reviewer", _passing_artifact())


def _accepted_run(git_repo: Path) -> tuple[dict[str, Any], Any, Any, FixtureGitHubPublisher]:
    state, states, git = _completed_run(git_repo)
    tree = git.resolve(f"{state['run_branch']}^{{tree}}")
    integrated = subprocess.run(
        [
            "git", "commit-tree", tree, "-p", str(state["run_branch"]),
            "-m", "feat(ticket): integrated accepted ticket",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{state['run_branch']}", integrated],
        cwd=git_repo,
        check=True,
    )
    state["ticket_jobs"]["2"]["integrated_sha"] = integrated
    states.save_run(str(state["run_id"]), state)
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=PassingRunReviewer(),
        github=publisher,
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    return accepted, states, git, publisher


def test_publish_then_explicit_approve_creates_one_normal_merge_commit(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    published = engine.publish(str(state["run_id"]))

    assert published["status"] == "run_approval_pending"
    final = published["run_publication"]
    assert final["phase"] == "ready_for_approval"
    assert len(agents.requests) == 1
    assert final["record"]["run_head_sha"] == git.resolve(str(state["run_branch"]))
    assert final["record"]["default_head_sha"] == git.resolve("main")
    assert publisher.live_pull_request(int(final["pr_number"]))["state"] == "OPEN"
    pull = publisher.data["delivery"]["pull_requests"][0]
    assert pull["body"].startswith(
        "Parent Issue: #1\nDelivery Type: Final Run\n\n"
    )
    assert publisher.data["delivery"]["agent_run_status"] == [
        {
            "pr_number": final["pr_number"],
            "scope": "final-run",
            "base_sha": git.resolve("main"),
            "candidate_sha": git.resolve(str(state["run_branch"])),
            "validation_verdict": "pass",
            "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
            "required_checks": "none",
            "next_action": "await explicit maintainer approval",
        }
    ]

    completed = engine.approve(str(state["run_id"]))

    assert completed["status"] == "completed"
    merged = publisher.live_pull_request(int(final["pr_number"]))
    assert merged["state"] == "MERGED"
    assert merged["integrated_parents"] == [
        final["record"]["default_head_sha"],
        final["record"]["run_head_sha"],
    ]


def test_approve_rejects_default_branch_drift_without_merging(git_repo: Path) -> None:
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
    (git_repo / "drift.txt").write_text("default advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "drift.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert result["status"] == "run_acceptance_pending"
    assert result["run_acceptance"]["phase"] == "pending"
    assert publisher.live_pull_request(
        int(published["run_publication"]["pr_number"])
    )["state"] == "OPEN"


def test_revise_and_abandon_preserve_audit_but_stop_future_mutation(
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

    revised = engine.revise(str(state["run_id"]), "请补充默认分支兼容性。")

    assert revised["status"] == "run_acceptance_pending"
    repair = revised["run_acceptance"]["repair_request"]
    assert repair["repair_source"] == "human_revision"
    assert repair["human_feedback"] == "请补充默认分支兼容性。"
    assert revised["run_acceptance"]["modification_attempts"] == 0

    temporary = states.root / "worktrees" / str(state["run_id"]) / "temporary"
    temporary.mkdir(parents=True)
    abandoned = engine.abandon(str(state["run_id"]))

    assert abandoned["status"] == "abandoned"
    assert abandoned["run_publication"]["phase"] == "abandoned"
    assert not temporary.exists()
    assert publisher.live_pull_request(
        int(published["run_publication"]["pr_number"])
    )["state"] == "CLOSED"
    resumed = run_cli(
        git_repo, git_repo / "github.json", "accept-run", str(state["run_id"])
    )
    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "abandoned"
    confirmed = run_cli(
        git_repo,
        git_repo / "github.json",
        "confirm-structure",
        str(state["run_id"]),
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert stdout_json(confirmed)["status"] == "abandoned"
    assert not Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).record_execution_failure(str(state["run_id"]), "must not overwrite abandonment")
    assert states.load_run(str(state["run_id"]))["status"] == "abandoned"


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
    assert failed_checks["run_acceptance"]["repair_request"]["repair_source"] == "required_checks"


def test_final_merge_conflict_enters_shared_run_repair(git_repo: Path) -> None:
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
    assert published["status"] == "run_approval_pending"

    conflicted = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert conflicted["status"] == "run_acceptance_pending"
    assert conflicted["run_acceptance"]["repair_request"]["repair_source"] == "merge_conflict"


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

    with pytest.raises(OSError, match="lost response"):
        engine.approve(str(state["run_id"]))

    recovered = engine.approve(str(state["run_id"]))
    assert recovered["status"] == "completed"
    assert recovered["run_publication"]["phase"] == "merged"


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
    assert repaired["run_acceptance"]["repair_request"]["repair_source"] == "required_checks"


def test_retries_a_worker_interrupted_before_publication_artifact(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    class InterruptedPublication(RunPublicationAgents):
        def run_publication(self, request: dict[str, Any]) -> dict[str, str]:
            del request
            raise OSError("simulated publication interruption")

    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=InterruptedPublication(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    with pytest.raises(OSError, match="interruption"):
        engine.publish(str(state["run_id"]))

    retried = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert retried["status"] == "run_approval_pending"


def test_closed_final_pr_is_not_replaced_with_a_second_pr(git_repo: Path) -> None:
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
    publisher.data["delivery"]["pull_requests"][0]["state"] = "CLOSED"
    published["run_publication"]["phase"] = "publishing"
    states.save_run(str(state["run_id"]), published)

    with pytest.raises(ValueError, match="not open"):
        engine.publish(str(state["run_id"]))
    assert len(publisher.data["delivery"]["pull_requests"]) == 1


def test_revise_is_rejected_outside_a_human_gate(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    with pytest.raises(ValueError, match="explicit human gate"):
        RunPublicationEngine(
            git=git,
            states=states,
            agents=RunPublicationAgents(),
            github=publisher,
            default_branch="main",
            default_head_sha=git.resolve("main"),
        ).revise(str(state["run_id"]), "too early")


def test_public_cli_publish_then_approve_is_an_end_to_end_user_flow(
    git_repo: Path,
) -> None:
    state, _states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "run-publication-agents.json"
    artifact = RunPublicationAgents().run_publication({"run_id": state["run_id"]})
    agents.write_text(json.dumps({"run_publications": [artifact]}), encoding="utf-8")

    published = run_cli(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert published.returncode == 0, published.stderr
    assert stdout_json(published)["status"] == "run_approval_pending"
    approved = run_cli(
        git_repo, git_repo / "github.json", "approve", str(state["run_id"])
    )
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    assert FixtureGitHubPublisher(git_repo / "github.json", _git).live_pull_request(1)["state"] == "MERGED"
