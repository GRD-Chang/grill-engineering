from __future__ import annotations

import subprocess
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.controller import Controller
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import (
    MAX_CANDIDATE_ACCEPTANCE_HISTORY,
    invalidate_stale_run_repair,
    ticket_completion_records,
)
from agent_run.run_repair_currentness import RunRepairObservationPending
from agent_run.state import StateStore
from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_candidate_acceptance_history,
    require_current_run_state,
)

from conftest import write_fixture
from test_cli import run_internal_stage, run_cli, stdout_json


_BLOCKED_EVIDENCE = (
    "发生：GitHub 拒绝访问 Parent Issue；尝试：执行 gh issue view；人必须：授予 Issue 读取权限。"
)
_PASS_EVIDENCE = {
    "e2e": "操作或命令：执行完整 Run 验收流程；退出码：0；结果：完整 Run 通过。",
    "standards": "审查范围或基线：仓库编码规范与完整 Run diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：Parent Issue 的全部验收标准；覆盖结论：完整 Run 已覆盖。",
}


def _passing_artifact() -> dict[str, object]:
    return {
        "checks": {
            name: {"status": "pass", "evidence": _PASS_EVIDENCE[name], "findings": []}
            for name in ("e2e", "standards", "spec")
        },
    }


def _repair_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The accumulated flow loses the first Ticket behavior.",
        "findings": [
            "问题：集成流程不完整；证据：两个 Ticket 组合后端到端场景失败；必须修复：恢复完整组合流程；复验：运行完整累计场景。"
        ],
    }
    return artifact


def _candidate_finding_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["spec"] = {
        "status": "fail",
        "evidence": "The candidate still misses the refreshed Parent contract.",
        "findings": [
            "问题：Candidate 未覆盖新的 Parent 约束；证据：Spec lane 仍失败；必须修复：补齐约束并重新验证；复验：重新运行完整 Candidate Acceptance。"
        ],
    }
    return artifact


def _human_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": _BLOCKED_EVIDENCE,
        "findings": [],
    }
    return artifact


def _failed_invocation(
    *, role: str, phase: str, work_subject: str, generation: int,
    requested_thread_id: str | None, reported_thread_id: str | None,
    currentness_boundary: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": currentness_boundary or {},
        "status": "failed",
        "requested_thread_id": requested_thread_id,
        "reported_thread_id": reported_thread_id,
        "attempt_count": 1,
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:00:01+00:00",
        "error": "fixture failure",
        "return_code": 1,
        "signal": None,
    }


class ScriptedRunAgents:
    def __init__(self) -> None:
        self.development_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        # The initial Run rejection is followed by exactly one Candidate Run
        # Acceptance; promotion must not invoke a third whole-Run reviewer.
        self._reviews = [
            _repair_artifact(),
            _passing_artifact(),
        ]

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        (checkout / "run-repair.txt").write_text("repaired\n", encoding="utf-8")
        return DevelopmentResult(
            thread_id="run-repair-developer",
            summary="Repaired the accumulated behavior.",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        checkout = Path(str(request["checkout"]))
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if request.get("candidate_acceptance") is True:
            assert (checkout / "run-repair.txt").read_text(encoding="utf-8") == "repaired\n"
            assert not {
                "run_id",
                "parent",
                "ticket_graph",
                "ticket_completion_records",
                "base_sha",
                "run_head_sha",
                "candidate_sha",
                "repair_base_run_head_sha",
                "expected_merge_result",
                "acceptance_artifact",
                "ci_evidence",
                "human_feedback",
                "merge_conflict_evidence",
            }.intersection(request)
        return ReviewResult(
            thread_id=f"run-reviewer-{len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )

    def publication(self, request: dict[str, Any]) -> dict[str, str]:
        return {
            "commit_message": "fix(run): repair accumulated delivery behavior",
            "pr_title": "fix(run): repair accumulated delivery behavior",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe combined Run failed.\n\n"
                "## Why This Change Was Made\n\nThe repair restores the combined behavior.\n\n"
                "## User Impact\n\nThe complete delivery works together.\n\n"
                "## Evidence\n\nThe fresh Run validation will recheck it."
            ),
        }


def _completed_run(git_repo: Path) -> tuple[dict[str, Any], StateStore, GitRepository]:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
                "state": "CLOSED",
                "labels": [],
                "blocked_by": [],
            }
        },
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    state["active_ticket_job"] = None
    state["frontier"] = []
    state["status"] = "run_acceptance_pending"
    state["terminal_kind"] = "all_tickets_completed"
    state["ticket_jobs"] = {
        "2": {
            "ticket_number": 2,
            "phase": "completed",
            "development_thread_id": "ticket-developer",
            "development_thread_history": [],
            "reviewer_thread_ids": ["ticket-reviewer"],
            "modification_attempts": 1,
            "effective_revision": state["ticket_graph"]["tickets"]["2"][
                "content_revision"
            ],
            "acceptance_record": {"artifact": _passing_artifact()},
            "integrated_sha": git.resolve(str(state["run_branch"])),
        }
    }
    states.save_run(str(state["run_id"]), state)
    return state, states, git


@pytest.mark.parametrize(
    ("display_outcome", "expected_display_status"),
    [
        ("linked", "linked"),
        ("api_error", "unavailable"),
        ("empty", "unavailable"),
        ("missing_readback", "unavailable"),
    ],
)
def test_run_acceptance_repairs_then_rechecks_the_whole_run(
    git_repo: Path, display_outcome: str, expected_display_status: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    repair_branch = f"agent-run-repair/{state['run_id']}/1"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).setdefault("published_branches", {})[
        repair_branch
    ] = git.resolve(str(state["run_branch"]))
    data["delivery"]["linked_branch_display_outcomes"] = display_outcome
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(
        str(state["run_id"])
    )

    assert result["status"] == "run_publication_pending"
    run = result["run_acceptance"]
    assert run["phase"] == "accepted"
    assert run["modification_attempts"] == 1
    assert run["reviewer_thread_ids"] == [
        "run-reviewer-1",
        "run-reviewer-2",
    ]

    assert run["reviewed_head_sha"] == git.resolve(str(state["run_branch"]))
    assert run["acceptance_record"]["acceptance_state"] == "integrated"
    assert run["acceptance_record"]["reviewed_base_sha"] == git.resolve("main")
    assert run["candidate_acceptance_history"]
    assert all(
        "acceptance_record" not in item and "artifact" not in item
        for item in run["candidate_acceptance_history"]
    )
    assert run["repair_cycle"]["status"] == "promoted"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert run["repair_cycle"]["validation_attempts"] == 1
    assert result["ticket_jobs"]["2"]["modification_attempts"] == 1
    assert len(agents.development_requests) == 1
    assert agents.development_requests[0]["repair_source"] == "acceptance"
    assert agents.development_requests[0]["acceptance_artifact"] == _repair_artifact()
    assert len(agents.review_requests) == 2
    candidate_request = agents.review_requests[1]
    assert candidate_request["candidate_acceptance"] is True
    assert not {
        "run_id",
        "parent",
        "ticket_graph",
        "ticket_completion_records",
        "base_sha",
        "run_head_sha",
        "candidate_sha",
        "repair_base_run_head_sha",
        "expected_merge_result",
        "acceptance_artifact",
        "ci_evidence",
        "human_feedback",
        "merge_conflict_evidence",
    }.intersection(candidate_request)
    validation_checkouts = [Path(str(request["checkout"])) for request in agents.review_requests]
    assert len({str(checkout) for checkout in validation_checkouts}) == 2
    assert all(not checkout.exists() for checkout in validation_checkouts)
    fixture_data = json.loads((git_repo / "github.json").read_text(encoding="utf-8"))
    repair_prs = fixture_data["delivery"]["pull_requests"]
    assert len(repair_prs) == 1
    assert repair_prs[0]["scope"] == "run_repair"
    assert repair_prs[0]["base_branch"] == state["run_branch"]
    assert repair_prs[0]["body"].startswith(
        "Parent Issue: #1\nDelivery Type: Run Repair\n\n"
    )
    assert fixture_data["delivery"]["acceptance_records"] == []
    repair_statuses = fixture_data["delivery"]["agent_run_status"]
    assert repair_statuses[0]["scope"] == "run-repair-1"
    assert repair_statuses[0]["candidate_sha"] == run["candidate_sha"]
    assert fixture_data["delivery"]["closed_issues"] == []
    assert not (
        states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    ).exists()
    repair_branch = run["completed_repair_jobs"][0]["repair_branch"]
    assert run["completed_repair_jobs"][0]["linked_branch_display"] == {
        "display_attempted": True,
        "status": expected_display_status,
    }
    assert fixture_data["delivery"]["linked_branches"] == (
        {"1": repair_branch} if expected_display_status == "linked" else {}
    )
    assert repair_branch not in fixture_data["delivery"]["published_branches"]
    with pytest.raises(GitError):
        git.resolve(repair_branch)


def test_run_repair_budget_exhaustion_ends_the_repair_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    run_branch = str(state["run_branch"])
    base_sha = git.resolve(run_branch)
    artifact = _repair_artifact()
    run = {
        "phase": "repairing",
        "acceptance_generation": 1,
        "modification_attempts": 10,
        "validation_attempts": 10,
        "development_thread_id": "run-repair-developer",
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "acceptance_artifact": artifact,
        "repair_cycle": {
            "generation": 1,
            "status": "active",
            "code_modification_attempts": 10,
            "validation_attempts": 10,
        },
    }
    run["repair_job"] = {
        "run_id": state["run_id"],
        "phase": "escalating",
        "repair_attempt": 1,
        "repair_generation": 1,
        "repair_branch": f"agent-run-repair/{state['run_id']}/1",
        "base_sha": base_sha,
        "default_base_sha": git.resolve("main"),
        "repair_base_run_head_sha": base_sha,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance",
        "repair_input_artifact": artifact,
        "acceptance_artifact": artifact,
        "modification_attempts": 10,
        "code_modification_attempts": 10,
        "validation_attempts": 10,
        "acceptance_generation": 1,
        "candidate_acceptance_history": [],
        "repair_checkout": str(
            states.root / "worktrees" / str(state["run_id"]) / "run-repair"
        ),
        "escalation_code": "modification_budget_exhausted",
    }
    state["run_acceptance"] = run
    state["status"] = "run_acceptance_pending"
    states.save_run(str(state["run_id"]), state)
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(json.dumps({}), encoding="utf-8")

    result = run_cli(
        git_repo,
        git_repo / "github.json",
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )

    assert result.returncode == 2, result.stderr
    assert stdout_json(result)["status"] == "ready_for_human"
    status_result = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"]), "--json"
    )
    history_result = run_cli(
        git_repo, git_repo / "github.json", "history", str(state["run_id"]), "--json"
    )
    assert status_result.returncode == history_result.returncode == 0
    status = stdout_json(status_result)
    assert status["run_repair"]["cycle_status"] == "budget_exhausted"
    assert status["run_repair"]["code_modification_attempts"] == 10
    assert stdout_json(history_result)["run_id"] == state["run_id"]
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    persisted_run = persisted["run_acceptance"]
    assert persisted_run["repair_job"]["modification_attempts"] == 10
    assert persisted_run["repair_job"]["validation_attempts"] == 10
    assert "candidate_sha" not in persisted_run["repair_job"]
    assert persisted_run["repair_job"]["phase"] == "blocked"
    assert persisted_run["repair_job"]["blocked_reason"] == (
        "modification_budget_exhausted"
    )
    assert persisted_run["repair_cycle"]["status"] == "budget_exhausted"
    assert persisted_run["repair_cycle"]["ended_reason"] == (
        "modification_budget_exhausted"
    )
    assert not (
        states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    ).exists()


def test_candidate_acceptance_history_keeps_a_bounded_recovery_window(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    candidate_sha = git.resolve(str(state["run_branch"]))
    base_sha = candidate_sha
    job = {
        "default_base_sha": git.resolve("main"),
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance",
        "candidate_acceptance_history": [],
    }

    for attempt in range(MAX_CANDIDATE_ACCEPTANCE_HISTORY + 8):
        engine.candidate_acceptance.record(
            job,
            f"reviewer-{attempt}",
            _passing_artifact(),
        )

    history = job["candidate_acceptance_history"]
    assert len(history) == MAX_CANDIDATE_ACCEPTANCE_HISTORY
    assert history[0]["reviewer_thread_id"] == "reviewer-8"
    assert history[-1]["reviewer_thread_id"] == "reviewer-39"
    assert all("acceptance_record" not in item for item in history)
    assert all("artifact" not in item for item in history)
    assert history[-1]["ticket_completion_records_fingerprint"] == canonical_fingerprint(
        job["ticket_completion_records"]
    )
    assert require_candidate_acceptance_history(
        history, "run_acceptance.candidate_acceptance"
    ) == history
    with pytest.raises(IncompatibleRunStateError):
        require_candidate_acceptance_history(
            [*history, history[-1]], "run_acceptance.candidate_acceptance"
        )


@pytest.mark.parametrize(
    ("artifact", "expected_outcome"),
    [
        (_passing_artifact(), "accepted"),
        (_candidate_finding_artifact(), "finding"),
        (_human_artifact(), "blocked"),
    ],
)
def test_candidate_acceptance_history_preserves_each_artifact_outcome(
    git_repo: Path,
    artifact: dict[str, object],
    expected_outcome: str,
) -> None:
    state, _states, git = _completed_run(git_repo)
    engine = RunAcceptanceEngine(
        git=git,
        states=StateStore(git_repo / ".agent-run"),
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    candidate_sha = git.resolve(str(state["run_branch"]))
    job = {
        "default_base_sha": git.resolve("main"),
        "candidate_sha": candidate_sha,
        "base_sha": candidate_sha,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance",
        "candidate_acceptance_history": [],
    }

    engine.candidate_acceptance.record(job, "candidate-reviewer", artifact)

    assert job["candidate_acceptance_history"][-1]["outcome"] == expected_outcome
    state["run_acceptance"] = {
        "phase": "repairing",
        "candidate_acceptance_history": [],
        "repair_job": job,
    }
    require_current_run_state(state)


def _malformed_candidate_history() -> list[dict[str, object]]:
    return [{"candidate_sha": "candidate", "artifact": {"result_kind": "acceptance"}}]


def test_candidate_history_state_load_rejects_nested_acceptance_record_before_mutation(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "pending",
        "candidate_acceptance_history": _malformed_candidate_history(),
    }
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError):
        states.load_current_run(str(state["run_id"]))


def test_candidate_history_stale_recovery_rejects_unknown_fields_before_mutation(
    git_repo: Path,
) -> None:
    state, _states, _git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_cycle": {"status": "active"},
        "candidate_acceptance_history": [],
        "repair_job": {
            "candidate_acceptance_history": _malformed_candidate_history(),
        },
    }
    before = deepcopy(state)

    with pytest.raises(IncompatibleRunStateError):
        invalidate_stale_run_repair(state)

    assert state == before


def test_run_repair_promotion_mismatch_discards_candidate_for_fresh_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class MismatchingPublisher(FixtureGitHubPublisher):
        def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
            super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
            subprocess.run(
                ["git", "update-ref", f"refs/heads/{run_branch}", git.resolve("main")],
                cwd=git.root,
                check=True,
            )

    agents = ScriptedRunAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=MismatchingPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    run = result["run_acceptance"]
    assert result["status"] == "run_acceptance_pending"
    assert result["terminal_kind"] == "run_acceptance_stale"
    assert run["phase"] == "pending"
    assert "repair_job" not in run
    assert "acceptance_record" not in run
    assert all(
        "acceptance_record" not in item and "artifact" not in item
        for item in run.get("candidate_acceptance_history", [])
    )
    assert run["discarded_repair_thread_ids"][-1] == "run-reviewer-2"
    assert not (states.root / "worktrees" / str(state["run_id"]) / "run-repair").exists()


def test_run_repair_unknown_required_checks_waits_without_new_candidate(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["unknown"]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    run = result["run_acceptance"]
    job = run["repair_job"]
    assert result["status"] == "waiting_external"
    assert result["terminal_kind"] == "waiting_external"
    assert job["phase"] == "waiting_checks"
    assert job["modification_attempts"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert run.get("completed_repair_jobs", []) == []
    assert result["supervision_window"]["kind"] == "github_convergence"


def test_run_repair_default_branch_drift_revalidates_candidate_in_same_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    initial_default = git.resolve("main")

    class DriftingCandidateReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._drifted = False
            self.fail_next_candidate_review = False

        def review(self, request: dict[str, Any]) -> ReviewResult:
            if (
                request.get("candidate_acceptance") is True
                and self.fail_next_candidate_review
            ):
                self.fail_next_candidate_review = False
                self.review_requests.append(request)
                raise ValueError("new-base Candidate Reviewer failed")
            result = super().review(request)
            if request.get("candidate_acceptance") is True and not self._drifted:
                self._drifted = True
                (git_repo / "default-drift-during-repair.txt").write_text(
                    "advanced\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "default-drift-during-repair.txt"],
                    cwd=git_repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "advance default during repair"],
                    cwd=git_repo,
                    check=True,
                    capture_output=True,
                )
                data = json.loads(fixture.read_text(encoding="utf-8"))
                data["default_head_sha"] = git.resolve("main")
                fixture.write_text(json.dumps(data), encoding="utf-8")
            return result

    first_agents = DriftingCandidateReviewer()
    first = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first_agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=initial_default,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    run = first["run_acceptance"]
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    job = run["repair_job"]
    generation = run["repair_generation"]
    thread_id = job["development_thread_id"]
    assert first["status"] == "run_acceptance_pending"
    assert run["phase"] == "repairing"
    assert job["phase"] == "candidate"
    assert job["default_base_sha"] == git.resolve("main")
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert repair_checkout.exists()
    assert "pending_review_result" not in job

    first_agents._reviews = [_passing_artifact()]
    first_agents.fail_next_candidate_review = True
    with pytest.raises(ValueError, match="new-base Candidate Reviewer failed"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=first_agents,
            github=FixtureGitHubPublisher(fixture, git),
            default_head_sha=git.resolve("main"),
            currentness_reader=FixtureGitHubReader(fixture),
        ).accept(str(state["run_id"]))

    interrupted = states.load_current_run(str(state["run_id"]))
    assert interrupted is not None
    interrupted_job = interrupted["run_acceptance"]["repair_job"]
    assert interrupted_job["phase"] == "reviewing"
    assert "pending_review_result" not in interrupted_job

    fresh = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first_agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert fresh["status"] == "run_publication_pending"
    assert fresh["run_acceptance"]["phase"] == "accepted"
    assert fresh["run_acceptance"]["repair_generation"] == generation
    assert fresh["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1
    assert fresh["run_acceptance"]["development_thread_history"] == [thread_id]
    assert len(first_agents.review_requests) == 4
    assert first_agents.review_requests[-1]["candidate_acceptance"] is True
    assert not repair_checkout.exists()


def test_run_repair_default_drift_during_development_preserves_next_attempt(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    initial_default = git.resolve("main")

    class DriftingSecondDevelopment(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [
                _repair_artifact(),
                _candidate_finding_artifact(),
                _passing_artifact(),
            ]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            attempt = len(self.development_requests)
            checkout = Path(str(request["checkout"]))
            (checkout / "run-repair.txt").write_text(
                f"repair-{attempt}\n", encoding="utf-8"
            )
            if attempt == 2:
                (git_repo / "default-drift-during-development.txt").write_text(
                    "advanced\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "default-drift-during-development.txt"],
                    cwd=git_repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "advance default during development"],
                    cwd=git_repo,
                    check=True,
                    capture_output=True,
                )
                data = json.loads(fixture.read_text(encoding="utf-8"))
                data["default_head_sha"] = git.resolve("main")
                fixture.write_text(json.dumps(data), encoding="utf-8")
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary=f"Repaired attempt {attempt}.",
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            checkout = Path(str(request["checkout"]))
            assert subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            return ReviewResult(
                thread_id=f"run-reviewer-{len(self.review_requests)}",
                artifact=self._reviews.pop(0),
            )

    agents = DriftingSecondDevelopment()
    first = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=initial_default,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    run = first["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    generation = run["repair_generation"]
    assert first["status"] == "run_acceptance_pending"
    assert job["phase"] == "committing_candidate"
    assert job["pending_attempt"] == 2
    assert job["modification_attempts"] == 1
    assert job["development_thread_id"] == "run-repair-developer"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert checkout.exists()

    completed = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert completed["status"] == "run_publication_pending"
    completed_run = completed["run_acceptance"]
    assert completed_run["repair_generation"] == generation
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 2
    assert completed_run["development_thread_history"] == ["run-repair-developer"]
    assert not checkout.exists()


@pytest.mark.parametrize("candidate_requires_more_repair", [False, True])
def test_run_repair_default_drift_after_merge_revalidates_same_cycle(
    git_repo: Path, candidate_requires_more_repair: bool
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    initial_default = git.resolve("main")

    class DefaultDriftingAfterMergePublisher(FixtureGitHubPublisher):
        def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
            super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
            (git_repo / "default-drift-after-merge.txt").write_text(
                "advanced\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "default-drift-after-merge.txt"],
                cwd=git_repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "advance default after repair merge"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["default_head_sha"] = git.resolve("main")
            fixture.write_text(json.dumps(data), encoding="utf-8")

    class ChangingNarrativeAgents(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self.publication_requests: list[dict[str, Any]] = []

        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            self.publication_requests.append(request)
            if len(self.publication_requests) == 1:
                return super().publication(request)
            return {
                "commit_message": "fix(run): describe the revalidated repair",
                "pr_title": "fix(run): describe the revalidated repair",
                "pr_body_markdown": (
                    "## What Problem This Solves\n\nThe combined Run failed.\n\n"
                    "## Why This Change Was Made\n\nThe revalidated repair restores it.\n\n"
                    "## User Impact\n\nThe complete delivery works together.\n\n"
                    "## Evidence\n\nThe latest default combination was revalidated."
                ),
            }

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if not self.development_requests:
                return super().develop(request)
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            (checkout / "follow-up-repair.txt").write_text(
                "repaired after revalidation\n", encoding="utf-8"
            )
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary="Repaired the latest default combination.",
            )

    first_agents = ChangingNarrativeAgents()
    first = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first_agents,
        github=DefaultDriftingAfterMergePublisher(fixture, git),
        default_head_sha=initial_default,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    run = first["run_acceptance"]
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    assert first["status"] == "run_acceptance_pending", first
    job = run["repair_job"]
    generation = run["repair_generation"]
    thread_id = job["development_thread_id"]
    assert run["phase"] == "repairing"
    assert job["phase"] == "candidate"
    assert job["integrated_sha"] == git.resolve(str(state["run_branch"]))
    merged_publication_sha = job["publication_sha"]
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert repair_checkout.exists()

    first_agents._reviews = (
        [_repair_artifact(), _passing_artifact()]
        if candidate_requires_more_repair
        else [_passing_artifact()]
    )
    second = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first_agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert second["status"] == "run_publication_pending"
    assert second["run_acceptance"]["phase"] == "accepted"
    assert second["run_acceptance"]["repair_generation"] == generation
    assert second["run_acceptance"]["repair_cycle"]["status"] == "promoted"
    expected_modifications = 2 if candidate_requires_more_repair else 1
    assert (
        second["run_acceptance"]["repair_cycle"]["code_modification_attempts"]
        == expected_modifications
    )
    assert second["run_acceptance"]["development_thread_history"] == [thread_id]
    if candidate_requires_more_repair:
        assert second["run_acceptance"]["publication_sha"] != merged_publication_sha
    else:
        assert second["run_acceptance"]["publication_sha"] == merged_publication_sha
    expected_additional_attempts = 1 if candidate_requires_more_repair else 0
    assert len(first_agents.review_requests) == 3 + expected_additional_attempts
    assert len(first_agents.publication_requests) == 1 + expected_additional_attempts
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    repair_prs = [
        pull_request
        for pull_request in fixture_data["delivery"]["pull_requests"]
        if pull_request.get("scope") == "run_repair"
    ]
    assert len(repair_prs) == 1 + expected_additional_attempts
    assert all(pull_request["state"] == "MERGED" for pull_request in repair_prs)
    repair_branches = {
        str(pull_request["branch"]) for pull_request in repair_prs
    }
    assert len(repair_branches) == 1 + expected_additional_attempts
    completed_repairs = second["run_acceptance"]["completed_repair_jobs"]
    assert len(completed_repairs) == 1 + expected_additional_attempts
    assert {
        str(completed_repair["repair_branch"])
        for completed_repair in completed_repairs
    } == repair_branches
    assert repair_branches.isdisjoint(fixture_data["delivery"]["published_branches"])
    assert not repair_checkout.exists()


def test_run_repair_required_check_default_drift_revalidates_same_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {
        "phase": "ready_for_approval",
        "pr_number": final_pr,
        "artifact": {"pr_body_markdown": "Original final Run narrative."},
        "write_intent": {"action": "refresh_run_pr_narrative"},
        "approval_grant": {"fingerprint": "stale-default-boundary"},
    }
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(final_pr),
        },
    }
    states.save_run(str(state["run_id"]), state)
    initial_default = git.resolve("main")

    class DefaultDriftingAfterMergePublisher(FixtureGitHubPublisher):
        def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
            super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
            (git_repo / "default-drift-after-required-check-repair.txt").write_text(
                "advanced\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "default-drift-after-required-check-repair.txt"],
                cwd=git_repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "advance default after required-check repair"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["default_head_sha"] = git.resolve("main")
            fixture.write_text(json.dumps(data), encoding="utf-8")

    repair_agents = ScriptedRunAgents()
    repair_agents._reviews = [_passing_artifact()]
    first = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=repair_agents,
        github=DefaultDriftingAfterMergePublisher(fixture, git),
        default_head_sha=initial_default,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    run = first["run_acceptance"]
    assert first["status"] == "run_acceptance_pending"
    assert first["run_publication"]["phase"] == "stale"
    assert {"artifact", "write_intent", "approval_grant"}.isdisjoint(
        first["run_publication"]
    )
    job = run["repair_job"]
    generation = run["repair_generation"]
    thread_id = job["development_thread_id"]
    assert run["phase"] == "repairing"
    assert job["phase"] == "candidate"
    assert run["repair_cycle"]["status"] == "active"

    repair_agents._reviews = [_passing_artifact()]

    second = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=repair_agents,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert second["status"] == "run_publication_pending"
    assert second["run_acceptance"]["phase"] == "accepted"
    assert second["run_acceptance"]["repair_generation"] == generation
    assert second["run_acceptance"]["repair_cycle"]["status"] == "promoted"
    assert second["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1
    assert second["run_acceptance"]["development_thread_history"] == [thread_id]
    assert second["run_publication"]["pr_number"] == final_pr
    assert len(second["run_acceptance"]["completed_repair_jobs"]) == 1


def test_run_repair_promotion_rejects_final_pr_trigger_drift(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": final_pr}
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(final_pr),
        },
    }
    states.save_run(str(state["run_id"]), state)

    class TriggerDriftingPublisher(FixtureGitHubPublisher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._drifted = False

        def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
            super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
            self._drifted = True

        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            live = super().live_pull_request(pr_number)
            if self._drifted and pr_number == final_pr:
                live["head_sha"] = "foreign-final-pr-head"
            return live

    repair_agents = ScriptedRunAgents()
    repair_agents._reviews = [_passing_artifact()]
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=repair_agents,
        github=TriggerDriftingPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    run = result["run_acceptance"]
    assert result["status"] == "run_acceptance_pending"
    assert result["terminal_kind"] == "run_acceptance_stale"
    assert run["phase"] == "pending"
    assert run["repair_cycle"]["status"] == "discarded"
    assert "repair_job" not in run


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_repair_trigger_creation_supervises_recoverable_pr_reads(
    git_repo: Path, error_type: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": final_pr}
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(final_pr),
        },
    }
    states.save_run(str(state["run_id"]), state)

    class FailingTriggerPublisher(FixtureGitHubPublisher):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            if error_type == "github":
                raise GitHubReadError("github_read_failed", "trigger unavailable")
            if error_type == "os":
                raise OSError("trigger unavailable")
            raise TimeoutError("trigger unavailable")

    waiting = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FailingTriggerPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    run = waiting["run_acceptance"]
    job = run["repair_job"]
    assert waiting["status"] == "waiting_external"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["phase"] == "developing"
    assert job["development_thread_id"] is None
    assert job["repair_trigger_pending"] is True

    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    recovered = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    recovered_run = recovered["run_acceptance"]
    assert recovered["status"] == "run_publication_pending"
    assert recovered_run["repair_generation"] == 1
    assert recovered_run["repair_cycle"]["status"] == "promoted"
    assert recovered_run["repair_cycle"]["code_modification_attempts"] == 1


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_repair_currentness_supervises_recoverable_live_pr_reads(
    git_repo: Path, error_type: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": final_pr}
    currentness = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=publisher,
    ).repair_currentness
    assert currentness is not None
    trigger = currentness.create_trigger(
        state, "required_checks", {"ci_evidence": publisher.required_check_evidence(final_pr)}
    )
    assert trigger is not None
    job = {"repair_trigger": trigger}

    class FailingCurrentnessPublisher(FixtureGitHubPublisher):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            if error_type == "github":
                raise GitHubReadError("github_read_failed", "currentness unavailable")
            if error_type == "os":
                raise OSError("currentness unavailable")
            raise TimeoutError("currentness unavailable")

    failing = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FailingCurrentnessPublisher(fixture, git),
    ).repair_currentness
    assert failing is not None
    with pytest.raises(RunRepairObservationPending):
        failing.trigger_is_current(state, job, default_head_sha=git.resolve("main"))

    persisted = states.load_current_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["status"] == "waiting_external"
    assert persisted["supervision_window"]["kind"] == "github_convergence"
    assert persisted["diagnostics"][0]["code"] == (
        "github_pull_request_observation_pending"
    )


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_repair_resumes_returned_candidate_review_after_currentness_read_failure(
    git_repo: Path, error_type: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": final_pr}
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(final_pr),
        },
    }
    states.save_run(str(state["run_id"]), state)

    class FailingAfterCandidateReviewPublisher(FixtureGitHubPublisher):
        fail_currentness = False

        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            if self.fail_currentness and pr_number == final_pr:
                if error_type == "github":
                    raise GitHubReadError("github_read_failed", "currentness unavailable")
                if error_type == "os":
                    raise OSError("currentness unavailable")
                raise TimeoutError("currentness unavailable")
            return super().live_pull_request(pr_number)

    failing_publisher = FailingAfterCandidateReviewPublisher(fixture, git)

    class ReviewThenFailCurrentnessAgents(ScriptedRunAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            result = super().review(request)
            if request.get("candidate_acceptance") is True:
                failing_publisher.fail_currentness = True
            return result

    agents = ReviewThenFailCurrentnessAgents()
    agents._reviews = [_passing_artifact()]
    waiting = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=failing_publisher,
    ).accept(str(state["run_id"]))

    run = waiting["run_acceptance"]
    job = run["repair_job"]
    assert waiting["status"] == "waiting_external"
    assert job["phase"] == "reviewing"
    assert job["pending_review_result"]["reviewer_thread_id"] == "run-reviewer-1"
    assert len(agents.review_requests) == 1
    assert job["validation_attempts"] == 1
    candidate_sha = job["candidate_sha"]

    recovered = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    recovered_run = recovered["run_acceptance"]
    assert recovered["status"] == "run_publication_pending"
    assert recovered_run["repair_cycle"]["status"] == "promoted"
    assert recovered_run["repair_cycle"]["code_modification_attempts"] == 1
    assert recovered_run["candidate_sha"] == candidate_sha
    assert len(agents.review_requests) == 1


def test_run_repair_publication_default_drift_revalidates_in_same_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {"repair_source": "acceptance"},
    }
    states.save_run(str(state["run_id"]), state)

    class DriftingPublicationAgents(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_passing_artifact(), _passing_artifact()]
            self._drifted = False

        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            if not self._drifted:
                self._drifted = True
                (git_repo / "default-drift-before-repair-publication.txt").write_text(
                    "advanced\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "default-drift-before-repair-publication.txt"],
                    cwd=git_repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "advance default before repair publication"],
                    cwd=git_repo,
                    check=True,
                    capture_output=True,
                )
                data = json.loads(fixture.read_text(encoding="utf-8"))
                data["default_head_sha"] = git.resolve("main")
                fixture.write_text(json.dumps(data), encoding="utf-8")
                publisher.data["default_head_sha"] = git.resolve("main")
            return super().publication(request)

    agents = DriftingPublicationAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))
    run = result["run_acceptance"]
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    assert result["status"] == "run_publication_pending"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["status"] == "promoted"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert run["development_thread_history"] == ["run-repair-developer"]
    assert len(agents.review_requests) == 2
    assert not repair_checkout.exists()


def test_run_repair_development_uses_latest_candidate_finding(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {"repair_source": "acceptance"},
    }
    states.save_run(str(state["run_id"]), state)

    class IterativeRepairAgents(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [
                _candidate_finding_artifact(),
                _passing_artifact(),
            ]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            attempt = len(self.development_requests)
            (Path(str(request["checkout"])) / "run-repair.txt").write_text(
                f"repaired-{attempt}\n", encoding="utf-8"
            )
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary="Repaired the accumulated behavior.",
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            if request.get("candidate_acceptance") is True:
                assert Path(str(request["checkout"]), "run-repair.txt").read_text(
                    encoding="utf-8"
                ).startswith("repaired-")
            return ReviewResult(
                f"run-reviewer-{len(self.review_requests)}", self._reviews.pop(0)
            )

    agents = IterativeRepairAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert len(agents.development_requests) == 2
    assert agents.development_requests[1]["acceptance_artifact"] == (
        _candidate_finding_artifact()
    )


@pytest.mark.parametrize(
    "crash_key",
    [
        "crash_after_ensure_change_branch_once",
        "crash_after_ensure_change_pr_once",
        "crash_after_link_issue_branch_display_once",
    ],
)
def test_run_repair_recovers_lost_change_response_without_duplicate_worker(
    git_repo: Path, crash_key: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})[crash_key] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")
    publisher = FixtureGitHubPublisher(fixture, git)
    agents = ScriptedRunAgents()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
    )

    with pytest.raises(OSError, match="simulated lost response"):
        engine.accept(str(state["run_id"]))
    recovered = engine.accept(str(state["run_id"]))

    assert recovered["status"] == "run_publication_pending"
    assert len(agents.development_requests) == 1
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    pulls = delivery["pull_requests"]
    assert len(pulls) == 1
    assert len(delivery.get("linked_branch_display_attempts", [])) == 1
    expected_display_status = (
        "indeterminate" if crash_key.endswith("display_once") else "linked"
    )
    assert recovered["run_acceptance"]["completed_repair_jobs"][0][
        "linked_branch_display"
    ] == {"display_attempted": True, "status": expected_display_status}


def test_run_repair_rejects_a_same_named_foreign_ref_before_development(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    repair_branch = f"agent-run-repair/{state['run_id']}/1"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).setdefault("published_branches", {})[
        repair_branch
    ] = "foreign"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()

    with pytest.raises(ValueError, match="foreign identity"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))

    assert agents.development_requests == []


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_sha": "foreign"},
        {"base_sha": "foreign"},
        {"base_branch": "foreign"},
        {"head_repository": "foreign/project"},
        {"base_repository": "foreign/project"},
    ],
)
def test_run_repair_recovery_rejects_a_foreign_pr_identity_without_new_effects(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data.setdefault("delivery", {})
    delivery["crash_after_ensure_change_pr_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    )

    with pytest.raises(OSError, match="lost response"):
        engine.accept(str(state["run_id"]))
    data = json.loads(fixture.read_text(encoding="utf-8"))
    pull = data["delivery"]["pull_requests"][0]
    pull.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")
    requests_before = len(agents.development_requests)

    with pytest.raises(ValueError, match="foreign identity"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))

    assert len(agents.development_requests) == requests_before
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery


def test_run_acceptance_invocation_binds_the_reviewed_run_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class InvocationReviewer:
        def __init__(self) -> None:
            self.request: dict[str, Any] | None = None

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.request = request
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="run-reviewer", attempt_count=1)
            return ReviewResult("run-reviewer", _passing_artifact())

    agents = InvocationReviewer()
    completed = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    assert agents.request is not None
    invocation = completed["active_agent_invocation"]
    acceptance = completed["run_acceptance"]["acceptance_record"]
    assert invocation["status"] == "completed"
    assert invocation["role"] == "reviewer"
    assert invocation["phase"] == "run_acceptance"
    assert invocation["work_subject"] == f"run-acceptance:{state['run_id']}"
    assert invocation["generation"] == 1
    assert invocation["input_fingerprint"] == canonical_fingerprint(agents.request)
    assert invocation["currentness_boundary"] == {
        "reviewed_head_sha": acceptance["reviewed_head_sha"],
        "reviewed_default_base_sha": acceptance["reviewed_default_base_sha"],
        "expected_merge_tree": acceptance["expected_merge_tree"],
        "parent_revision": acceptance["parent_revision"],
        "ticket_graph_revision": acceptance["ticket_graph_revision"],
        "ticket_completion_records_fingerprint": canonical_fingerprint(
            acceptance["ticket_completion_records"]
        ),
    }


@pytest.mark.parametrize("new_thread", [False, True])
def test_run_acceptance_execution_failure_resumes_selected_thread(
    git_repo: Path, new_thread: bool
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=new_thread)

    run = resumed["run_acceptance"]
    assert resumed["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"
    if new_thread:
        assert run["reviewer_new_thread"] is True
        assert "reviewer_resume_thread_id" not in run
    else:
        assert run["reviewer_resume_thread_id"] == "failed-run-reviewer"


def test_second_reviewer_attempt_keeps_the_run_acceptance_generation(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 2,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="second-run-reviewer",
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["run_acceptance"]["validation_attempts"] == 2
    assert resumed["active_agent_invocation"]["generation"] == 1
    assert resumed["active_agent_invocation"]["status"] == "resuming"


@pytest.mark.parametrize(
    ("role", "work_subject"),
    [
        ("reviewer", "run-acceptance:{run_id}"),
        ("final_publication", "run-publication:{run_id}"),
    ],
)
def test_resume_rejects_stale_run_invocation_before_agent_start(
    git_repo: Path, role: str, work_subject: str
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    if role == "final_publication":
        state["run_acceptance"]["phase"] = "accepted"
        state["run_publication"] = {"phase": "publishing"}
    state["active_agent_invocation"] = _failed_invocation(
        role=role,
        phase="run_acceptance" if role == "reviewer" else "run_publication",
        work_subject=work_subject.format(run_id=state["run_id"]),
        generation=1,
        requested_thread_id="failed-thread",
        reported_thread_id="failed-thread",
        currentness_boundary={"reviewed_head_sha": "stale-head"},
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["terminal_kind"] == "run_acceptance_stale"
    assert resumed["run_acceptance"]["phase"] == "pending"
    assert "reviewer_resume_thread_id" not in resumed["run_acceptance"]


def test_ticket_completion_records_are_minimal_and_sorted_numerically(
    git_repo: Path,
) -> None:
    state, _states, git = _completed_run(git_repo)
    state["ticket_graph"]["tickets"]["10"] = {
        "number": 10,
        "title": "Ticket 10",
        "body": "Deliver ticket 10.",
        "content_revision": "ticket-10-revision",
    }
    state["ticket_jobs"]["10"] = {
        "ticket_number": 10,
        "phase": "completed",
        "integrated_sha": "integrated-10",
        "effective_revision": "effective-10",
        "acceptance_record": {
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
            "artifact": _passing_artifact(),
        },
    }
    state["ticket_jobs"]["2"].update(
        {
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "acceptance_record": {
                "reviewed_base_sha": git.resolve(str(state["run_branch"])),
                "reviewed_candidate_tree": "tree-2",
                "artifact": _passing_artifact(),
            },
        }
    )

    records = ticket_completion_records(state)

    assert records == [
        {
            "ticket_number": 2,
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-2",
        },
        {
            "ticket_number": 10,
            "integrated_sha": "integrated-10",
            "effective_revision": "effective-10",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
        },
    ]


def test_closed_ticket_edits_do_not_change_its_completion_revision(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    original_ticket = dict(state["ticket_graph"]["tickets"]["2"])
    original_completion = ticket_completion_records(state)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["title"] = "Edited after completion"
    data["issues"]["2"]["body"] = "This edit is outside the completed Ticket."
    data["parent"]["body"] = "The live Parent changed after Ticket completion."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["ticket_graph"]["tickets"]["2"] == original_ticket
    assert ticket_completion_records(refreshed) == original_completion


def test_parent_drift_immediately_stales_completed_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))
    assert accepted["run_acceptance"]["phase"] == "accepted"
    accepted["run_acceptance"].update(
        {
            "prior_human_blockers": ["An old blocker must not cross generations."],
            "human_response_history": [{"generation": 1, "response": "old"}],
            "reviewer_resume_thread_id": "old-run-reviewer",
        }
    )
    accepted["run_publication"] = {"phase": "pending"}
    states.save_run(str(state["run_id"]), accepted)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Updated after all Tickets completed."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["terminal_kind"] == "run_acceptance_stale"
    assert refreshed["run_acceptance"]["phase"] == "pending"
    assert "prior_human_blockers" not in refreshed["run_acceptance"]
    assert "human_response_history" not in refreshed["run_acceptance"]
    assert "reviewer_resume_thread_id" not in refreshed["run_acceptance"]
    assert refreshed["run_publication"]["phase"] == "stale"


def test_reopened_completed_ticket_fails_closed(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["state"] = "OPEN"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "unsupported_scope_change"
    assert refreshed["terminal_kind"] == "unsupported_scope_change"
    assert refreshed["diagnostics"] == [
        {
            "code": "completed_ticket_reopened",
            "message": "Completed Ticket #2 was reopened outside Run Abandonment Recovery",
            "ticket_numbers": [2],
        }
    ]


def test_run_acceptance_new_thread_resume_omits_failed_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=True)

    class NewThreadReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            assert request["thread_id"] is None
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="new-run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="new-run-reviewer", attempt_count=1)
            return ReviewResult("new-run-reviewer", _passing_artifact())

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=NewThreadReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(resumed["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["reviewer_thread_ids"] == [
        "new-run-reviewer"
    ]


def test_run_repair_development_human_blocker_stops_before_candidate_or_pr(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairDevelopment(ScriptedRunAgents):
        def develop(
            self, request: dict[str, Any]
        ) -> DevelopmentResult | HumanBlockerResult:
            self.development_requests.append(request)
            return HumanBlockerResult(
                thread_id="run-repair-development-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairDevelopment(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "developing"
    assert repair["development_thread_id"] == "run-repair-development-blocked"
    assert "candidate_sha" not in repair
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["pull_requests"] == []
    status = stdout_json(
        run_cli(git_repo, fixture, "status", str(state["run_id"]), "--json")
    )
    assert status["status"] == "ready_for_human"
    assert status["diagnostics"][0]["message"].startswith("GitHub denied access")
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("thread_id") == "run-repair-development-blocked"
        and event.get("worker") == "开发工作代理"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_repair_reviewer_human_blocker_history_uses_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_repair_artifact(), _human_artifact()]

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["blocked_reason"] == "reviewer_requires_human"
    assert repair["reviewer_thread_ids"][-1] == "run-reviewer-2"
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "独立验收工作代理"
        and event.get("thread_id") == "run-reviewer-2"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_repair_publication_human_blocker_stops_before_pr_mutation(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairPublication(ScriptedRunAgents):
        def publication(
            self, request: dict[str, Any]
        ) -> HumanBlockerResult:
            del request
            return HumanBlockerResult(
                thread_id="run-repair-publication-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairPublication(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "accepted"
    assert repair["publication_thread_id"] == "run-repair-publication-blocked"
    assert repair["publication_attempts"] == 1
    assert "publication_sha" not in repair
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "发布工作代理"
        and event.get("attempt") == 1
        and event.get("thread_id") == "run-repair-publication-blocked"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_acceptance_rejects_ticket_or_previous_reviewer_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("ticket-reviewer", _passing_artifact())

    with pytest.raises(ValueError, match="new Reviewer Thread"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedReviewer(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_run_acceptance_human_resume_reuses_thread_and_clears_current_blocker(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenPassingReviewer:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []
            self.checkouts: list[Path] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.requests.append(request)
            self.checkouts.append(Path(str(request["checkout"])))
            if len(self.requests) == 1:
                return ReviewResult("blocked-run-reviewer", _human_artifact())
            assert request["thread_id"] == "blocked-run-reviewer"
            assert request["prior_human_blockers"] == [_BLOCKED_EVIDENCE]
            assert request["human_response_history"] == [
                {
                    "generation": 1,
                    "human_blockers": [
                        _BLOCKED_EVIDENCE
                    ],
                    "response": "Issue read access has been granted.",
                }
            ]
            return ReviewResult("blocked-run-reviewer", _passing_artifact())

    agents = HumanThenPassingReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )

    blocked = engine.accept(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    assert all(not checkout.exists() for checkout in agents.checkouts)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        human_response="Issue read access has been granted.",
    )
    assert resumed["run_acceptance"]["prior_human_blockers"] == [
        _BLOCKED_EVIDENCE
    ]
    assert resumed["run_acceptance"]["human_response_history"] == [
        {
            "generation": 1,
            "human_blockers": [
                _BLOCKED_EVIDENCE
            ],
            "response": "Issue read access has been granted.",
        }
    ]

    accepted = engine.accept(str(state["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    run = accepted["run_acceptance"]
    assert run["reviewer_thread_ids"] == ["blocked-run-reviewer"]
    assert len(set(agents.checkouts)) == 2
    assert all(not checkout.exists() for checkout in agents.checkouts)
    assert run["human_blocker_history"] == [
        {
            "phase": "pending",
            "human_blockers": [
                _BLOCKED_EVIDENCE
            ],
        }
    ]
    for key in ("human_blockers", "human_blocker_phase", "prior_human_blockers"):
        assert key not in run


def test_run_acceptance_human_resume_rejects_an_older_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenHistoricalReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            self.calls += 1
            if self.calls == 1:
                return ReviewResult("blocked-latest-reviewer", _human_artifact())
            return ReviewResult("older-reviewer", _passing_artifact())

    agents = HumanThenHistoricalReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    blocked = engine.accept(str(state["run_id"]))
    run = blocked["run_acceptance"]
    run["reviewer_thread_ids"].insert(0, "older-reviewer")
    states.save_run(str(state["run_id"]), blocked)
    Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), resume_human_blocker=True)

    with pytest.raises(
        ValueError, match="Human Blocker resume requires the latest Reviewer Thread"
    ):
        engine.accept(str(state["run_id"]))


def test_stale_run_repair_publication_returns_to_fresh_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    run = state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "acceptance_record": {"reviewed_head_sha": git.resolve(state["run_branch"])},
    }
    job: dict[str, Any] = {
        "phase": "publication_pending",
        "repair_source": "acceptance",
        "acceptance_artifact": _repair_artifact(),
    }
    run["repair_job"] = job

    engine._invalidate_stale_repair(
        state, job, git_repo / "unused-checkout"
    )

    assert job["phase"] == "stale"
    assert "repair_job" not in run
    assert state["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"


def test_run_acceptance_rejects_run_repair_developer_as_a_reviewer(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedRunDeveloper(ScriptedRunAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            artifact = self._reviews.pop(0)
            thread_id = (
                "run-reviewer-1"
                if len(self.review_requests) == 1
                else "run-repair-developer"
            )
            return ReviewResult(thread_id, artifact)

    with pytest.raises(
        ValueError, match="Fresh Acceptance cannot reuse the Development Thread"
    ):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedRunDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_run_repair_rejects_ticket_development_thread_reuse(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedTicketDeveloper(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            return DevelopmentResult("ticket-developer", result.summary)

    with pytest.raises(ValueError, match="not independent"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedTicketDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_run_acceptance_discards_a_review_when_its_parent_snapshot_drifts(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    fixture = git_repo / "github.json"

    class DriftingReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["parent"]["body"] = "Changed while the Reviewer was running."
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return ReviewResult("run-reviewer", _passing_artifact())

    pending = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=DriftingReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert pending["run_acceptance"]["phase"] == "pending"
    assert "acceptance_artifact" not in pending["run_acceptance"]
    assert "acceptance_record" not in pending["run_acceptance"]


def test_run_acceptance_refreshes_currentness_before_reusing_an_accepted_run(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    initial = ScriptedRunAgents()
    initial._reviews = [_passing_artifact()]
    RunAcceptanceEngine(
        git=git,
        states=states,
        agents=initial,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed after acceptance."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    class FreshReviewer:
        def __init__(self) -> None:
            self.review_requests: list[dict[str, Any]] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            return ReviewResult("fresh-run-reviewer", _passing_artifact())

    refreshed = FreshReviewer()

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=refreshed,
        github=FixtureGitHubPublisher(fixture, git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2
    assert len(refreshed.review_requests) == 1


def test_run_acceptance_fixture_repairs_malformed_output_in_same_thread(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "repair-run-review.json"
    agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "run-reviewer-thread",
                        "thread_id": "run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["reported_thread_id"] == "run-reviewer-thread"
    assert invocation["attempt_count"] == 2


def test_run_acceptance_fixture_missing_thread_marks_invocation_failed(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "missing-run-review-thread.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"no_thread": True, "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["active_agent_invocation"]["status"] == "failed"


def test_run_acceptance_fixture_records_a_fresh_thread_without_expectation(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "fresh-run-review.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"thread_id": "fresh-run-reviewer", "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["requested_thread_id"] is None
    assert invocation["reported_thread_id"] == "fresh-run-reviewer"


def test_run_acceptance_fixture_resume_gets_a_fresh_repair_budget(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    failed_agents = git_repo / "failed-run-review.json"
    failed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2

    resumed_agents = git_repo / "resumed-run-review.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        git_repo / "github.json",
        "resume",
        str(state["run_id"]),
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    history = persisted["agent_invocation_history"]
    assert history[-2]["status"] == "failed"
    assert history[-2]["attempt_count"] == 3
    assert history[-1]["status"] == "completed"
    assert history[-1]["attempt_count"] == 1


def test_accept_run_cli_enters_publication_pending_after_fresh_run_review(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    ticket_agents = git_repo / "ticket-agents.json"
    ticket_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Delivered the ticket.",
                        "write_files": {"ticket.txt": "done\n"},
                    }
                ],
                "publications": [
                    {
                        "commit_message": "feat(run): deliver the ticket outcome",
                        "pr_title": "feat(run): deliver the ticket outcome",
                        "pr_body_markdown": (
                                "## What Problem This Solves\n\nThe Ticket was pending.\n\n"
                            "## Why This Change Was Made\n\nIt completes the requested path.\n\n"
                            "## User Impact\n\nThe path is available.\n\n"
                            "## Evidence\n\nThe fixture flow passed."
                        ),
                    }
                ],
                "reviews": [{"thread_id": "ticket-reviewer", "artifact": _passing_artifact()}],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(ticket_agents)
    )
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"
    run_agents = git_repo / "run-agents.json"
    run_agents.write_text(
        json.dumps(
            {"reviews": [{"thread_id": "run-reviewer", "artifact": _passing_artifact()}]},
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        git_repo, fixture, "accept-run", run_id, "--agent-fixture", str(run_agents)
    )

    assert accepted.returncode == 0, accepted.stderr
    assert stdout_json(accepted)["status"] == "run_publication_pending"


def test_run_repair_drift_discards_repair_before_fresh_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    repair_branch = f"agent-run-repair/{state['run_id']}/1"
    publisher.ensure_run_repair_branch(
        branch=repair_branch, base_branch=str(state["run_branch"])
    )
    pr_number = publisher.ensure_run_repair_pr(
        branch=repair_branch,
        base_branch=str(state["run_branch"]),
        title="old repair",
        body="old repair body",
    )
    run = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "repair_job": {
            "phase": "developing",
            "repair_generation": 1,
            "repair_branch": repair_branch,
            "base_sha": "stale-run-base",
            "parent_revision": state["parent"]["revision"],
            "ticket_graph_revision": state["ticket_graph"]["revision"],
            "ticket_completion_records": [],
            "repair_source": "acceptance",
            "acceptance_artifact": _repair_artifact(),
            "development_thread_id": "repair-old",
            "reviewer_thread_ids": ["repair-reviewer-old"],
            "pr_number": pr_number,
            "publication_sha": git.resolve(str(state["run_branch"])),
        },
    }
    state["run_acceptance"] = run
    state["status"] = "run_acceptance_pending"
    states.save_run(str(state["run_id"]), state)
    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert "requeue_required" not in refreshed
    assert "repair_job" not in refreshed["run_acceptance"]
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
    assert pulls[0]["state"] == "OPEN"


def test_run_repair_discards_an_inflight_development_after_parent_drift(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    original_head = git.resolve(str(state["run_branch"]))

    class DriftingRepairDeveloper(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["parent"]["body"] = "Changed while Run Repair was running."
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return result

    agents = DriftingRepairDeveloper()
    agents._reviews = [_repair_artifact()]
    stale = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert "repair_job" not in stale["run_acceptance"]
    assert "run-repair-developer" in stale["run_acceptance"][
        "discarded_repair_thread_ids"
    ]
    assert git.resolve(str(state["run_branch"])) == original_head


def test_run_repair_discards_an_inflight_development_after_final_pr_drift(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    pr_number = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": pr_number}
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(pr_number),
        },
    }
    states.save_run(str(state["run_id"]), state)

    class DriftingRepairDeveloper(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            for pull in publisher.data["delivery"]["pull_requests"]:
                if pull["number"] == pr_number:
                    pull["state"] = "CLOSED"
            return result

    agents = DriftingRepairDeveloper()
    stale = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert "repair_job" not in stale["run_acceptance"]


@pytest.mark.parametrize(
    "error",
    [
        GitHubReadError("github_timeout", "fingerprint read timed out"),
        OSError("fingerprint transport unavailable"),
        TimeoutError("fingerprint read timed out"),
    ],
)
def test_required_check_trigger_fingerprint_read_is_supervised_in_same_cycle(
    git_repo: Path, error: BaseException
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class FingerprintReadFailsOncePublisher(FixtureGitHubPublisher):
        evidence_error: BaseException | None = error

        def required_check_evidence(self, pr_number: int) -> dict[str, Any]:
            if self.evidence_error is not None:
                raised = self.evidence_error
                self.evidence_error = None
                raise raised
            return super().required_check_evidence(pr_number)

    publisher = FingerprintReadFailsOncePublisher(fixture, git)
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]),
        expected_head_sha=git.resolve(str(state["run_branch"])),
    )
    pr_number = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    evidence = FixtureGitHubPublisher(fixture, git).required_check_evidence(pr_number)
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": pr_number}
    state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": evidence,
        },
    }
    states.save_run(str(state["run_id"]), state)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        currentness_reader=FixtureGitHubReader(fixture),
    )

    waiting = engine.accept(str(state["run_id"]))

    run = waiting["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["supervision_window"]["kind"] == "github_convergence"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["generation"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["phase"] == "developing"
    assert job["modification_attempts"] == 0
    assert job["development_thread_id"] is None
    assert checkout.exists()
    assert agents.development_requests == []

    completed = engine.accept(str(state["run_id"]))

    assert completed["status"] == "run_publication_pending"
    completed_run = completed["run_acceptance"]
    assert completed_run["repair_generation"] == 1
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1
    assert len(agents.development_requests) == 1
    assert agents.development_requests[0]["thread_id"] is None


def test_stale_run_repair_keeps_inflight_review_threads_out_of_fresh_review(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class DriftingRepairReviewer(ScriptedRunAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            result = super().review(request)
            if request.get("candidate_acceptance") is True:
                data = json.loads(fixture.read_text(encoding="utf-8"))
                data["parent"]["body"] = "Changed while Run Repair review was running."
                fixture.write_text(json.dumps(data), encoding="utf-8")
            return result

    stale = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=DriftingRepairReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert set(stale["run_acceptance"]["discarded_repair_thread_ids"]) >= {
        "run-repair-developer",
        "run-reviewer-1",
    }


def test_stale_run_repair_keeps_threads_out_of_fresh_review(
    git_repo: Path,
) -> None:
    state, _states, _git = _completed_run(git_repo)
    run = state["run_acceptance"] = {
        "phase": "repairing",
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": ["run-reviewer"],
        "repair_job": {
            "development_thread_id": "repair-developer",
            "development_thread_history": ["repair-developer-old"],
            "reviewer_thread_ids": ["repair-reviewer"],
        },
    }

    invalidate_stale_run_repair(state)

    engine = RunAcceptanceEngine(
        git=_git,
        states=_states,
        agents=ScriptedRunAgents(),
    )
    assert engine._all_prior_threads(state, run) >= {
        "run-reviewer",
        "repair-developer",
        "repair-developer-old",
        "repair-reviewer",
    }


def test_accept_run_does_not_review_after_a_github_refresh_failure(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["error"] = {"code": "github_read_failed", "message": "offline"}
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = git_repo / "run-agents.json"
    agents.write_text(json.dumps({"reviews": []}), encoding="utf-8")

    result = run_internal_stage(
        git_repo,
        fixture,
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert result.returncode == 0, result.stderr
    assert stdout_json(result)["status"] == "waiting_external"
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert "run_acceptance" not in persisted


def test_interrupted_run_review_restarts_with_a_fresh_attempt(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    states.save_run(str(state["run_id"]), state)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(
        str(state["run_id"])
    )

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2


def test_default_branch_drift_invalidates_run_acceptance_and_rechecks_merge_preview(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    first = ScriptedRunAgents()
    first._reviews = [_passing_artifact()]
    RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    (git_repo / "default-branch.txt").write_text("new default\n", encoding="utf-8")
    subprocess.run(["git", "add", "default-branch.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default branch"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    second = ScriptedRunAgents()
    second._reviews = [_passing_artifact()]
    second.review_requests = [{}]

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=second,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2
    assert result["run_acceptance"]["acceptance_record"]["reviewed_default_base_sha"] == git.resolve("main")


def test_controller_default_branch_drift_stales_accepted_run(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    first = ScriptedRunAgents()
    first._reviews = [_passing_artifact()]
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first,
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    assert accepted["run_acceptance"]["phase"] == "accepted"

    (git_repo / "controller-default-drift.txt").write_text(
        "advanced\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "controller-default-drift.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default for controller refresh"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["default_head_sha"] = git.resolve("main")
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["run_acceptance"]["phase"] == "pending"


def test_run_acceptance_discards_inflight_review_when_default_base_advances(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class DriftingDefaultReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            (git_repo / "default-branch.txt").write_text("advanced\n", encoding="utf-8")
            subprocess.run(["git", "add", "default-branch.txt"], cwd=git_repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance default during review"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["default_head_sha"] = git.resolve("main")
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return ReviewResult("stale-reviewer", _passing_artifact())

    stale = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=DriftingDefaultReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert "acceptance_record" not in stale["run_acceptance"]

    class ReusedReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("stale-reviewer", _passing_artifact())

    with pytest.raises(ValueError, match="new Reviewer Thread"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedReviewer(),
            github=FixtureGitHubPublisher(fixture, git),
            default_head_sha=git.resolve("main"),
            currentness_reader=FixtureGitHubReader(fixture),
        ).accept(str(state["run_id"]))

    class FreshReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            return ReviewResult("fresh-reviewer", _passing_artifact())

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=FreshReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["reviewer_thread_ids"] == [
        "stale-reviewer",
        "fresh-reviewer",
    ]
    assert accepted["run_acceptance"]["acceptance_record"]["reviewed_default_base_sha"] == (
        git.resolve("main")
    )


def test_run_acceptance_binds_the_previewed_merge_tree(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))

    record = accepted["run_acceptance"]["acceptance_record"]
    assert record["expected_merge_tree"] == git.expected_merge_tree(
        default_head_sha=git.resolve("main"),
        run_head_sha=git.resolve(str(state["run_branch"])),
    )
