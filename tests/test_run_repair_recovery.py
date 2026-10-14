from __future__ import annotations

from support.repairing_run import prepare_run_repair

from support.run_artifacts import _candidate_finding_artifact, _passing_artifact
from support.run_scenarios import _completed_run
from support.final_publication import final_publication_context

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_repair_currentness import RunRepairObservationPending
from agent_run.state_errors import MissingRunMaterialsError


from support.run_agents import ScriptedRunAgents

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
    prepare_run_repair(
        state, states, repair_request={
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(
                final_pr, expected_head_sha=git.resolve(str(state["run_branch"]))
            ),
        },
    )

    class TriggerDriftingPublisher(FixtureGitHubPublisher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._drifted = False

        def publish_branch(self, branch: str, head_sha: str, *, expected_remote_sha: str) -> Any:
            result = super().publish_branch(branch, head_sha, expected_remote_sha=expected_remote_sha)
            self._drifted = True
            return result

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
    prepare_run_repair(
        state, states, repair_request={
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(
                final_pr, expected_head_sha=git.resolve(str(state["run_branch"]))
            ),
        },
    )

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
    assert "candidate_sha" not in job
    persisted = states.load_current_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["status"] == "waiting_external"
    assert persisted["run_acceptance"] == run
    assert persisted["supervision_window"]["kind"] == "github_convergence"
    assert persisted["diagnostics"][0]["code"] == "github_pull_request_observation_pending"

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
    states, git, state = final_publication_context(git_repo, approved=False)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    final_pr = state["run_publication"]["pr_number"]
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
    state = failing.states.load_current_run(str(state["run_id"]))
    assert state is not None
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
    prepare_run_repair(
        state, states, repair_request={
            "repair_source": "required_checks",
            "ci_evidence": publisher.required_check_evidence(
                final_pr, expected_head_sha=git.resolve(str(state["run_branch"]))
            ),
        },
    )

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

def test_run_repair_review_default_drift_revalidates_in_same_cycle(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    prepare_run_repair(
        state, states, repair_request={"repair_source": "acceptance"},
    )

    class DriftingReviewAgents(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_passing_artifact(), _passing_artifact()]
            self._drifted = False

        def review(self, request: dict[str, Any]) -> ReviewResult:
            if not self._drifted:
                self._drifted = True
                (git_repo / "default-drift-before-repair-write.txt").write_text(
                    "advanced\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "default-drift-before-repair-write.txt"],
                    cwd=git_repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "advance default before repair write"],
                    cwd=git_repo,
                    check=True,
                    capture_output=True,
                )
                data = json.loads(fixture.read_text(encoding="utf-8"))
                data["default_head_sha"] = git.resolve("main")
                fixture.write_text(json.dumps(data), encoding="utf-8")
                publisher.data["default_head_sha"] = git.resolve("main")
            return super().review(request)

    agents = DriftingReviewAgents()
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=publisher,
        default_head_sha=git.resolve("main"),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))
    assert result["status"] == "run_acceptance_pending"
    assert result["run_acceptance"]["repair_generation"] == 1
    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_head_sha=git.resolve("main"), currentness_reader=FixtureGitHubReader(fixture),
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
    prepare_run_repair(
        state, states, repair_request={"repair_source": "acceptance"},
    )

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

def test_run_repair_recovers_lost_run_branch_response_without_duplicate_worker(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    prepare_run_repair(state, states, repair_request={"repair_source": "acceptance"})
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["publish_branch_outcomes"] = ["lost_response"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    engine = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=FixtureGitHubPublisher(fixture, git),
    )
    paused = engine.accept(str(state["run_id"]))
    assert paused["status"] == "ready_for_human"
    job = paused["run_acceptance"]["repair_job"]
    assert job["blocked_reason"] == "run_branch_write_failed"
    intent = dict(job["run_branch_write_intent"])
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["published_branches"][state["run_branch"]] == intent["candidate_sha"]
    assert Path(job["repair_checkout"]).exists()

    Controller(FixtureGitHubReader(fixture), git, states).resume(str(state["run_id"]), explicit_resume=True)
    recovered = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    assert recovered["status"] == "run_publication_pending"
    assert len(agents.development_requests) == len(agents.review_requests) == 1
    assert "pr_number" not in job
    archived = recovered["run_acceptance"]["completed_repair_jobs"][-1]
    assert archived["integrated_sha"] == intent["candidate_sha"]
    assert git.resolve(str(state["run_branch"])) == intent["candidate_sha"]
    assert "pr_number" not in archived
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"] == []


def test_run_repair_rejects_foreign_run_branch_write_without_losing_candidate(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    prepare_run_repair(state, states, repair_request={"repair_source": "acceptance"})
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["publish_branch_outcomes"] = ["cas_race"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    assert result["status"] == "ready_for_human"
    job = result["run_acceptance"]["repair_job"]
    assert job["blocked_reason"] == "run_branch_write_failed"
    assert job["candidate_sha"] == job["run_branch_write_intent"]["candidate_sha"]
    assert git.resolve(str(state["run_branch"])) == job["base_sha"]
    assert Path(job["repair_checkout"]).exists()
    assert len(agents.development_requests) == len(agents.review_requests) == 1
    assert "pr_number" not in job
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["published_branches"][state["run_branch"]] == "foreign"
    assert delivery["pull_requests"] == []


# Final PR identity checks remain covered by the trigger-currentness cases
# above and test_run_publication_approval; the removed internal PR's recovery
# is replaced by exact saved Run Branch write identity and remote CAS tests.
@pytest.mark.parametrize("field", ["branch", "base_sha", "candidate_sha"])
def test_run_repair_recovery_rejects_foreign_write_identity_without_new_effects(
    git_repo: Path, field: str
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    prepare_run_repair(state, states, repair_request={"repair_source": "acceptance"})
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["publish_branch_outcomes"] = ["lost_response"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    paused = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    assert paused["status"] == "ready_for_human"
    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(str(state["run_id"]), explicit_resume=True)
    job = resumed["run_acceptance"]["repair_job"]
    job["run_branch_write_intent"][field] = "foreign"
    states.save_run(str(state["run_id"]), resumed)
    fixture_before = fixture.read_bytes()
    requests_before = (len(agents.development_requests), len(agents.review_requests))
    with pytest.raises(MissingRunMaterialsError, match="write intent differs from its reviewed candidate"):
        RunAcceptanceEngine(
            git=git, states=states, agents=agents, github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))
    assert (len(agents.development_requests), len(agents.review_requests)) == requests_before
    assert fixture.read_bytes() == fixture_before
