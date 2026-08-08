from __future__ import annotations

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.codex import CodexProcessError
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
                "## Evidence\n\nFresh Run Acceptance passed."
            ),
        }


class PassingRunReviewer:
    def review(self, request: dict[str, Any]) -> ReviewResult:
        del request
        return ReviewResult("run-reviewer", _passing_artifact())


class InterruptedRunPublisher(FixtureGitHubPublisher):
    def ensure_run_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        del branch, base_branch, title, body
        raise OSError("simulated Publisher interruption")


class HumanThenRunPublicationAgents:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if len(self.requests) == 1:
            return {
                "human_blockers": [
                    "GitHub denied access; tried gh issue view; grant Issue read access."
                ],
                "_thread_id": "blocked-publication-thread",
            }
        assert request["thread_id"] == "blocked-publication-thread"
        assert request["prior_human_blockers"] == [
            "GitHub denied access; tried gh issue view; grant Issue read access."
        ]
        return {
            "commit_message": "feat(run): publish the completed delivery",
            "pr_title": "feat(run): publish the completed delivery",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe accepted Run needs publication.\n\n"
                "## Why This Change Was Made\n\nAccess has been restored.\n\n"
                "## User Impact\n\nMaintainers can approve the Run.\n\n"
                "## Evidence\n\nThe original thread rechecked GitHub."
            ),
            "_thread_id": "blocked-publication-thread",
        }


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
    assert merged["integrated_tree"] == final["record"]["expected_merge_tree"]
    assert 1 in publisher.data["delivery"]["closed_issues"]
    assert publisher.data["parent"]["state"] == "CLOSED"


def test_final_publication_human_resume_clears_current_blocker(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = HumanThenRunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    blocked = engine.publish(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    assert publisher.data["delivery"]["pull_requests"] == []
    history = stdout_json(
        run_cli(
            git_repo,
            git_repo / "github.json",
            "history",
            str(state["run_id"]),
            "--json",
        )
    )
    assert any(
        event.get("worker") == "运行发布工作代理"
        and event.get("thread_id") == "blocked-publication-thread"
        and event.get("human_blockers")
        for event in history["timeline"]
    )

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), resume_human_blocker=True)
    assert resumed["run_publication"]["prior_human_blockers"] == [
        "GitHub denied access; tried gh issue view; grant Issue read access."
    ]

    published = engine.publish(str(state["run_id"]))

    assert published["status"] == "run_approval_pending"
    publication = published["run_publication"]
    assert publication["thread_id"] == "blocked-publication-thread"
    assert publication["human_blocker_history"] == [
        {
            "phase": "pending",
            "human_blockers": [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ],
        }
    ]
    for key in ("human_blockers", "human_blocker_phase", "prior_human_blockers"):
        assert key not in publication


def test_final_run_pr_renders_completed_ticket_links(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    state["ticket_jobs"]["2"]["pr_number"] = 42
    states.save_run(str(state["run_id"]), state)
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    engine.publish(str(state["run_id"]))

    body = publisher.data["delivery"]["pull_requests"][0]["body"]
    assert "## Completed Tickets\n\n- [#2: Ticket 2](https://github.com/example/project/pull/42)" in body
    assert body.index("## Completed Tickets") < body.index("## What Problem This Solves")


def test_parent_closeout_recovers_without_a_second_merge(git_repo: Path) -> None:
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
    publisher.data["delivery"]["crash_after_parent_close_once"] = True

    with pytest.raises(OSError, match="Parent Issue close"):
        engine.approve(str(state["run_id"]))

    pending = states.load_run(str(state["run_id"]))
    assert pending is not None
    assert pending["run_publication"]["phase"] == "merged"
    assert pending["status"] == "parent_closeout_pending"
    assert publisher.live_pull_request(int(published["run_publication"]["pr_number"]))["state"] == "MERGED"
    assert not Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).record_execution_failure(str(state["run_id"]), "lost Parent closeout response")
    preserved = states.load_run(str(state["run_id"]))
    assert preserved is not None
    assert preserved["status"] == "parent_closeout_pending"

    completed = engine.approve(str(state["run_id"]))

    assert completed["status"] == "completed"
    assert publisher.data["delivery"]["closed_issues"].count(1) == 1
    assert publisher.data["parent"]["state"] == "CLOSED"


def test_final_run_closeout_records_its_actual_delivery_type(git_repo: Path) -> None:
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
    engine.approve(str(state["run_id"]))

    comment = next(
        mutation
        for mutation in publisher.data["delivery"]["mutations"]
        if mutation["action"] == "parent_completion_comment"
    )
    assert comment["action"] == "parent_completion_comment"
    assert comment["delivery_type"] == "Final Run"


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


def test_worker_failure_before_publication_artifact_is_not_retried(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    class InterruptedPublication(RunPublicationAgents):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def run_publication(self, request: dict[str, Any]) -> dict[str, str]:
            del request
            self.attempts += 1
            raise CodexProcessError("simulated publication worker crash")

    agents = InterruptedPublication()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    with pytest.raises(CodexProcessError, match="publication worker crash"):
        engine.publish(str(state["run_id"]))

    interrupted = states.load_run(str(state["run_id"]))
    assert interrupted is not None
    assert interrupted["run_publication"]["phase"] == "publishing"
    assert interrupted["run_publication"]["publication_attempts"] == 1
    assert agents.attempts == 1
    assert interrupted["run_acceptance"]["phase"] == "accepted"

    retried = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert retried["status"] == "run_approval_pending"
    assert retried["run_publication"]["publication_attempts"] == 2
    assert retried["run_acceptance"]["phase"] == "accepted"


def test_malformed_final_run_publication_is_not_retried(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    class MalformedRunPublication:
        def __init__(self) -> None:
            self.attempts = 0

        def run_publication(self, request: dict[str, Any]) -> dict[str, str]:
            del request
            self.attempts += 1
            return {"invalid": "publication"}

    agents = MalformedRunPublication()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    with pytest.raises(ValueError, match="commit_message"):
        engine.publish(str(state["run_id"]))

    assert agents.attempts == 1
    interrupted = states.load_run(str(state["run_id"]))
    assert interrupted is not None
    assert interrupted["run_publication"]["phase"] == "publishing"
    assert interrupted["run_publication"]["publication_attempts"] == 1


def test_malformed_final_run_publication_is_reported_as_execution_failed_by_cli(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "malformed-run-publication.json"
    agents.write_text(
        json.dumps({"run_publications": [{"invalid": "publication"}]}),
        encoding="utf-8",
    )

    failed = run_cli(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["status"] == "execution_failed"
    assert persisted["terminal_kind"] == "execution_failed"
    assert persisted["run_publication"]["phase"] == "publishing"
    assert persisted["run_publication"]["publication_attempts"] == 1


def test_resume_retries_only_exhausted_final_run_publication(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    pending = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=InterruptedRunPublisher(git_repo / "github.json", git),
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    acceptance = pending["run_acceptance"]["acceptance_record"]
    agents = git_repo / "resume-publication-agents.json"
    agents.write_text(
        json.dumps(
            {"run_publications": [RunPublicationAgents().run_publication({})]}
        ),
        encoding="utf-8",
    )

    resumed = run_cli(
        git_repo,
        git_repo / "github.json",
        "resume",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    recovered = states.load_run(str(state["run_id"]))
    assert recovered is not None
    assert recovered["run_acceptance"]["acceptance_record"] == acceptance
    assert recovered["run_publication"]["publication_attempts"] == 1


def test_recovered_publication_replaces_the_pending_terminal_kind(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    pending = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=InterruptedRunPublisher(git_repo / "github.json", git),
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    publisher.data["delivery"]["required_checks"] = ["pending"]

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert pending["terminal_kind"] == "publication_pending"
    assert waiting["status"] == "waiting_checks"
    assert waiting["terminal_kind"] == "waiting_checks"


def test_final_run_publication_receives_only_role_required_facts(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()

    RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    request = agents.requests[0]
    assert set(request) == {"acceptance_artifact", "checkout", "parent_issue_url"}
    assert request["parent_issue_url"].endswith("/issues/1")


def test_retries_publication_with_a_fresh_narrative_agent(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    publisher.data["delivery"]["crash_after_ensure_run_pr_once"] = True
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    retried = engine.publish(str(state["run_id"]))

    assert retried["status"] == "run_approval_pending"
    assert len(agents.requests) == 2


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
