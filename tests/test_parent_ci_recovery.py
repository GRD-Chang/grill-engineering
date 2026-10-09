"""Parent CI recovery across the delivery and Run Repair boundaries."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest


from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.cli_presentation import _run_repair_status
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.inprocess_cli import invoke_cli_inprocess
from support.delivery_agents import PassAgents
from support.run_artifacts import _candidate_finding_artifact, _passing_artifact, _repair_artifact
from support.run_agents import ScriptedRunAgents
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run, _completed_run
from support.seeding import write_fixture


class ParentCIAgents(PassAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        if request.get("repair_source") == "required_checks":
            self.development_requests.append(request)
            (self.checkout / "ci-fix.txt").write_text(
                f"fixed {len(self.development_requests)}\n", encoding="utf-8"
            )
            return DevelopmentResult(
                thread_id=str(request.get("thread_id") or "parent-ci-developer"),
                summary="Fixed CI",
            )
        return super().develop(request)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        return super().review(request)


class RunCIAgents(ScriptedRunAgents):
    reviewer_offset = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        target = checkout / "run-repair.txt"
        prior = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(
            (prior + f"repair {len(self.development_requests)}\n") if prior else "repaired\n",
            encoding="utf-8",
        )
        return DevelopmentResult(
            thread_id=str(request.get("thread_id") or f"run-ci-developer-{len(self.development_requests)}"),
            summary="Fixed CI",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        assert (Path(str(request["checkout"])) / "run-repair.txt").is_file()
        return ReviewResult(
            thread_id=f"run-ci-reviewer-{self.reviewer_offset + len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )


def test_parent_only_ci_repair_reviews_final_candidate_and_keeps_pr(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    class UnknownFailurePublisher(FixtureGitHubPublisher):
        def required_check_evidence(self, pr_number: int, *, expected_head_sha: str | None = None) -> dict[str, Any]:
            return {**super().required_check_evidence(pr_number, expected_head_sha=expected_head_sha),
                    "repairability": "unknown", "log": "RAW_PLATFORM_FAILURE"}

    publisher = UnknownFailurePublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["pending", "fail", "pass"]
    publisher._save()
    agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")

    engine = ParentDeliveryEngine(
        git=git, states=states, github=publisher, agents=agents,
    )
    waiting = engine.deliver(run_id)
    assert waiting["parent_job"]["phase"] == "waiting_checks"
    assert waiting["parent_job"]["ci_recovery"]["developments_used"] == 0
    failed = engine.deliver(run_id)
    assert failed["parent_job"]["phase"] == "repairing"
    result = engine.deliver(run_id)

    job = result["parent_job"]
    assert result["status"] == "parent_approval_pending", (job["phase"], job.get("blocked_reason"))
    assert job["ci_recovery"]["window"] == 1
    assert job["ci_recovery"]["developments_used"] == 1
    assert job["ci_recovery"]["reviewer_invocations"] == 1
    assert len(agents.review_requests) == 2
    assert agents.review_requests[-1]["candidate_sha"] == job["candidate_sha"]
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    review_request = agents.review_requests[-1]
    assert review_request["work_source"] == "ci"
    assert review_request["ci_evidence"]["log"] == "RAW_PLATFORM_FAILURE"
    assert review_request["parent_acceptance_baseline"]["acceptance_scope"] == "parent_only"
    assert not engine.has_current_approval_grant(run_id)
    approved = engine.approve(run_id)
    assert approved["status"] == "completed"
    assert git.is_ancestor(job["published_sha"], git.resolve("main"))


def test_parent_only_published_source_rollback_pauses_without_repush(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["pending"]
    publisher._save()
    agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)

    waiting = engine.deliver(run_id)
    job = waiting["parent_job"]
    assert job["phase"] == "waiting_checks"
    published = job["published_sha"]
    previous = job["parent_integration"]["source_sha"]
    assert previous != published
    branch = str(job["parent_branch"])
    publisher.data["delivery"]["published_branches"][branch] = previous
    publisher.data["delivery"]["pull_requests"][0]["head_sha"] = previous
    publisher._save()
    reviews_before = len(agents.review_requests)

    paused = engine.deliver(run_id)
    assert paused["status"] == "blocked", (
        paused["parent_job"]["phase"], paused["diagnostics"]
    )
    assert paused["terminal_kind"] == "waiting_human"
    assert paused["parent_job"]["blocked_reason"] == "integration_history_rewritten"
    assert paused["parent_job"]["published_sha"] == published
    assert publisher.live_branch_heads(branch, "main")[0] == previous
    assert len(agents.review_requests) == reviews_before


def test_parent_only_published_source_append_reuses_pr(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["pending"]
    publisher._save()
    agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    waiting = engine.deliver(run_id)
    job = waiting["parent_job"]
    assert job["phase"] == "waiting_checks"
    original_pr = job["pr_number"]
    published = job["published_sha"]

    collaborator = tmp_path / "collaborator"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(collaborator), published],
        cwd=git_repo, check=True, capture_output=True,
    )
    try:
        (collaborator / "human.txt").write_text("human contribution\n", encoding="utf-8")
        subprocess.run(["git", "add", "human.txt"], cwd=collaborator, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Collaborator", "-c", "user.email=c@example.invalid",
             "commit", "-m", "append Parent contribution"],
            cwd=collaborator, check=True, capture_output=True,
        )
        appended = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=collaborator, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(collaborator)],
            cwd=git_repo, check=True, capture_output=True,
        )
    branch = str(job["parent_branch"])
    publisher.data["delivery"]["published_branches"][branch] = appended
    publisher.data["delivery"]["pull_requests"][0]["head_sha"] = appended
    publisher.data["delivery"]["required_checks"] = ["pass"]
    publisher.data["delivery"]["check_position"] = 0
    publisher._save()

    delivered = engine.deliver(run_id)
    final = delivered["parent_job"]
    assert delivered["status"] == "parent_approval_pending", (
        final["phase"], final.get("blocked_reason")
    )
    assert final["pr_number"] == original_pr
    assert git.is_ancestor(published, final["published_sha"])
    assert git.is_ancestor(appended, final["published_sha"])
    assert len(agents.review_requests) == 2
    assert agents.review_requests[-1]["candidate_sha"] == final["candidate_sha"]


def test_run_final_ci_repair_shares_budget_with_internal_pr(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    publisher.data["delivery"]["required_checks"] = ["fail", "pass", "pass"]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    failed = publication.publish(run_id)
    final_pr = failed["run_publication"]["pr_number"]
    agents = RunCIAgents()
    agents._reviews = [_passing_artifact()]

    repaired = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    ).accept(run_id)

    assert repaired["status"] == "run_publication_pending"
    assert len(agents.review_requests) == 1
    assert repaired["run_acceptance"]["ci_recovery"]["developments_used"] == 1
    assert repaired["run_acceptance"]["review_budget"]["development_attempts"] == 0
    assert repaired["run_acceptance"]["completed_repair_jobs"]
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr
    assert published["run_acceptance"]["ci_recovery"]["window"] == 1
    review_request = agents.review_requests[-1]
    assert review_request["work_source"] == "ci"
    assert review_request["parent_acceptance_baseline"]["ticket_completion_records"]
    assert not publication.has_current_approval_grant(run_id)
    approved = publication.approve(run_id)
    assert approved["status"] == "completed"


def test_run_second_failed_final_head_keeps_cumulative_allowance_and_same_pr(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    publisher.data["delivery"]["required_checks"] = [
        "fail", "pass", "fail", "pass", "pass",
    ]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    failed = publication.publish(run_id)
    final_pr = failed["run_publication"]["pr_number"]
    agents = RunCIAgents()
    agents._reviews = [_candidate_finding_artifact(), _passing_artifact(), _candidate_finding_artifact()]
    acceptance = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    )

    first_repair = acceptance.accept(run_id)
    assert first_repair["status"] == "run_publication_pending"
    assert first_repair["run_acceptance"]["ci_recovery"]["developments_used"] == 2
    assert first_repair["run_acceptance"]["completed_repair_jobs"]
    assert "repair_job" not in first_repair["run_acceptance"]
    assert StateStore(states.root).load_current_run(run_id) == first_repair
    failed_again = publication.publish(run_id)
    assert failed_again["status"] == "run_acceptance_pending"
    assert failed_again["run_publication"]["pr_number"] == final_pr
    assert failed_again["run_acceptance"]["ci_recovery"]["window"] == 1

    second_repair = acceptance.accept(run_id)
    assert second_repair["status"] == "ready_for_human"
    run = second_repair["run_acceptance"]
    assert run["ci_recovery"]["developments_used"] == 3
    assert run["ci_recovery"]["checkpoint_reason"] == "ci_development_budget_exhausted"
    assert run["review_budget"]["development_attempts"] == 0
    assert all(key not in run["repair_job"] for key in
               ("review_budget", "review_budget_history", "ci_recovery", "integration_resync"))
    assert StateStore(states.root).load_current_run(run_id) == second_repair
    ordinary = deepcopy(run["review_budget"])
    ticket_budget = deepcopy(second_repair["ticket_jobs"]["2"]["review_budget"])
    fixture = git_repo / "github.json"
    before = states.load_run(run_id)
    status = invoke_cli_inprocess(git_repo, fixture, "status", run_id,
                                  "--state-dir", str(states.root), "--json")
    assert status.returncode == 0, status.stderr
    visible = json.loads(status.stdout)
    assert visible["ci_recovery"]["developments_used"] == 3
    history = invoke_cli_inprocess(git_repo, fixture, "history", run_id,
                                   "--state-dir", str(states.root), "--json")
    assert history.returncode == 0, history.stderr
    assert "run-ci-reviewer" in history.stdout
    assert states.load_run(run_id) == before
    controller = Controller(FixtureGitHubReader(fixture), git, StateStore(states.root))
    resumed, _ = controller.resume(run_id, explicit_resume=True)
    renewed_ci = deepcopy(resumed["run_acceptance"]["ci_recovery"])
    assert renewed_ci["window"] == 2
    assert renewed_ci["developments_used"] == 0
    assert renewed_ci["history"][-1]["developments_used"] == 3
    assert resumed["run_acceptance"]["review_budget"] == ordinary
    assert resumed["ticket_jobs"]["2"]["review_budget"] == ticket_budget
    with pytest.raises(ValueError, match="no canonical recovery boundary"):
        controller.resume(run_id, explicit_resume=True)
    repeated, _ = controller.resume(run_id)
    assert repeated["run_acceptance"]["ci_recovery"] == renewed_ci
    agents._reviews = [_passing_artifact()]
    recovered = acceptance.accept(run_id)
    assert recovered["status"] == "run_publication_pending"
    assert recovered["run_acceptance"]["ci_recovery"]["developments_used"] == 1
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr
    assert len(agents.review_requests) == 4


def test_internal_repair_pr_ci_failure_uses_same_run_budget(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    publisher.data["delivery"]["required_checks"] = ["fail", "fail", "pass", "pass"]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    failed = publication.publish(run_id)
    final_pr = failed["run_publication"]["pr_number"]
    agents = RunCIAgents()
    agents._reviews = [_passing_artifact(), _passing_artifact()]
    acceptance = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    )

    repaired = acceptance.accept(run_id)

    assert repaired["status"] == "run_publication_pending"
    recovery = repaired["run_acceptance"]["ci_recovery"]
    assert recovery["window"] == 1
    assert recovery["developments_used"] == 2
    assert len(agents.development_requests) == 2
    assert len(agents.review_requests) == 2
    assert repaired["run_acceptance"]["review_budget"]["development_attempts"] == 0
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr


def test_run_ci_reviewer_exhausts_shared_development_allowance(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    publication.publish(run_id)
    agents = RunCIAgents()
    agents._reviews = [_candidate_finding_artifact()] * 3

    blocked = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    ).accept(run_id)

    assert blocked["status"] == "ready_for_human"
    run = blocked["run_acceptance"]
    assert run["blocked_reason"] == "ci_development_budget_exhausted"
    assert run["ci_recovery"]["window"] == 1
    assert run["ci_recovery"]["developments_used"] == 3
    assert run["review_budget"]["development_attempts"] == 0
    assert len(agents.review_requests) == 3
    original_ci = agents.development_requests[0]["ci_evidence"]
    assert all(request["work_source"] == "ci" and request["ci_evidence"] == original_ci
               for request in agents.development_requests)
    assert agents.development_requests[1]["repair_source"] == "acceptance"
    assert all(request["parent_acceptance_baseline"]["artifact"] == _passing_artifact()
               for request in agents.review_requests)
    assert run["repair_cycle"]["status"] == "budget_exhausted"
    assert run["repair_cycle"]["ended_reason"] == "ci_development_budget_exhausted"
    assert _run_repair_status(blocked)["code_modification_attempts"] == 0
    job = run["repair_job"]
    job["attempt_kind"] = "ordinary"
    run["review_budget"]["development_attempts"] = 1
    assert _run_repair_status(blocked)["code_modification_attempts"] == 1
    job["attempt_kind"] = "ci_repair"
    run["review_budget"]["development_attempts"] = 0

    resumed, _ = Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        run_id, explicit_resume=True,
    )
    assert resumed["run_acceptance"]["repair_cycle"]["status"] == "active"
    assert "ended_reason" not in resumed["run_acceptance"]["repair_cycle"]
    new_agents = RunCIAgents()
    new_agents.reviewer_offset = 3
    new_agents._reviews = [_passing_artifact()] * 3
    recovered = RunAcceptanceEngine(
        git=git, states=states, agents=new_agents, github=publisher,
    ).accept(run_id)
    assert recovered["status"] == "run_publication_pending", (
        recovered["run_acceptance"].get("blocked_reason"),
        recovered["run_acceptance"].get("repair_job", {}).get("blocked_reason"),
        recovered["run_acceptance"]["ci_recovery"],
        len(new_agents.development_requests), len(new_agents.review_requests),
    )
    assert recovered["run_acceptance"]["ci_recovery"]["window"] == 2


def test_parent_only_ci_resume_renews_only_exhausted_allowance(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()

    class RejectCIReview(ParentCIAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            return ReviewResult(
                thread_id=f"parent-reviewer-{len(self.review_requests)}",
                artifact=(
                    _passing_artifact()
                    if len(self.review_requests) == 1
                    else _candidate_finding_artifact()
                ),
            )

    agents = RejectCIReview(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(
        git=git, states=states, github=publisher, agents=agents,
    )
    engine.deliver(run_id)
    blocked = engine.deliver(run_id)
    assert blocked["status"] == "blocked"
    job = blocked["parent_job"]
    assert job["blocked_reason"] == "ci_development_budget_exhausted"
    assert job["ci_recovery"]["window"] == 1
    assert job["ci_recovery"]["developments_used"] == 3
    ordinary_budget = job["review_budget"].copy()
    original_ci = agents.development_requests[1]["ci_evidence"]
    assert all(request["ci_evidence"] == original_ci
               for request in agents.development_requests[1:])
    assert agents.development_requests[-1]["repair_source"] == "acceptance"
    assert all(request["parent_acceptance_baseline"]["artifact"] == _passing_artifact()
               for request in agents.review_requests[1:])

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
    )
    assert resumed["parent_job"]["ci_recovery"]["window"] == 2
    assert resumed["parent_job"]["review_budget"] == ordinary_budget
    passing_agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")
    recovered = ParentDeliveryEngine(
        git=git, states=states, github=publisher, agents=passing_agents,
    ).deliver(run_id)
    assert recovered["status"] == "parent_approval_pending"
    assert recovered["parent_job"]["pr_number"] == blocked["parent_job"]["pr_number"]
    assert recovered["parent_job"]["ci_recovery"]["window"] == 2


def test_parent_only_no_change_ci_investigation_resumes_same_scope(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()

    class InvestigationAgents(ParentCIAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("work_source") == "ci":
                self.development_requests.append(request)
                return DevelopmentResult(thread_id=str(request.get("thread_id")), summary="Investigated platform failure; no code changed")
            return super().develop(request)

    agents = InvestigationAgents(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    engine.deliver(run_id)
    blocked = engine.deliver(run_id)
    assert blocked["parent_job"]["blocked_reason"] == "ci_investigation_paused"
    assert blocked["parent_job"]["ci_recovery"]["developments_used"] == 1
    ordinary = blocked["parent_job"]["review_budget"]
    Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True, message="PLATFORM_RESTORED", new_thread=True,
    )
    resumed_agents = ParentCIAgents(agents.checkout)
    resumed_agents.review_requests = list(agents.review_requests)
    resumed_agents.review_count = agents.review_count
    result = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=resumed_agents).deliver(run_id)
    assert result["status"] == "parent_approval_pending"
    assert result["parent_job"]["ci_recovery"]["developments_used"] == 2
    assert result["parent_job"]["review_budget"] == ordinary
    request = resumed_agents.development_requests[-1]
    assert request["ci_evidence"] == agents.development_requests[-1]["ci_evidence"]
    assert request["work_source"] == "ci"
    assert "PLATFORM_RESTORED" in str(request["human_response_history"])


def test_run_no_change_ci_investigation_resumes_without_renewal(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    publisher.data["delivery"]["required_checks"] = ["fail", "pass", "pass"]
    publisher._save()
    publication = RunPublicationEngine(git=git, states=states, agents=RunPublicationAgents(), github=publisher,
                                       default_branch="main", default_head_sha=git.resolve("main"))
    publication.publish(run_id)

    class InvestigationAgents(RunCIAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            return DevelopmentResult(thread_id="run-ci-investigator", summary="Investigated failure; no code changed")

    agents = InvestigationAgents()
    blocked = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher).accept(run_id)
    job = blocked["run_acceptance"]["repair_job"]
    assert job["blocked_reason"] == "ci_investigation_paused"
    assert blocked["run_acceptance"]["ci_recovery"]["developments_used"] == 1
    ordinary = blocked["run_acceptance"]["review_budget"]
    assert "ci_recovery" not in job
    assert "review_budget" not in job
    Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        run_id, explicit_resume=True, message="PLATFORM_RESTORED", new_thread=True,
    )
    resumed_agents = RunCIAgents()
    resumed_agents._reviews = [_passing_artifact()]
    result = RunAcceptanceEngine(git=git, states=states, agents=resumed_agents, github=publisher).accept(run_id)
    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["ci_recovery"]["developments_used"] == 2
    assert result["run_acceptance"]["review_budget"] == ordinary
    request = resumed_agents.development_requests[-1]
    assert request["ci_evidence"] == agents.development_requests[-1]["ci_evidence"]
    assert "PLATFORM_RESTORED" in str(request["human_response_history"])


def test_parent_ci_target_append_findings_use_integration_allowance(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()
    target_heads: list[str] = []

    class AppendTargetAgents(ParentCIAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") == "integration":
                self.development_requests.append(request)
                (self.checkout / "integration-fix.txt").write_text("fixed combination\n")
                return DevelopmentResult(thread_id=str(request["thread_id"]), summary="Fixed integration regression")
            return super().develop(request)

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            ordinal = len(self.review_requests)
            if ordinal == 2:
                assert request["work_source"] == "ci"
                collaborator = git_repo.parent / "ci-target-append"
                previous = git.resolve("main")
                subprocess.run(["git", "worktree", "add", "--detach", str(collaborator), previous],
                               cwd=git_repo, check=True, capture_output=True)
                try:
                    (collaborator / "target-after-ci.txt").write_text("new target\n")
                    subprocess.run(["git", "add", "target-after-ci.txt"], cwd=collaborator, check=True)
                    subprocess.run(["git", "commit", "-m", "append target during CI review"],
                                   cwd=collaborator, check=True, capture_output=True)
                    target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=collaborator,
                                            check=True, capture_output=True, text=True).stdout.strip()
                finally:
                    subprocess.run(["git", "worktree", "remove", "--force", str(collaborator)],
                                   cwd=git_repo, check=True, capture_output=True)
                subprocess.run(["git", "update-ref", "refs/heads/main", target, previous],
                               cwd=git_repo, check=True)
                publisher.data["default_head_sha"] = target
                publisher.data["delivery"]["published_branches"]["main"] = target
                for pull in publisher.data["delivery"]["pull_requests"]:
                    pull["base_sha"] = target
                publisher._save()
                target_heads.append(target)
            return ReviewResult(thread_id=f"append-reviewer-{ordinal}",
                                artifact=_candidate_finding_artifact() if ordinal == 3 else _passing_artifact())

    agents = AppendTargetAgents(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    engine.deliver(run_id)
    result = engine.deliver(run_id)
    assert result["status"] == "parent_approval_pending", (result["diagnostics"], result["parent_job"].get("blocked_reason"))
    job = result["parent_job"]
    assert job["ci_recovery"]["developments_used"] == 1
    assert job["ci_recovery"]["reviewer_invocations"] == 1
    assert job["integration_development_budget"]["used"] == 1
    assert job["review_budget"]["development_attempts"] == 1
    assert [request.get("work_source") for request in agents.review_requests[1:]] == ["ci", "integration", "integration"]
    assert agents.development_requests[-1]["repair_source"] == "integration"
    assert "ci_evidence" not in agents.development_requests[-1]
    assert job["ci_evidence"] == agents.review_requests[1]["ci_evidence"]
    assert job["ci_recovery"]["failure_head"] == job["ci_evidence"]["head_sha"]
    assert git.is_ancestor(target_heads[0], job["candidate_sha"])


def test_run_ci_target_append_findings_use_shared_integration_owner(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = str(state["run_id"])
    target_branch = str(state["run_branch"])
    targets: list[str] = []

    class AppendPublisher(FixtureGitHubPublisher):
        def live_branch_heads(self, source_branch: str, base_branch: str) -> tuple[str | None, str]:
            current = states.load_run(run_id)
            job = current.get("run_acceptance", {}).get("repair_job", {}) if current else {}
            if (base_branch == target_branch and not targets and job.get("phase") == "accepted"
                and current["run_acceptance"]["ci_recovery"]["developments_used"] == 1):
                checkout = git_repo.parent / "run-ci-target-append"
                previous = git.resolve(target_branch)
                subprocess.run(["git", "worktree", "add", "--detach", str(checkout), previous],
                               cwd=git_repo, check=True, capture_output=True)
                try:
                    (checkout / "run-target-after-ci.txt").write_text("new Run contribution\n")
                    subprocess.run(["git", "add", "run-target-after-ci.txt"], cwd=checkout, check=True)
                    subprocess.run(["git", "commit", "-m", "append Run target after CI"],
                                   cwd=checkout, check=True, capture_output=True)
                    target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                            check=True, capture_output=True, text=True).stdout.strip()
                finally:
                    subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                                   cwd=git_repo, check=True, capture_output=True)
                self.data["delivery"]["published_branches"][target_branch] = target
                for pull in self.data["delivery"]["pull_requests"]:
                    if pull["base_branch"] == target_branch:
                        pull["base_sha"] = target
                self._save()
                targets.append(target)
            return super().live_branch_heads(source_branch, base_branch)

    publisher = AppendPublisher(git_repo / "github.json", git)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass", "pass"]
    publisher._save()
    class IntegrationAgents(RunCIAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            if not self.review_requests:
                self.review_requests.append(request)
                return ReviewResult(thread_id="run-ci-reviewer-1", artifact=self._reviews.pop(0))
            return super().review(request)

    agents = IntegrationAgents()
    agents._reviews = [_repair_artifact(), _passing_artifact(), _passing_artifact(),
                       _candidate_finding_artifact(), _passing_artifact()]
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    result = engine.accept(run_id)
    if result["status"] == "run_acceptance_pending":
        result = engine.accept(run_id)
    assert result["status"] == "run_publication_pending", result["diagnostics"]
    run = result["run_acceptance"]
    assert run["ci_recovery"]["developments_used"] == 1
    assert run["ci_recovery"]["reviewer_invocations"] == 1
    assert run["review_budget"]["development_attempts"] == 2
    assert [request.get("work_source") for request in agents.review_requests[2:]] == ["ci", "integration", "integration"]
    assert agents.development_requests[-1]["work_source"] == "integration"
    assert agents.development_requests[-1]["repair_source"] == "integration"
    assert "ci_evidence" not in agents.development_requests[-1]
    assert run["completed_repair_jobs"][-1]["ci_evidence"]["head_sha"] == agents.review_requests[2]["ci_evidence"]["head_sha"]
    assert git.is_ancestor(targets[0], git.resolve(target_branch))
