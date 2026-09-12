from __future__ import annotations

from copy import deepcopy
import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.change_currentness import candidate_or_acceptance_is_inconsistent
from agent_run.controller import Controller
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import invalidate_stale_run_repair
from agent_run.run_thread_identity import prior_thread_identities

from conftest import seed_idle_control, seed_run, write_fixture
from agent_run.task_control import TaskControlStore, TaskKey
from test_cli import failed_invocation, run_internal_stage, run_cli, stdout_json

from run_acceptance_test_support import (
    ScriptedRunAgents,
    _canonical_run_budget,
    _completed_run,
    _human_artifact,
    _passing_artifact,
    _repair_artifact,
)


def test_prior_rejection_is_history_not_new_candidate_authority() -> None:
    artifact = {"checks": {"e2e": {"status": "fail"}}}
    job = {
        "phase": "reviewing",
        "candidate_sha": "candidate-3",
        "last_review_candidate_sha": "candidate-2",
        "acceptance_record": {
            "reviewed_candidate_sha": "candidate-2",
            "artifact": artifact,
        },
        "review_budget": {
            "review_artifacts": [
                {"candidate_sha": "candidate-2", "artifact": artifact}
            ]
        },
    }

    assert candidate_or_acceptance_is_inconsistent(job) is False
    job["review_budget"]["review_artifacts"].append(
        {"candidate_sha": "candidate-3", "artifact": {"checks": {}}}
    )
    assert candidate_or_acceptance_is_inconsistent(job) is True
    job["review_budget"]["review_artifacts"].pop()
    job["acceptance_record"]["artifact"] = {"checks": {}}
    assert candidate_or_acceptance_is_inconsistent(job) is True


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
    started = seed_run(git_repo, fixture, "1")
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
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_history": [],
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_generation": 1,
            "repair_branch": repair_branch,
            "base_sha": "stale-run-base",
            "parent_revision": state["parent"]["revision"],
            "ticket_graph_revision": state["ticket_graph"]["revision"],
            "ticket_completion_records": [],
            "repair_source": "acceptance",
            "repair_mode": "squash",
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


def test_public_resume_retires_stale_run_repair_before_preserving_dirty_checkout(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    seed_idle_control(
        TaskControlStore(git_repo / ".agent-run"),
        TaskKey(git_repo, "example/project", 1),
        str(state["run_id"]),
        state_dir=states.root,
    )
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    run_id = str(state["run_id"])
    ticket_branch = f"agent-run/{run_id}/ticket-2"
    state["ticket_jobs"]["2"]["ticket_branch"] = ticket_branch
    git.ensure_run_branch(
        ticket_branch, str(state["ticket_jobs"]["2"]["integrated_sha"])
    )
    publisher.ensure_run_repair_branch(
        branch=ticket_branch, base_branch=str(state["run_branch"])
    )
    repair_branch = f"agent-run-repair/{run_id}/1"
    publisher.ensure_run_repair_branch(
        branch=repair_branch, base_branch=str(state["run_branch"])
    )
    checkout = states.root / "worktrees" / run_id / "run-repair"
    git.prepare_ticket_checkout(
        branch=repair_branch,
        base_sha=str(state["run_branch"]),
        checkout=checkout,
    )
    tracked = checkout / "pyproject.toml"
    tracked.write_text(
        tracked.read_text(encoding="utf-8") + "\n# preserved repair\n",
        encoding="utf-8",
    )
    untracked = checkout / "run-repair.txt"
    untracked.write_text("unfinished repair\n", encoding="utf-8")
    invocation = failed_invocation(
        work_subject=f"run-repair:{run_id}",
        role="development",
        phase="developing",
    )
    attempt = deepcopy(invocation["semantic_attempt"])
    budget = _canonical_run_budget()
    budget["development_attempts"] = 1
    repair_job = {
        "phase": "developing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": budget,
        "review_budget_history": [],
        "repair_generation": 1,
        "repair_branch": repair_branch,
        "base_sha": "stale-run-base",
        "published_sha": git.checkout_head(checkout),
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": [],
        "repair_source": "acceptance",
        "repair_mode": "squash",
        "acceptance_artifact": _repair_artifact(),
        "development_thread_id": "repair-old",
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "modification_attempts": 0,
        "pending_attempt": 1,
        "pending_attempt_kind": "ordinary",
        "managed_checkout_head": git.checkout_head(checkout),
        "pending_semantic_attempt": attempt,
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
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "repair_job": repair_job,
    }
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "diagnostics": [
                {
                    "code": "agent_invocation_failed",
                    "message": "fixture failure",
                }
            ],
            "active_agent_invocation": invocation,
        }
    )
    states.save_run(run_id, state)
    delivery_before = deepcopy(
        json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    )

    held = run_cli(git_repo, fixture, "run", "1")
    assert held.returncode == 2
    assert stdout_json(held)["status"] == "execution_failed"
    assert states.load_run(run_id) == state
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == (
        delivery_before
    )

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
    )

    assert resumed.returncode == 0, resumed.stderr
    output = stdout_json(resumed)
    assert output["status"] == "run_acceptance_pending"
    persisted = states.load_run(run_id)
    assert persisted is not None
    assert persisted["run_acceptance"]["phase"] == "pending"
    assert "repair_job" not in persisted["run_acceptance"]
    assert "repair_request" not in persisted["run_acceptance"]
    assert persisted["active_agent_invocation"] is None
    resume_event = persisted["resume_audit"]["history"][-1]
    assert resume_event["semantic_attempt_id"] == attempt["attempt_id"]
    assert resume_event["human_response_supplied"] is False
    assert resume_event["new_thread"] is False
    assert resume_event["successor_invocation_started_at"] is None
    retired = persisted["retired_semantic_attempt_owners"][-1]
    assert retired["work_subject"] == f"run-repair:{run_id}"
    assert retired["semantic_attempt_history"][-1]["attempt_id"] == attempt[
        "attempt_id"
    ]
    assert retired["semantic_attempt_history"][-1]["outcome"] == (
        "currentness_invalidated"
    )
    assert tracked.read_text(encoding="utf-8").endswith("# preserved repair\n")
    assert untracked.read_text(encoding="utf-8") == "unfinished repair\n"
    cleanup = output["delivery_cleanup"]
    assert cleanup["status"] == "cleanup_pending"
    assert cleanup["items"][0]["checkout"] == str(checkout)
    assert "agent-run run 1" in cleanup["items"][0]["recovery_action"]
    assert "agent-run run 1" in output["next_action"]
    delivery_after_resume = json.loads(fixture.read_text(encoding="utf-8"))[
        "delivery"
    ]
    expected_delivery = deepcopy(delivery_before)
    expected_delivery["published_branches"].pop(ticket_branch)
    expected_delivery["mutations"].append(
        {"action": "delete_managed_branch", "branch": ticket_branch}
    )
    assert delivery_after_resume == expected_delivery

    no_agents = git_repo / "no-agents.json"
    no_agents.write_text("{}", encoding="utf-8")
    still_dirty = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(no_agents),
    )
    assert still_dirty.returncode == 0, still_dirty.stdout
    dirty_state = states.load_run(run_id)
    assert dirty_state is not None
    assert dirty_state["run_acceptance"]["phase"] == "pending"
    assert dirty_state["run_acceptance"]["acceptance_generation"] == 2
    assert dirty_state["retired_semantic_attempt_owners"] == persisted[
        "retired_semantic_attempt_owners"
    ]
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == (
        delivery_after_resume
    )
    assert tracked.is_file()
    assert untracked.is_file()

    subprocess.run(
        ["git", "checkout", "--", "pyproject.toml"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    untracked.unlink()
    fresh_agents = git_repo / "fresh-run-agents.json"
    fresh_agents.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "thread_id": "fresh-run-reviewer",
                        "artifact": _human_artifact(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(fresh_agents),
    )

    assert recovered.returncode == 2, recovered.stdout
    recovered_state = states.load_run(run_id)
    assert recovered_state is not None
    assert recovered_state["status"] == "ready_for_human"
    assert recovered_state["run_acceptance"]["phase"] == "ready_for_human"
    assert recovered_state["run_acceptance"]["acceptance_generation"] == 2
    assert recovered_state["delivery_cleanup"]["status"] == "completed"
    assert not checkout.exists()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{repair_branch}"],
        cwd=git_repo,
        check=False,
    ).returncode == 1
    recovered_delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert repair_branch not in recovered_delivery["published_branches"]
    assert [
        mutation
        for mutation in recovered_delivery["mutations"]
        if mutation.get("action") == "delete_managed_branch"
        and mutation.get("branch") == repair_branch
    ] == [{"action": "delete_managed_branch", "branch": repair_branch}]
    assert recovered_state["retired_semantic_attempt_owners"] == persisted[
        "retired_semantic_attempt_owners"
    ]

def test_run_repair_preserves_dirty_inflight_development_after_parent_drift(
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
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    assert (repair_checkout / "run-repair.txt").is_file()
    cleanup = stale["delivery_cleanup"]
    assert cleanup["status"] == "cleanup_pending"
    assert cleanup["last_error"] == "untracked files"
    retired = stale["retired_semantic_attempt_owners"][-1]
    assert retired["work_subject"] == f"run-repair:{state['run_id']}"
    assert retired["semantic_attempt_history"][-1]["outcome"] == (
        "currentness_invalidated"
    )

def test_run_repair_preserves_dirty_inflight_development_after_final_pr_drift(
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
                pr_number, expected_head_sha=git.resolve(str(state["run_branch"]))
            ),
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
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    assert (repair_checkout / "run-repair.txt").is_file()
    cleanup = stale["delivery_cleanup"]
    assert cleanup["status"] == "cleanup_pending"
    assert cleanup["last_error"] == "untracked files"
    retired = stale["retired_semantic_attempt_owners"][-1]
    assert retired["work_subject"] == f"run-repair:{state['run_id']}"
    assert retired["semantic_attempt_history"][-1]["outcome"] == (
        "currentness_invalidated"
    )

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

        def required_check_evidence(
            self, pr_number: int, *, expected_head_sha: str | None = None
        ) -> dict[str, Any]:
            if self.evidence_error is not None:
                raised = self.evidence_error
                self.evidence_error = None
                raise raised
            return super().required_check_evidence(
                pr_number, expected_head_sha=expected_head_sha
            )

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
    evidence = FixtureGitHubPublisher(fixture, git).required_check_evidence(
        pr_number, expected_head_sha=git.resolve(str(state["run_branch"]))
    )
    state["run_publication"] = {"phase": "ready_for_approval", "pr_number": pr_number}
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
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": ["run-reviewer"],
        "repair_job": {
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "development_thread_id": "repair-developer",
            "development_thread_history": ["repair-developer-old"],
            "reviewer_thread_ids": ["repair-reviewer"],
        },
    }
    state["ticket_jobs"]["2"]["development_thread_history"] = [
        "ticket-developer-old"
    ]

    invalidate_stale_run_repair(state)

    assert prior_thread_identities(state, run) >= {
        "run-reviewer",
        "repair-developer",
        "repair-developer-old",
        "repair-reviewer",
        "ticket-developer",
        "ticket-developer-old",
        "ticket-reviewer",
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
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
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
