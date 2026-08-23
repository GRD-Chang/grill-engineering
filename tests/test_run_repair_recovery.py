from __future__ import annotations

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_repair_currentness import RunRepairObservationPending


from run_acceptance_test_support import (
    ScriptedRunAgents,
    _canonical_run_budget,
    _candidate_finding_artifact,
    _completed_run,
    _passing_artifact,
    _repair_artifact,
)

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
        state,
        "required_checks",
        {
            "ci_evidence": publisher.required_check_evidence(
                final_pr, expected_head_sha=git.resolve(str(state["run_branch"]))
            )
        },
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
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
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
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
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
