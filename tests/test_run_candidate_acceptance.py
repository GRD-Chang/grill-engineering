from __future__ import annotations

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.change_currentness import unknown_pr_mutation
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import (
    invalidate_run_acceptance,
    ticket_completion_records,
)
from agent_run.state import StateStore
from agent_run.state_contract import (
    require_candidate_acceptance_history,
    require_current_run_state,
)

from test_cli import run_cli, stdout_json

from run_acceptance_test_support import (
    ScriptedRunAgents,
    _BLOCKED_EVIDENCE,
    _canonical_run_budget,
    _candidate_finding_artifact,
    _completed_run,
    _human_artifact,
    _passing_artifact,
    _repair_artifact,
)

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
    assert [
        item["reviewer_thread_id"]
        for item in run["review_budget"]["review_artifacts"]
    ] == ["run-reviewer-1", "run-reviewer-2"]

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
    assert agents.development_requests[0]["repair_scope"] == "run_repair"
    assert agents.development_requests[0]["repair_source"] == "acceptance"
    assert agents.development_requests[0]["acceptance_artifact"] == _repair_artifact()
    assert len(agents.review_requests) == 2
    candidate_request = agents.review_requests[1]
    assert candidate_request["candidate_acceptance"] is True
    assert candidate_request["repair_scope"] == "run_repair"
    assert candidate_request["previous_acceptance_artifact"] == _repair_artifact()
    assert candidate_request["previous_review_identity"] == agents.review_requests[0][
        "current_review_identity"
    ]
    assert set(candidate_request["current_review_identity"]) == {
        "run_base_sha",
        "repair_candidate_sha",
        "expected_merge_tree",
    }
    assert candidate_request["current_review_identity"]["repair_candidate_sha"] == (
        run["candidate_sha"]
    )
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
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 4,
        "repair_generation": 2,
        "modification_attempts": 10,
        "validation_attempts": 10,
        "development_thread_id": "run-repair-developer",
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "acceptance_artifact": artifact,
        "repair_cycle": {
            "generation": 2,
            "status": "active",
            "code_modification_attempts": 10,
            "validation_attempts": 10,
        },
    }
    run["repair_job"] = {
        "run_id": state["run_id"],
        "phase": "escalating",
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_attempt": 1,
        "repair_generation": 2,
        "repair_branch": f"agent-run-repair/{state['run_id']}/1",
        "base_sha": base_sha,
        "default_base_sha": git.resolve("main"),
        "repair_base_run_head_sha": base_sha,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance",
        "repair_mode": "squash",
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
    text_status_result = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"])
    )
    history_result = run_cli(
        git_repo, git_repo / "github.json", "history", str(state["run_id"]), "--json"
    )
    assert status_result.returncode == text_status_result.returncode == 0
    assert history_result.returncode == 0
    status = stdout_json(status_result)
    assert status["run_repair"]["acceptance_generation"] == 4
    assert status["run_repair"]["repair_cycle_generation"] == 2
    assert status["run_repair"]["candidate_validation_phase"] == "blocked"
    assert status["run_repair"]["cycle_status"] == "budget_exhausted"
    assert status["run_repair"]["code_modification_attempts"] == 10
    assert "Run Acceptance Generation 4" in text_status_result.stdout
    assert "Repair Cycle Generation 2" in text_status_result.stdout
    assert "Candidate 验证状态 已阻塞" in text_status_result.stdout
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

def test_status_distinguishes_stale_acceptance_generation_from_repair_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    run = state["run_acceptance"] = {
        "phase": "pending",
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "repair_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 0,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    for _ in range(3):
        invalidate_run_acceptance(state)
    run.update(
        {
            "phase": "repairing",
            "acceptance_artifact": _repair_artifact(),
            "repair_request": {"repair_source": "acceptance"},
        }
    )
    state["status"] = "run_acceptance_pending"
    states.save_run(str(state["run_id"]), state)

    class BlockedRepairAgents:
        def develop(self, _request: dict[str, Any]) -> HumanBlockerResult:
            return HumanBlockerResult(
                "blocked-repair-developer", (_BLOCKED_EVIDENCE,)
            )

        def review(self, _request: dict[str, Any]) -> ReviewResult:
            raise AssertionError("blocked Development must stop before review")

        def publication(self, _request: dict[str, Any]) -> dict[str, str]:
            raise AssertionError("blocked Development must stop before publication")

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRepairAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    job = blocked["run_acceptance"]["repair_job"]
    assert blocked["run_acceptance"]["acceptance_generation"] == 4
    assert job["acceptance_generation"] == 4
    assert job["repair_generation"] == 2

    json_status = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"]), "--json"
    )
    text_status = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"])
    )
    assert json_status.returncode == text_status.returncode == 0
    repair_status = stdout_json(json_status)["run_repair"]
    assert repair_status["acceptance_generation"] == 4
    assert repair_status["repair_cycle_generation"] == 2
    assert repair_status["candidate_validation_phase"] == "blocked"
    assert "Run Acceptance Generation 4" in text_status.stdout
    assert "Repair Cycle Generation 2" in text_status.stdout
    assert "Candidate 验证状态 已阻塞" in text_status.stdout

@pytest.mark.parametrize(
    ("job_phase", "run_status"),
    [
        ("publishing", "active"),
        ("waiting_checks", "waiting_external"),
        ("waiting_merge", "waiting_external"),
    ],
)
def test_status_keeps_passed_candidate_validation_separate_from_delivery_phase(
    git_repo: Path, job_phase: str, run_status: str
) -> None:
    state, states, git = _completed_run(git_repo)
    candidate_sha = git.resolve(str(state["run_branch"]))
    state["status"] = run_status
    state["run_acceptance"] = {
        "phase": "repairing",
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 4,
        "repair_cycle": {
            "generation": 2,
            "status": "active",
            "code_modification_attempts": 1,
            "validation_attempts": 1,
        },
        "repair_job": {
            "phase": job_phase,
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_mode": "squash",
            "candidate_sha": candidate_sha,
            "acceptance_record": {
                "reviewed_candidate_sha": candidate_sha,
                "artifact": _passing_artifact(),
            },
        },
    }
    states.save_run(str(state["run_id"]), state)

    json_status = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"]), "--json"
    )
    text_status = run_cli(
        git_repo, git_repo / "github.json", "status", str(state["run_id"])
    )

    assert json_status.returncode == text_status.returncode == 0
    repair_status = stdout_json(json_status)["run_repair"]
    assert repair_status["phase"] == job_phase
    assert repair_status["candidate_validation_status"] == "pass"
    assert repair_status["candidate_validation_phase"] == "pass"
    assert "Candidate 验证状态 已通过" in text_status.stdout

def test_status_binds_candidate_verdict_to_the_candidate_being_reviewed(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class ConsecutiveCandidateAgents(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [
                _repair_artifact(),
                _candidate_finding_artifact(),
                _passing_artifact(),
            ]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            candidate_number = len(self.development_requests)
            (checkout / "run-repair.txt").write_text(
                f"repaired-{candidate_number}\n", encoding="utf-8"
            )
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary=f"Produced Candidate {candidate_number}.",
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            if len(self.review_requests) == 3:
                json_status = run_cli(
                    git_repo, fixture, "status", str(state["run_id"]), "--json"
                )
                text_status = run_cli(
                    git_repo, fixture, "status", str(state["run_id"])
                )
                assert json_status.returncode == text_status.returncode == 0
                repair_status = stdout_json(json_status)["run_repair"]
                assert repair_status["candidate_validation_status"] == "reviewing"
                assert "Candidate 验证状态 验收中" in text_status.stdout
            return ReviewResult(
                thread_id=f"run-reviewer-{len(self.review_requests)}",
                artifact=self._reviews.pop(0),
            )

        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            json_status = run_cli(
                git_repo, fixture, "status", str(state["run_id"]), "--json"
            )
            text_status = run_cli(
                git_repo, fixture, "status", str(state["run_id"])
            )
            assert json_status.returncode == text_status.returncode == 0
            repair_status = stdout_json(json_status)["run_repair"]
            assert repair_status["candidate_validation_status"] == "pass"
            assert "Candidate 验证状态 已通过" in text_status.stdout
            return super().publication(request)

    agents = ConsecutiveCandidateAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert len(agents.development_requests) == 2
    assert len(agents.review_requests) == 3

def test_candidate_acceptance_history_keeps_every_candidate_across_cycles_and_reload(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class AuditAgents:
        cycle = 0
        candidate_count = 0
        review_count = 0

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.candidate_count += 1
            checkout = Path(str(request["checkout"]))
            (checkout / f"audit-{self.candidate_count}.txt").write_text(
                f"candidate {self.candidate_count}\n", encoding="utf-8"
            )
            return DevelopmentResult(
                f"audit-developer-{self.cycle}", "Created a distinct audit Candidate."
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            assert request["candidate_acceptance"] is True
            self.review_count += 1
            attempt = (self.review_count - 1) % 5
            return ReviewResult(
                f"audit-reviewer-{self.review_count}",
                _passing_artifact() if attempt == 4 else _candidate_finding_artifact(),
            )

        def publication(self, _request: dict[str, Any]) -> dict[str, str]:
            return {
                "commit_message": f"fix(run): publish audit cycle {self.cycle}",
                "pr_title": f"fix(run): publish audit cycle {self.cycle}",
                "pr_body_markdown": (
                    "## What Problem This Solves\n\nThe Run needs repair.\n\n"
                    "## Why This Change Was Made\n\nEach Candidate is immutable.\n\n"
                    "## User Impact\n\nThe repaired Run is auditable.\n\n"
                    "## Evidence\n\nCandidate Run Acceptance passed."
                ),
            }

    agents = AuditAgents()
    for cycle in range(4):
        agents.cycle = cycle + 1
        previous = state.get("run_acceptance")
        prior = previous if isinstance(previous, dict) else {}
        artifact = _repair_artifact()
        state["run_acceptance"] = {
            "phase": "repairing",
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_generation": int(prior.get("repair_generation", 0)),
            "modification_attempts": 0,
            "validation_attempts": 0,
            "reviewer_thread_ids": list(prior.get("reviewer_thread_ids", [])),
            "development_thread_history": list(
                prior.get("development_thread_history", [])
            ),
            "candidate_acceptance_history": list(
                prior.get("candidate_acceptance_history", [])
            ),
            "acceptance_artifact": artifact,
            "repair_request": {
                "repair_source": "acceptance",
                "acceptance_artifact": artifact,
            },
        }
        state["status"] = "run_acceptance_pending"
        state["terminal_kind"] = "run_repair_pending"
        state.pop("run_publication", None)
        states.save_run(str(state["run_id"]), state)

        promoted = RunAcceptanceEngine(
            git=git,
            states=states,
            agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))

        assert promoted["status"] == "run_publication_pending"
        reloaded_cycle = states.load_current_run(str(state["run_id"]))
        assert reloaded_cycle is not None
        state = reloaded_cycle
        assert len(state["run_acceptance"]["candidate_acceptance_history"]) == (
            cycle + 1
            ) * 5

    history = state["run_acceptance"]["candidate_acceptance_history"]
    assert len(history) == 20
    assert len({entry["candidate_sha"] for entry in history}) == 20
    assert history[0]["reviewer_thread_id"] == "audit-reviewer-1"
    assert history[-1]["reviewer_thread_id"] == "audit-reviewer-20"
    assert all("acceptance_record" not in item for item in history)
    assert all("artifact" not in item for item in history)
    assert history[-1]["ticket_completion_records_fingerprint"] == canonical_fingerprint(
        ticket_completion_records(state)
    )
    assert require_candidate_acceptance_history(
        history, "run_acceptance.candidate_acceptance"
    ) == history
    states.save_run(str(state["run_id"]), state)
    reloaded = states.load_current_run(str(state["run_id"]))
    assert reloaded is not None
    assert reloaded["run_acceptance"]["candidate_acceptance_history"][0] == history[0]

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
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "default_base_sha": git.resolve("main"),
        "candidate_sha": candidate_sha,
        "base_sha": candidate_sha,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance",
        "repair_mode": "squash",
        "candidate_acceptance_history": [],
    }

    engine.candidate_acceptance.record(job, "candidate-reviewer", artifact)

    assert job["candidate_acceptance_history"][-1]["outcome"] == expected_outcome
    state["run_acceptance"] = {
        "phase": "repairing",
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "candidate_acceptance_history": [],
        "repair_job": job,
    }
    require_current_run_state(state)

@pytest.mark.parametrize(
    ("changed_field", "changed_value", "expected"),
    [
        (None, None, None),
        ("head_sha", "foreign-head", "change_pr_head_changed_externally"),
        ("base_branch", "foreign-base", "change_pr_base_changed_externally"),
        ("base_sha", "foreign-base", "change_pr_base_changed_externally"),
        ("integrated_sha", "foreign-merge", "change_pr_integrated_sha_changed_externally"),
        ("integrated_tree", "foreign-tree", "change_pr_integrated_tree_changed_externally"),
        ("integrated_parents", [], "change_pr_integrated_parents_changed_externally"),
    ],
)
def test_integrated_revalidation_pr_exemption_requires_exact_live_facts(
    git_repo: Path,
    changed_field: str | None,
    changed_value: object,
    expected: str | None,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("main")
    tree = git.resolve(f"{base}^{{tree}}")

    def commit_tree(message: str, *parents: str) -> str:
        command = ["git", "commit-tree", tree]
        for parent in parents:
            command.extend(("-p", parent))
        return subprocess.run(
            command,
            cwd=git_repo,
            input=f"{message}\n",
            text=True,
            check=True,
            capture_output=True,
        ).stdout.strip()

    default = commit_tree("default", base)
    candidate = commit_tree("candidate", base, default)
    integrated = commit_tree("integrated", base, candidate)
    state = {"run_branch": "agent-run/run-1"}
    job = {
        "pr_number": 7,
        "integrated_sha": integrated,
        "integrated_revalidation_merge": {
            "base_sha": base,
            "default_base_sha": default,
            "candidate_sha": candidate,
            "publication_sha": candidate,
        },
    }
    live: dict[str, object] = {
        "state": "MERGED",
        "head_sha": candidate,
        "base_branch": state["run_branch"],
        "base_sha": integrated,
        "integrated_sha": integrated,
        "head_tree": tree,
        "integrated_tree": tree,
        "integrated_parents": [base, candidate],
    }
    canonical_live = dict(live)
    if changed_field is not None:
        live[changed_field] = changed_value

    class Reader:
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            assert pr_number == 7
            return dict(live)

    assert unknown_pr_mutation(
        state, "run-repair:1", job, Reader(), git
    ) == expected

    foreign_trees = {
        **canonical_live,
        "head_tree": "foreign-tree",
        "integrated_tree": "foreign-tree",
    }

    class ForeignTreeReader:
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            assert pr_number == 7
            return foreign_trees

    assert unknown_pr_mutation(
        state, "run-repair:1", job, ForeignTreeReader(), git
    ) == "change_pr_head_tree_changed_externally"
