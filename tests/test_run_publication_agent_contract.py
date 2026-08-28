from __future__ import annotations

import subprocess
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.run_repair_requests import RunRepairRequests

from run_acceptance_test_support import _completed_run, _passing_artifact
from test_cli import run_internal_stage, run_cli, stdout_json

from run_publication_test_support import (
    InterruptedRunPublisher,
    InvocationRunPublicationAgents,
    PassingRunReviewer,
    RunPublicationAgents,
    _accepted_run,
)


def _accepted_run_with_integration(
    git_repo: Path,
) -> tuple[dict[str, Any], Any, Any, FixtureGitHubPublisher]:
    state, states, git = _completed_run(git_repo)
    tree = git.resolve(f"{state['run_branch']}^{{tree}}")
    integrated = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            str(state["run_branch"]),
            "-m",
            "feat(ticket): integrate accepted ticket for publication contract",
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
    state["ticket_jobs"]["2"].update(
        {
            "integrated_sha": integrated,
            "deterministic_integration_record": {
                "source": "accepted",
                "base_sha": git.commit_parents(integrated)[0],
                "candidate_sha": integrated,
                "candidate_tree": tree,
                "publication_sha": integrated,
                "integrated_sha": integrated,
                "integrated_publication_sha": integrated,
                "integrated_tree": tree,
                "integrated_message": "feat(ticket): integrate accepted ticket for publication contract",
                "integrated_parents": [git.commit_parents(integrated)[0]],
                "window": 1,
                "final_ci_fix_used": False,
                "pr_number": 11,
                "required_checks_mode": "configured",
                "required_checks": "pass",
                "required_checks_evidence": {
                    "pr_number": 11,
                    "head_sha": integrated,
                    "result": "pass",
                    "checks": [{"name": "quality", "bucket": "pass"}],
                },
                "pr": {
                    "number": 11,
                    "state": "MERGED",
                    "head_sha": integrated,
                    "base_sha": git.commit_parents(integrated)[0],
                    "merge_commit_sha": integrated,
                },
                "effective_revision": state["ticket_graph"]["tickets"]["2"][
                    "content_revision"
                ],
                "acceptance_record": {
                    "acceptance_scope": "change_job",
                    "reviewed_base_sha": git.commit_parents(integrated)[0],
                    "reviewed_candidate_sha": integrated,
                    "reviewed_candidate_tree": tree,
                    "effective_revision": state["ticket_graph"]["tickets"]["2"][
                        "content_revision"
                    ],
                    "reviewer_thread_id": "ticket-reviewer",
                    "artifact": _passing_artifact(),
                },
            },
        }
    )
    completed_job = state["ticket_jobs"]["2"]
    completed_record = completed_job["deterministic_integration_record"]
    completed_acceptance = completed_record["acceptance_record"]
    review_artifact = {
        "reviewer_thread_id": completed_acceptance["reviewer_thread_id"],
        "candidate_sha": completed_acceptance["reviewed_candidate_sha"],
        "reviewed_base_sha": completed_acceptance["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": completed_acceptance["reviewed_base_sha"],
            "reviewed_candidate_sha": completed_acceptance[
                "reviewed_candidate_sha"
            ],
            "reviewed_candidate_tree": completed_acceptance[
                "reviewed_candidate_tree"
            ],
        },
        "artifact": completed_acceptance["artifact"],
    }
    completed_job["review_budget"] = {
        "window": 1,
        "development_attempts": 1,
        "reviewer_invocations": 1,
        "final_ci_fix_used": False,
        "review_artifacts": [review_artifact],
        "checkpoint_reason": None,
    }
    completed_job["review_budget_history"] = []
    completed_record["review_budget"] = deepcopy(completed_job["review_budget"])
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


def _bind_ticket_review_budget(
    job: dict[str, Any], record: dict[str, Any]
) -> None:
    acceptance = record["acceptance_record"]
    review_artifact = {
        "reviewer_thread_id": acceptance["reviewer_thread_id"],
        "candidate_sha": acceptance["reviewed_candidate_sha"],
        "reviewed_base_sha": acceptance["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": acceptance["reviewed_base_sha"],
            "reviewed_candidate_sha": acceptance["reviewed_candidate_sha"],
            "reviewed_candidate_tree": acceptance["reviewed_candidate_tree"],
        },
        "artifact": acceptance["artifact"],
    }
    budget = {
        "window": 1,
        "development_attempts": 1,
        "reviewer_invocations": 1,
        "final_ci_fix_used": False,
        "review_artifacts": [review_artifact],
        "checkpoint_reason": None,
    }
    job["review_budget"] = deepcopy(budget)
    job["review_budget_history"] = []
    record["review_budget"] = deepcopy(budget)


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
    pending_attempt_id = interrupted["run_publication"]["pending_semantic_attempt"][
        "attempt_id"
    ]
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
    assert retried["run_publication"]["publication_attempts"] == 1
    assert (
        retried["run_publication"]["semantic_attempt_history"][-1]["attempt_id"]
        == pending_attempt_id
    )
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

def test_final_publication_discards_an_artifact_when_run_head_drifts(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)

    class DriftingRunPublication(RunPublicationAgents):
        def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = super().run_publication(request)
            tree = git.resolve(f"{state['run_branch']}^{{tree}}")
            drifted = subprocess.run(
                [
                    "git",
                    "commit-tree",
                    tree,
                    "-p",
                    str(state["run_branch"]),
                    "-m",
                    "test: drift run head during publication",
                ],
                cwd=git_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", f"refs/heads/{state['run_branch']}", drifted],
                cwd=git_repo,
                check=True,
            )
            return result

    stale = RunPublicationEngine(
        git=git,
        states=states,
        agents=DriftingRunPublication(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert "artifact" not in stale["run_publication"]

def test_final_publication_discards_an_artifact_when_default_base_advances(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    fixture = git_repo / "github.json"

    class DriftingDefaultPublication(RunPublicationAgents):
        def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = super().run_publication(request)
            (git_repo / "default-branch.txt").write_text("advanced\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "default-branch.txt"], cwd=git_repo, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "advance default during publication"],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["default_head_sha"] = git.resolve("main")
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return result

    stale = RunPublicationEngine(
        git=git,
        states=states,
        agents=DriftingDefaultPublication(),
        github=publisher,
        default_branch="main",
        default_head_sha=state["run_acceptance"]["acceptance_record"][
            "reviewed_default_base_sha"
        ],
        currentness_reader=FixtureGitHubReader(fixture),
    ).publish(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert "artifact" not in stale["run_publication"]
    assert "pr_number" not in stale["run_publication"]

def test_malformed_final_run_publication_is_reported_as_execution_failed_by_cli(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "malformed-run-publication.json"
    agents.write_text(
        json.dumps({"run_publications": [{"invalid": "publication"}]}),
        encoding="utf-8",
    )

    failed = run_internal_stage(
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
    invocation = persisted["active_agent_invocation"]
    assert invocation["role"] == "final_publication"
    assert invocation["status"] == "failed"

def test_final_publication_fixture_missing_thread_marks_invocation_failed(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "missing-run-publication-thread.json"
    agents.write_text(
        json.dumps({"run_publications": [{"no_thread": True}]}),
        encoding="utf-8",
    )

    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["active_agent_invocation"]["status"] == "failed"

def test_final_publication_fixture_repairs_malformed_output_in_same_thread(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "repair-run-publication.json"
    artifact = RunPublicationAgents().run_publication({})
    agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-publication-thread",
                        "invalid": "publication",
                    },
                    {
                        "expected_thread_id": "run-publication-thread",
                        "thread_id": "run-publication-thread",
                        **artifact,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    published = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert published.returncode == 0, published.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["reported_thread_id"] == "run-publication-thread"
    assert invocation["attempt_count"] == 2

def test_final_publication_fixture_records_a_fresh_thread_without_expectation(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "fresh-run-publication.json"
    agents.write_text(
        json.dumps({"run_publications": [RunPublicationAgents().run_publication({})]}),
        encoding="utf-8",
    )

    published = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert published.returncode == 0, published.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["requested_thread_id"] is None
    assert invocation["reported_thread_id"] == "fixture-run-publication"

def test_final_publication_fixture_resume_gets_a_fresh_repair_budget(
    git_repo: Path,
) -> None:
    state, states, _git, _publisher = _accepted_run(git_repo)
    failed_agents = git_repo / "failed-run-publication.json"
    failed_agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "failed-run-publication-thread",
                        "invalid": "publication",
                    },
                    {
                        "expected_thread_id": "failed-run-publication-thread",
                        "thread_id": "failed-run-publication-thread",
                        "invalid": "publication",
                    },
                    {
                        "expected_thread_id": "failed-run-publication-thread",
                        "thread_id": "failed-run-publication-thread",
                        "invalid": "publication",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2

    resumed_agents = git_repo / "resumed-run-publication.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "expected_thread_id": "failed-run-publication-thread",
                        "thread_id": "failed-run-publication-thread",
                        **RunPublicationAgents().run_publication({}),
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


def test_publish_run_cannot_bypass_exhausted_final_run_operation_retry(
    git_repo: Path,
) -> None:
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
        json.dumps({"run_publications": [RunPublicationAgents().run_publication({})]}),
        encoding="utf-8",
    )

    resumed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "publication_pending"
    recovered = states.load_run(str(state["run_id"]))
    assert recovered is not None
    assert recovered["run_acceptance"]["acceptance_record"] == acceptance
    assert recovered["run_publication"]["publication_attempts"] == 1
    retry = recovered["run_publication"]["publication_operation_retry"]
    assert retry["attempts"] == retry["limit"]
    assert recovered["run_publication"]["artifact"]


def test_remote_checks_cannot_reopen_exhausted_publication_operation_retry(
    git_repo: Path,
) -> None:
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
    assert waiting["status"] == "publication_pending"
    assert waiting["terminal_kind"] == "publication_pending"

def test_final_run_publication_receives_only_role_required_facts(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run_with_integration(git_repo)
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = {
        "source": "accepted",
        "base_sha": "run-base",
        "candidate_sha": "candidate",
        "candidate_tree": "candidate-tree",
        "publication_sha": "published",
        "integrated_sha": state["ticket_jobs"]["2"]["integrated_sha"],
        "integrated_publication_sha": "published",
        "integrated_tree": "candidate-tree",
        "integrated_message": "feat: integrated ticket",
        "integrated_parents": ["run-base"],
        "effective_revision": "test-revision",
        "pr_number": 11,
        "window": 1,
        "final_ci_fix_used": False,
        "required_checks_mode": "configured",
        "required_checks": "pass",
        "required_checks_evidence": {
            "pr_number": 11,
            "head_sha": "published",
            "result": "pass",
            "checks": [{"name": "quality", "bucket": "pass"}],
        },
        "pr": {
            "number": 11,
            "state": "MERGED",
            "head_sha": "published",
            "base_sha": "run-base",
            "merge_commit_sha": state["ticket_jobs"]["2"]["integrated_sha"],
        },
        "acceptance_record": {
            "acceptance_scope": "change_job",
            "reviewed_base_sha": "run-base",
            "reviewed_candidate_sha": "candidate",
            "reviewed_candidate_tree": "candidate-tree",
            "effective_revision": "test-revision",
            "reviewer_thread_id": "ticket-reviewer",
            "artifact": _passing_artifact(),
        },
        "sentinel": "reviewer-only integration evidence",
    }
    _bind_ticket_review_budget(
        state["ticket_jobs"]["2"],
        state["ticket_jobs"]["2"]["deterministic_integration_record"],
    )
    states.save_run(str(state["run_id"]), state)
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
    assert callable(request.pop("_invocation_event"))
    assert callable(request.pop("_currentness_check"))
    assert set(request) == {
        "acceptance_artifact",
        "checkout",
        "parent_issue_url",
        "repository",
    }
    assert request["parent_issue_url"].endswith("/issues/1")
    assert request["repository"] == "example/project"


def test_publication_prompts_and_run_repair_requests_keep_integration_records_with_reviewers(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run_with_integration(git_repo)
    fallback_ticket_records = [
        {"sentinel": "fallback-ticket-record-publication-leak"}
    ]
    previous_review_identity = {
        "reviewed_base_sha": "previous-review-identity-publication-leak",
        "reviewed_candidate_sha": "previous-candidate",
    }
    integration_records = [
        {
            "ticket_number": 2,
            "integration_record": {
                "source": "accepted",
                "base_sha": "run-base",
                "candidate_sha": "candidate",
                "candidate_tree": "candidate-tree",
                "publication_sha": "published",
                "integrated_sha": state["ticket_jobs"]["2"]["integrated_sha"],
                "integrated_publication_sha": "published",
                "integrated_tree": "candidate-tree",
                "integrated_message": "feat: integrated ticket",
                "integrated_parents": ["run-base"],
                "effective_revision": "test-revision",
                "pr_number": 11,
                "window": 1,
                "final_ci_fix_used": False,
                "required_checks_mode": "configured",
                "required_checks": "pass",
                "required_checks_evidence": {
                    "pr_number": 11,
                    "head_sha": "published",
                    "result": "pass",
                    "checks": [{"name": "quality", "bucket": "pass"}],
                },
                "pr": {
                    "number": 11,
                    "state": "MERGED",
                    "head_sha": "published",
                    "base_sha": "run-base",
                    "merge_commit_sha": state["ticket_jobs"]["2"]["integrated_sha"],
                },
                "acceptance_record": {
                    "acceptance_scope": "change_job",
                    "reviewed_base_sha": "run-base",
                    "reviewed_candidate_sha": "candidate",
                    "reviewed_candidate_tree": "candidate-tree",
                    "effective_revision": "test-revision",
                    "reviewer_thread_id": "ticket-reviewer",
                    "artifact": _passing_artifact(),
                },
                "sentinel": "reviewer-only integration evidence",
            },
        }
    ]
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = (
        integration_records[0]["integration_record"]
    )
    _bind_ticket_review_budget(
        state["ticket_jobs"]["2"],
        integration_records[0]["integration_record"],
    )
    states.save_run(str(state["run_id"]), state)

    repair_requests = RunRepairRequests(
        git=SimpleNamespace(
            expected_merge_tree=lambda **_kwargs: "expected-merge-tree"
        ),
        github=publisher,
    )
    repair_job = {
        "base_sha": "run-base",
        "candidate_sha": "repair-candidate",
        "default_base_sha": "default-base",
        "acceptance_artifact": {},
        "development_thread_id": "repair-developer",
        "publication_attempts": 0,
    }
    publication_request = repair_requests.publication(
        state, repair_job, git_repo / "checkout"
    )
    review_request = repair_requests.review(
        state, repair_job, git_repo / "validation"
    )

    assert "ticket_integration_records" not in publication_request
    reviewer_records = review_request["ticket_integration_records"]
    assert len(reviewer_records) == 1
    assert reviewer_records[0]["ticket_number"] == 2
    assert reviewer_records[0]["integration_record"] == integration_records[0][
        "integration_record"
    ]

    final_publication_prompt = CodexCliBackend._run_publication_prompt(
        {
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "acceptance_artifact": {},
            "fallback_ticket_records": fallback_ticket_records,
            "previous_review_identity": previous_review_identity,
            "ticket_integration_records": integration_records,
        }
    )
    run_repair_publication_prompt = CodexCliBackend._publication_prompt(
        {
            "acceptance_scope": "run",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "acceptance_artifact": {},
            "fallback_ticket_records": fallback_ticket_records,
            "previous_review_identity": previous_review_identity,
            "ticket_integration_records": integration_records,
        }
    )
    reviewer_prompt = CodexCliBackend._review_prompt(
        {
            "acceptance_scope": "run",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "ticket_integration_records": integration_records,
            "fallback_ticket_records": fallback_ticket_records,
            "previous_acceptance_artifact": {
                "sentinel": "previous-review-artifact-reviewer-visible"
            },
            "previous_review_identity": previous_review_identity,
            "current_review_identity": {
                "default_base_sha": "default-base",
                "run_head_sha": "run-head",
                "expected_merge_tree": "expected-merge-tree",
            },
        }
    )

    for prompt in (final_publication_prompt, run_repair_publication_prompt):
        assert "ticket_integration_records" not in prompt
        assert "reviewer-only integration evidence" not in prompt
        assert "fallback-ticket-record-publication-leak" not in prompt
        assert "previous-review-identity-publication-leak" not in prompt
    assert "ticket_integration_records" in reviewer_prompt
    assert "reviewer-only integration evidence" in reviewer_prompt
    assert "fallback-ticket-record-publication-leak" in reviewer_prompt
    assert "previous-review-identity-publication-leak" in reviewer_prompt

def test_final_run_publication_invocation_binds_accepted_run_identity(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = InvocationRunPublicationAgents()

    completed = RunPublicationEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    invocation = completed["active_agent_invocation"]
    run = completed["run_acceptance"]
    acceptance = run["acceptance_record"]
    request = agents.requests[0]
    assert invocation["status"] == "completed"
    assert invocation["work_subject"] == f"run-publication:{state['run_id']}"
    assert invocation["generation"] == run["validation_attempts"]
    assert invocation["input_fingerprint"] == canonical_fingerprint(request)
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
    assert completed["agent_invocation_history"][-1] == invocation


def test_retries_publication_operation_without_a_fresh_narrative_agent(
    git_repo: Path,
) -> None:
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
    assert len(agents.requests) == 1
    assert retried["run_publication"]["publication_attempts"] == 1
    assert retried["run_publication"]["publication_operation_retry"]["attempts"] == 1

def test_closed_final_pr_is_replaced_after_fresh_acceptance(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["crash_after_ensure_run_pr_once"] = True
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    published = engine.publish(str(state["run_id"]))
    prior_attempt = published["run_publication"]["semantic_attempt_history"][-1]
    assert prior_attempt["publication_operation_retry"] == {
        "attempts": 1,
        "limit": 5,
    }
    publisher.data["delivery"]["pull_requests"][0]["state"] = "CLOSED"
    published["run_publication"]["phase"] = "publishing"
    states.save_run(str(state["run_id"]), published)

    stale = engine.publish(str(state["run_id"]))

    assert stale["status"] == "run_acceptance_pending"
    assert stale["run_acceptance"]["phase"] == "pending"
    assert len(publisher.data["delivery"]["pull_requests"]) == 1

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
    ).accept(str(state["run_id"]))
    recovered = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert fresh["run_acceptance"]["phase"] == "accepted"
    attempts = recovered["run_publication"]["semantic_attempt_history"]
    assert [attempt["ordinal"] for attempt in attempts] == [1, 2]
    assert attempts[0]["attempt_id"] == prior_attempt["attempt_id"]
    assert attempts[0]["publication_operation_retry"] == {
        "attempts": 1,
        "limit": 5,
    }
    assert attempts[1]["attempt_id"] != attempts[0]["attempt_id"]
    assert (
        recovered["run_publication"]["pr_number"]
        != published["run_publication"]["pr_number"]
    )
    assert [pull["state"] for pull in publisher.data["delivery"]["pull_requests"]] == [
        "CLOSED",
        "OPEN",
    ]

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
