from __future__ import annotations

import subprocess
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_current_run_state,
)

from run_acceptance_test_support import _passing_artifact
from test_cli import run_internal_stage, run_cli, stdout_json

from run_publication_test_support import (
    CountingNarrativeRefreshPublisher,
    DelayedChecksRunPublisher,
    HumanThenRunPublicationAgents,
    InterruptedFinalRefPublisher,
    RunPublicationAgents,
    UnknownNarrativeWritePublisher,
    WaitingThenInterruptedChecksPublisher,
    _accepted_run,
)

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
    assert pull["body"].startswith("Parent Issue: #1\nDelivery Type: Final Run\n\n")
    assert publisher.data["delivery"]["agent_run_status"] == [
        {
            "pr_number": final["pr_number"],
            "scope": "final-run",
            "base_sha": git.resolve("main"),
            "candidate_sha": git.resolve(str(state["run_branch"])),
            "validation_outcome": "pass",
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

def test_final_pr_read_lag_waits_without_recreating_publication(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = DelayedChecksRunPublisher(git_repo / "github.json", git)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert waiting["diagnostics"][0]["waiting_for"] == (
        "Run PR #1 Required Checks observation"
    )
    assert len(agents.requests) == 1

    resumed = engine.publish(str(state["run_id"]))

    assert resumed["status"] == "run_approval_pending"
    assert len(agents.requests) == 1

def test_final_required_checks_read_failure_is_supervised_without_rewriting_pr(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = CountingNarrativeRefreshPublisher(git_repo / "github.json", git)
    publisher.data["delivery"]["run_required_checks_read_failures"] = [
        {
            "code": "github_write_failed",
            "message": "temporary gh pr checks failure",
        }
    ]
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert "publication_operation_retry" not in waiting["run_publication"]
    assert waiting["supervision_window"]["kind"] == "github_convergence"
    assert waiting["diagnostics"][0]["waiting_for"].endswith(
        "Required Checks observation"
    )
    assert all(
        diagnostic["code"] != "github_write_outcome_unknown"
        for diagnostic in waiting["diagnostics"]
    )
    before_resume = json.loads((git_repo / "github.json").read_text(encoding="utf-8"))
    final_prs = [
        pull
        for pull in before_resume["delivery"]["pull_requests"]
        if pull.get("scope") == "final_run"
    ]
    assert len(final_prs) == 1
    assert publisher.refresh_attempts == 0
    original_title = final_prs[0]["title"]
    original_body = final_prs[0]["body"]

    resumed = engine.publish(str(state["run_id"]))

    assert resumed["status"] == "run_approval_pending"
    assert len(agents.requests) == 1
    assert publisher.refresh_attempts == 0
    after_resume = json.loads((git_repo / "github.json").read_text(encoding="utf-8"))
    resumed_prs = [
        pull
        for pull in after_resume["delivery"]["pull_requests"]
        if pull.get("scope") == "final_run"
    ]
    assert len(resumed_prs) == 1
    assert resumed_prs[0]["title"] == original_title
    assert resumed_prs[0]["body"] == original_body


def test_repeated_required_checks_snapshot_unavailability_stays_supervised(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "required checks unavailable"}
        for _ in range(6)
    ]
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))
    for _ in range(5):
        waiting = engine.publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert "publication_operation_retry" not in waiting["run_publication"]
    final_prs = [
        pull
        for pull in publisher.data["delivery"]["pull_requests"]
        if pull.get("scope") == "final_run"
    ]
    assert len(final_prs) == 1
    assert len(agents.requests) == 1

    resumed = engine.publish(str(state["run_id"]))

    assert resumed["status"] == "run_approval_pending"
    assert resumed["run_publication"]["phase"] == "ready_for_approval"
    assert "publication_operation_retry" not in resumed["run_publication"]
    assert len(agents.requests) == 1


def test_first_unknown_required_checks_observation_does_not_create_operation_retry(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["unknown"]
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["required_checks_observation_status"] == "unknown"
    assert "publication_operation_retry" not in waiting["run_publication"]

    waiting["run_publication"]["publication_operation_retry"] = {
        "attempts": 4,
        "limit": 5,
    }
    waiting["run_publication"]["last_publication_error"] = "legacy read aggregate"
    publication_attempts = waiting["run_publication"]["semantic_attempt_history"]
    publication_attempts[-1]["publication_operation_retry"] = dict(
        waiting["run_publication"]["publication_operation_retry"]
    )
    states.save_run(str(state["run_id"]), waiting)
    publisher.data["delivery"]["required_checks"] = ["none"]
    resumed = engine.publish(str(state["run_id"]))

    assert resumed["status"] == "run_approval_pending"
    assert "required_checks_observation_status" not in resumed["run_publication"]
    assert "publication_operation_retry" not in resumed["run_publication"]


def test_waiting_final_publication_counts_process_failures_to_hard_boundary(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = WaitingThenInterruptedChecksPublisher(git_repo / "github.json", git)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))
    exhausted = engine.publish(str(state["run_id"]))
    calls_at_exhaustion = publisher.required_check_calls
    unchanged = engine.publish(str(state["run_id"]))

    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert exhausted["status"] == "publication_pending"
    assert exhausted["run_publication"]["publication_operation_retry"] == {
        "attempts": 5,
        "limit": 5,
    }
    assert calls_at_exhaustion == 1
    assert publisher.interrupted_live_reads == 5
    assert publisher.required_check_calls == calls_at_exhaustion
    assert unchanged["status"] == "publication_pending"
    assert len(agents.requests) == 1


def test_final_ref_write_and_readback_stop_at_operation_retry_boundary(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = InterruptedFinalRefPublisher(git_repo / "github.json", git)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    result = engine.publish(str(state["run_id"]))
    assert result["run_publication"]["publication_operation_retry"] == {
        "attempts": 1,
        "limit": 5,
    }
    for expected_attempts in range(2, 6):
        result = engine.publish(str(state["run_id"]))
        assert result["run_publication"]["publication_operation_retry"] == {
            "attempts": expected_attempts,
            "limit": 5,
        }

    calls_at_exhaustion = publisher.ref_calls
    unchanged = engine.publish(str(state["run_id"]))
    assert result["status"] == "publication_pending"
    assert calls_at_exhaustion == 5
    assert publisher.ref_calls == calls_at_exhaustion
    assert unchanged["status"] == "publication_pending"
    assert len(agents.requests) == 1

def test_unknown_final_pr_narrative_write_does_not_replay(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    initial = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert initial["status"] == "run_approval_pending"
    # Model an interruption after the PR narrative write but before its
    # Publication Record became durable. Recovery must reconcile the write
    # intent without replaying it.
    initial["run_publication"].pop("record")
    initial["run_publication"]["phase"] = "waiting_external"
    initial["status"] = "waiting_external"
    initial["terminal_kind"] = "waiting_external"
    states.save_run(str(state["run_id"]), initial)
    uncertain_publisher = UnknownNarrativeWritePublisher(
        git_repo / "github.json", git
    )
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=uncertain_publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))
    resumed = engine.publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert resumed["status"] == "run_approval_pending"
    assert uncertain_publisher.refresh_attempts == 1

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
    assert blocked["run_publication"]["blocked_reason"] == "agent_requires_human"
    malformed = deepcopy(blocked)
    malformed["run_publication"].pop("blocked_reason")
    with pytest.raises(IncompatibleRunStateError, match="Human Blocker phase"):
        require_current_run_state(malformed)
    assert publisher.data["delivery"]["pull_requests"] == []
    for command in ("status", "history"):
        view = run_cli(
            git_repo,
            git_repo / "github.json",
            command,
            str(state["run_id"]),
        )
        assert view.returncode == 0, view.stderr
        assert "类型: Human Blocker" in view.stdout
        assert "对象: Run Publication" in view.stdout
        assert "阶段: pending" in view.stdout
        assert (
            "原因: GitHub denied access; tried gh issue view; grant Issue read access."
            in view.stdout
        )
        assert (
            "触发阻塞的 Agent: publication；model publication-model；"
            "reasoning effort high；本轮时长: 0 秒"
            in view.stdout
        )
        assert (
            "唯一下一步: agent-run resume 1 --repo example/project"
            in view.stdout
        )
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
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        message="Issue read access is now available.",
    )
    assert resumed["run_publication"]["prior_human_blockers"] == [
        "GitHub denied access; tried gh issue view; grant Issue read access."
    ]
    assert resumed["run_publication"]["human_response_history"] == [
        {
            "generation": 1,
            "human_blockers": [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ],
            "response": "Issue read access is now available.",
        }
    ]
    assert "blocked_reason" not in resumed["run_publication"]

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
    assert (
        "## Completed Tickets\n\n- [#2: Ticket 2](https://github.com/example/project/pull/42)"
        in body
    )
    assert body.index("## Completed Tickets") < body.index(
        "## What Problem This Solves"
    )

def test_fresh_publication_refreshes_an_existing_final_run_pr(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    first = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    pr_number = int(first["run_publication"]["pr_number"])

    (git_repo / "new-default.txt").write_text("advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "new-default.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default for final narrative"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    data = json.loads((git_repo / "github.json").read_text(encoding="utf-8"))
    data["default_head_sha"] = git.resolve("main")
    (git_repo / "github.json").write_text(json.dumps(data), encoding="utf-8")
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        str(state["run_id"])
    )

    class FreshRunReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("fresh-run-reviewer", _passing_artifact())

    fresh = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=FreshRunReviewer(),
        github=publisher,
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(git_repo / "github.json"),
    ).accept(str(state["run_id"]))
    assert fresh["run_acceptance"]["phase"] == "accepted"

    class RefreshedNarrative(RunPublicationAgents):
        def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
            artifact = super().run_publication(request)
            artifact["pr_title"] = "feat(run): refreshed final delivery"
            artifact["pr_body_markdown"] = (
                "## What Problem This Solves\n\nThe default base advanced.\n\n"
                "## Why This Change Was Made\n\nThe final evidence was refreshed.\n\n"
                "## User Impact\n\nMaintainers see the current review boundary.\n\n"
                "## Evidence\n\nFresh default-base acceptance passed."
            )
            return artifact

    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RefreshedNarrative(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(git_repo / "github.json"),
    ).publish(str(state["run_id"]))

    assert published["run_publication"]["pr_number"] == pr_number
    pull = publisher.data["delivery"]["pull_requests"][0]
    assert pull["title"] == "feat(run): refreshed final delivery"
    assert "Fresh default-base acceptance passed." in pull["body"]

def test_publication_does_not_create_a_final_pr_after_parent_drifts(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    fixture = git_repo / "github.json"

    class ParentDriftingLookupPublisher(FixtureGitHubPublisher):
        def find_run_pr(self, **authority: str) -> int | None:
            result = super().find_run_pr(**authority)
            self.data["parent"]["body"] = "Changed after publication agent completed."
            self._save()
            return result

    drifting = ParentDriftingLookupPublisher(fixture, git)
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=drifting,
        default_branch="main",
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).publish(str(state["run_id"]))

    assert published["status"] == "run_acceptance_pending"
    assert published["run_acceptance"]["phase"] == "pending"
    assert drifting.data["delivery"]["pull_requests"] == []

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
    assert (
        publisher.live_pull_request(int(published["run_publication"]["pr_number"]))[
            "state"
        ]
        == "MERGED"
    )
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
    assert (
        publisher.live_pull_request(int(published["run_publication"]["pr_number"]))[
            "state"
        ]
        == "OPEN"
    )

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
    assert (
        publisher.live_pull_request(int(published["run_publication"]["pr_number"]))[
            "state"
        ]
        == "CLOSED"
    )
    resumed = run_internal_stage(
        git_repo, git_repo / "github.json", "accept-run", str(state["run_id"])
    )
    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "abandoned"
    removed_command = run_cli(
        git_repo,
        git_repo / "github.json",
        "confirm-structure",
        str(state["run_id"]),
    )
    assert removed_command.returncode == 2
    assert "invalid choice" in removed_command.stderr
    assert not Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).record_execution_failure(str(state["run_id"]), "must not overwrite abandonment")
    assert states.load_run(str(state["run_id"]))["status"] == "abandoned"

def test_abandon_recovers_lost_final_run_pr_close_response(
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
    publisher.data["delivery"]["crash_after_abandon_run_pr_once"] = True

    with pytest.raises(OSError, match="simulated lost response after abandon_run_pr"):
        engine.abandon(str(state["run_id"]))

    interrupted = states.load_run(str(state["run_id"]))
    assert interrupted["status"] == "abandonment_pending"
    assert interrupted["run_abandonment"]["final_pr"] == {
        "pr_number": pr_number,
        "status": "pending",
    }
    assert publisher.live_pull_request(pr_number)["state"] == "CLOSED"
    close_mutations = [
        mutation
        for mutation in publisher.data["delivery"]["mutations"]
        if mutation["action"] == "close_final_run_pr"
    ]
    assert close_mutations == [{"action": "close_final_run_pr", "pr_number": pr_number}]

    recovered = engine.abandon(str(state["run_id"]))

    assert recovered["status"] == "abandoned"
    assert recovered["run_abandonment"]["final_pr"]["status"] == "completed"
    assert [
        mutation
        for mutation in publisher.data["delivery"]["mutations"]
        if mutation["action"] == "close_final_run_pr"
    ] == close_mutations
