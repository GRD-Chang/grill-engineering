from __future__ import annotations

import subprocess
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.delivery_history import history_records
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import invalidate_stale_run_repair
from agent_run.run_publication import RunPublicationEngine
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.state_contract import IncompatibleRunStateError

from test_cli import run_cli, stdout_json

from run_acceptance_test_support import (
    ScriptedRunAgents,
    _canonical_run_budget,
    _candidate_finding_artifact,
    _completed_run,
    _passing_artifact,
    _repair_artifact,
)
from run_publication_test_support import RunPublicationAgents, _accepted_run


def _malformed_candidate_history() -> list[dict[str, object]]:
    return [{"candidate_sha": "candidate", "artifact": {"result_kind": "acceptance"}}]

def test_candidate_history_state_load_rejects_nested_acceptance_record_before_mutation(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "pending",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
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
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_cycle": {"status": "active"},
        "candidate_acceptance_history": [],
        "repair_job": {
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "candidate_acceptance_history": _malformed_candidate_history(),
        },
    }
    before = deepcopy(state)

    with pytest.raises(IncompatibleRunStateError):
        invalidate_stale_run_repair(state)

    assert state == before

@pytest.mark.parametrize(
    "mismatch",
    [
        "candidate_tree",
        "repair_base",
        "actual_run_tree",
        "default_head",
        "parent_revision",
        "graph_revision",
        "completion_records",
    ],
)
def test_run_repair_promotion_rejects_each_authority_binding(
    git_repo: Path, mismatch: str,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class PromotionMismatchEngine(RunAcceptanceEngine):
        def _candidate_promotion_record(
            self,
            live_state: dict[str, Any],
            job: dict[str, Any],
            integrated: str,
        ) -> dict[str, Any] | None:
            record = job["acceptance_record"]
            if mismatch == "candidate_tree":
                record["reviewed_candidate_tree"] = "mismatched-candidate-tree"
            elif mismatch == "repair_base":
                record["repair_base_run_head_sha"] = "mismatched-repair-base"
            elif mismatch == "actual_run_tree":
                subprocess.run(
                    [
                        "git",
                        "update-ref",
                        f"refs/heads/{live_state['run_branch']}",
                        git.resolve("main"),
                    ],
                    cwd=git.root,
                    check=True,
                )
            elif mismatch == "default_head":
                record["reviewed_default_base_sha"] = integrated
            elif mismatch == "parent_revision":
                live_state["parent"]["revision"] = "mismatched-parent-revision"
            elif mismatch == "graph_revision":
                live_state["ticket_graph"]["revision"] = "mismatched-graph-revision"
            elif mismatch == "completion_records":
                live_state["ticket_jobs"]["2"]["effective_revision"] = (
                    "mismatched-completion-revision"
                )
            return super()._candidate_promotion_record(live_state, job, integrated)

    agents = ScriptedRunAgents()
    result = PromotionMismatchEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
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

@pytest.mark.parametrize(
    ("checks", "evidence"),
    [
        ("unknown", None),
        (
            "fail",
            {
                "pr_number": 1,
                "checks": [
                    {
                        "name": "cancelled",
                        "workflow": "ci",
                        "bucket": "cancel",
                        "state": "CANCELLED",
                        "link": "https://example.invalid/cancelled",
                    }
                ],
            },
        ),
        (
            "fail",
            {
                "pr_number": 1,
                "checks": [
                    {
                        "name": "platform",
                        "workflow": "ci",
                        "bucket": "fail",
                        "state": "FAILURE",
                        "link": "https://example.invalid/platform",
                    }
                ],
            },
        ),
        (
            "fail",
            {
                "pr_number": 1,
                "checks": [
                    {
                        "name": "unknown",
                        "workflow": "ci",
                        "bucket": "fail",
                        "link": "https://example.invalid/unknown",
                    }
                ],
            },
        ),
    ],
    ids=("unknown-status", "cancelled", "platform-failure", "unknown-evidence"),
)
def test_run_repair_non_repairable_required_checks_wait_without_new_candidate(
    git_repo: Path, checks: str, evidence: dict[str, object] | None,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data.setdefault("delivery", {})
    delivery["required_checks"] = [checks]
    if evidence is not None:
        delivery["required_check_evidence"] = evidence
    fixture.write_text(json.dumps(data), encoding="utf-8")

    agents = ScriptedRunAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    run = result["run_acceptance"]
    job = run["repair_job"]
    assert result["status"] == "waiting_external"
    assert result["terminal_kind"] == "waiting_external"
    assert job["phase"] == "waiting_checks"
    assert job["modification_attempts"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert len(job["candidate_acceptance_history"]) == 1
    assert len(agents.development_requests) == 1
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

    archived_owners = second.get("retired_semantic_attempt_owners", [])
    assert len(archived_owners) == 1
    archived_owner = archived_owners[0]
    archived_attempts = archived_owner["semantic_attempt_history"]
    assert any(
        attempt.get("role") == "development"
        and attempt.get("development_summary")
        for attempt in archived_attempts
    )
    assert any(attempt.get("role") == "reviewer" for attempt in archived_attempts)
    assert isinstance(archived_owner["review_budget_history"], list)
    assert archived_owner["review_budget"]["review_artifacts"]

    archived_ids = {
        attempt["attempt_id"]
        for attempt in archived_attempts
        if isinstance(attempt.get("attempt_id"), str)
    }
    audit = {
        "semantic_agent_attempts": archived_attempts,
        "agent_invocations": second.get("agent_invocation_history", []),
        "timeline": second.get("timeline", []),
        "timeline_continuation": second.get("timeline_continuation", []),
        "agent_resumes": [],
    }
    archived_records = [
        record
        for record in history_records(second, audit)
        if record.get("attempt_id") in archived_ids
    ]
    assert archived_records
    assert any(record.get("development_summary") for record in archived_records)
    assert any(
        isinstance(record.get("acceptance_artifact"), dict)
        for record in archived_records
        if record.get("role") == "reviewer"
    )
    assert any(record.get("publication") for record in archived_records)

    history = run_cli(
        git_repo, fixture, "history", str(state["run_id"]), "--plain", "--details"
    )
    assert history.returncode == 0, history.stderr
    assert "Development Summary" in history.stdout
    assert "必需检查结果" in history.stdout
    assert "PR 编号" in history.stdout
    for internal_field in (
        "reviewer_thread_id",
        "policy_snapshot",
        "review_budget",
        "acceptance_record",
    ):
        assert internal_field not in history.stdout

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
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(
                final_pr, expected_head_sha=git.resolve(str(state["run_branch"]))
            ),
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

    class StatusAssertingRepairAgents(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            json_status = run_cli(
                git_repo,
                fixture,
                "status",
                str(state["run_id"]),
                "--json",
            )
            text_status = run_cli(
                git_repo,
                fixture,
                "status",
                str(state["run_id"]),
            )
            assert json_status.returncode == text_status.returncode == 0
            status = stdout_json(json_status)
            assert status["phase"] == "developing"
            assert status["worker"]["role"] == "运行修复开发工作代理"
            assert status["review_budget"]["development_limit"] == 10
            assert status["review_budget"]["reviewer_limit"] == 11
            assert status["progress"]["phase"] == "developing"
            assert status["progress"]["current_object"] == "Run Acceptance"
            assert status["progress"]["round_progress"]["development_label"] == (
                "Run Development"
            )
            assert "阶段:       开发中" in text_status.stdout
            assert "当前对象:   Run Acceptance" in text_status.stdout
            assert (
                "最近 Agent: Development Agent · Run Acceptance（开发 Agent）"
                in text_status.stdout
            )
            assert "Run Development     1 / 10 轮" in text_status.stdout
            assert "Run Review          0 / 11 轮" in text_status.stdout
            assert "等待人工批准" not in text_status.stdout
            assert "当前对象:   Run Publication" not in text_status.stdout

            active_repair = states.load_current_run(str(state["run_id"]))
            publication_phase_labels = {
                "blocked": "已阻塞",
                "ready_for_human": "等待人工处理",
                "publication_pending": "等待发布",
            }
            for publication_phase, phase_label in publication_phase_labels.items():
                gated = deepcopy(active_repair)
                publication = gated["run_publication"]
                publication["phase"] = publication_phase
                if publication_phase == "publication_pending":
                    retry = {"attempts": 5, "limit": 5}
                    attempt = allocate_semantic_attempt(
                        publication,
                        role="publication",
                        work_subject=f"run-publication:{state['run_id']}",
                        generation=1,
                        currentness_boundary={
                            "run_head_sha": git.resolve(str(state["run_branch"]))
                        },
                        ordinal=1,
                    )
                    publication["publication_attempts"] = 1
                    publication["publication_operation_retry"] = retry
                    close_semantic_attempt(
                        publication,
                        attempt,
                        outcome="publication_artifact",
                    )
                    gated["status"] = "publication_pending"
                    gated["terminal_kind"] = "publication_pending"
                else:
                    publication["blocked_reason"] = "agent_requires_human"
                    publication["human_blockers"] = [
                        "Final Run publication requires maintainer action."
                    ]
                    gated["status"] = "ready_for_human"
                    gated["terminal_kind"] = "ready_for_human"
                states.save_run(str(state["run_id"]), gated)

                gated_json_result = run_cli(
                    git_repo,
                    fixture,
                    "status",
                    str(state["run_id"]),
                    "--json",
                )
                gated_text = run_cli(
                    git_repo,
                    fixture,
                    "status",
                    str(state["run_id"]),
                )
                assert gated_json_result.returncode == gated_text.returncode == 0, (
                    publication_phase,
                    gated_json_result.stdout,
                    gated_text.stdout,
                )
                gated_json = stdout_json(gated_json_result)
                assert gated_json["phase"] == publication_phase
                assert gated_json["progress"]["phase"] == publication_phase
                assert gated_json["progress"]["current_object"] == "Run Publication"
                assert gated_json["worker"] is None
                assert gated_json["progress"]["current_agent"] is None
                assert f"阶段:       {phase_label}" in gated_text.stdout
                assert "当前对象:   Run Publication" in gated_text.stdout
                assert "运行修复开发工作代理" not in gated_text.stdout

            states.save_run(str(state["run_id"]), active_repair)
            return super().develop(request)

    repair_agents = StatusAssertingRepairAgents()
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

    json_status = stdout_json(
        run_cli(git_repo, fixture, "status", str(state["run_id"]), "--json")
    )
    text_status = run_cli(git_repo, fixture, "status", str(state["run_id"]))
    assert json_status["phase"] == "stale"
    assert json_status["progress"]["current_object"] == "Run Publication"
    assert "阶段:       已失效" in text_status.stdout
    assert "当前对象:   Run Publication" in text_status.stdout


def test_required_check_repair_promotion_clears_old_observation_and_archives_provenance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    old_head = git.resolve(str(state["run_branch"]))
    publisher.ensure_final_run_ref(
        branch=str(state["run_branch"]), expected_head_sha=old_head
    )
    final_pr = publisher.ensure_run_pr(
        branch=str(state["run_branch"]),
        base_branch="main",
        expected_head_sha=old_head,
        expected_base_sha=git.resolve("main"),
        title="Final Run",
        body="Original final Run narrative.",
    )
    failure_evidence = publisher.required_check_evidence(
        final_pr, expected_head_sha=old_head
    )
    old_observation = {
        "pr_number": final_pr,
        "head_sha": old_head,
        "result": "fail",
        "checks": deepcopy(failure_evidence["checks"]),
    }
    state["run_publication"] = {
        "phase": "ready_for_approval",
        "pr_number": final_pr,
        "required_checks_evidence": old_observation,
        "artifact": {"pr_body_markdown": "Original final Run narrative."},
        "approval_grant": {"fingerprint": "old-observation"},
    }
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "acceptance_artifact": _repair_artifact(),
        "repair_request": {
            "repair_source": "required_checks",
            "ci_evidence": failure_evidence,
        },
    }
    states.save_run(str(state["run_id"]), state)

    repair_agents = ScriptedRunAgents()
    repair_agents._reviews = [_passing_artifact()]
    promoted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=repair_agents,
        github=publisher,
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert promoted["status"] == "run_publication_pending"
    publication = promoted["run_publication"]
    assert publication["phase"] == "stale"
    assert "required_checks_evidence" not in publication
    assert "required_checks_observation_status" not in publication
    completed = promoted["run_acceptance"]["completed_repair_jobs"][-1]
    archived = completed["ci_evidence"]
    assert archived["pr_number"] == final_pr
    assert archived["head_sha"] == old_head
    assert archived["result"] == "fail"
    assert archived["checks"] == failure_evidence["checks"]
    assert archived["checks"][0]["job"]["head_sha"] == old_head
    assert archived["checks"][0]["job"]["steps"][0]["conclusion"] == "failure"
    assert promoted["run_acceptance"]["publication_sha"] != old_head

    reloaded = states.load_current_run(str(state["run_id"]))
    assert reloaded is not None
    assert "required_checks_evidence" not in reloaded["run_publication"]
    assert reloaded["run_acceptance"]["completed_repair_jobs"][-1][
        "ci_evidence"
    ] == archived


def test_required_check_origin_survives_acceptance_repair_before_promotion(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()

    failed = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    final_pr = int(failed["run_publication"]["pr_number"])
    old_head = git.resolve(str(state["run_branch"]))
    failure_evidence = publisher.required_check_evidence(
        final_pr, expected_head_sha=old_head
    )

    class TwoRoundRepairAgents(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            if len(self.development_requests) == 2:
                checkout = Path(str(request["checkout"]))
                (checkout / "follow-up-repair.txt").write_text(
                    "second repair\n", encoding="utf-8"
                )
            return result

    agents = TwoRoundRepairAgents()
    agents._reviews = [_candidate_finding_artifact(), _passing_artifact()]
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["phase"] == "accepted"
    assert len(agents.development_requests) == 2
    assert len(agents.review_requests) == 2
    archived = result["run_acceptance"]["completed_repair_jobs"][-1]["ci_evidence"]
    assert archived == {
        **failure_evidence,
        "head_sha": old_head,
        "result": "fail",
    }
    assert result["run_publication"]["phase"] == "stale"
    assert "required_checks_evidence" not in result["run_publication"]
