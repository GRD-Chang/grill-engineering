"""Parent CI recovery across the delivery and Run Repair boundaries."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.cli_presentation import _run_repair_status
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.delivery_agents import PassAgents
from support.run_artifacts import _candidate_finding_artifact, _passing_artifact
from support.run_agents import ScriptedRunAgents
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run
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
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = ["fail", "pass"]
    publisher._save()
    agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")

    engine = ParentDeliveryEngine(
        git=git, states=states, github=publisher, agents=agents,
    )
    failed = engine.deliver(run_id)
    assert failed["parent_job"]["phase"] == "repairing"
    result = engine.deliver(run_id)

    job = result["parent_job"]
    assert result["status"] == "parent_approval_pending", (job["phase"], job.get("blocked_reason"))
    assert job["ci_recovery"]["rounds_used"] == 1
    assert job["ci_recovery"]["repairs_used"] == 1
    assert job["ci_recovery"]["reviewer_invocations"] == 1
    assert len(agents.review_requests) == 2
    assert agents.review_requests[-1]["candidate_sha"] == job["candidate_sha"]
    assert len(publisher.data["delivery"]["pull_requests"]) == 1


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
    assert repaired["run_acceptance"]["ci_recovery"]["repairs_used"] == 1
    assert repaired["run_acceptance"]["review_budget"]["development_attempts"] == 0
    assert repaired["run_acceptance"]["completed_repair_jobs"]
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr
    assert published["run_acceptance"]["ci_recovery"]["rounds_used"] == 1


def test_run_second_failed_final_head_uses_next_round_and_same_pr(git_repo: Path) -> None:
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
    agents._reviews = [_passing_artifact(), _passing_artifact()]
    acceptance = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    )

    first_repair = acceptance.accept(run_id)
    assert first_repair["status"] == "run_publication_pending"
    failed_again = publication.publish(run_id)
    assert failed_again["status"] == "run_acceptance_pending"
    assert failed_again["run_publication"]["pr_number"] == final_pr
    assert failed_again["run_acceptance"]["ci_recovery"]["rounds_used"] == 2

    second_repair = acceptance.accept(run_id)
    assert second_repair["status"] == "run_publication_pending"
    assert second_repair["run_acceptance"]["ci_recovery"]["repairs_used"] == 1
    assert second_repair["run_acceptance"]["review_budget"]["development_attempts"] == 0
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr
    assert len(agents.review_requests) == 2


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
    assert recovery["rounds_used"] == 2
    assert recovery["repairs_used"] == 1
    assert len(agents.development_requests) == 2
    assert len(agents.review_requests) == 2
    assert repaired["run_acceptance"]["review_budget"]["development_attempts"] == 0
    published = publication.publish(run_id)
    assert published["status"] == "run_approval_pending"
    assert published["run_publication"]["pr_number"] == final_pr


def test_run_ci_reviewer_exhausts_inner_without_advancing_outer(git_repo: Path) -> None:
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
    assert run["blocked_reason"] == "ci_inner_budget_exhausted"
    assert run["ci_recovery"]["rounds_used"] == 1
    assert run["ci_recovery"]["repairs_used"] == 3
    assert run["review_budget"]["development_attempts"] == 0
    assert len(agents.review_requests) == 3
    assert run["repair_cycle"]["status"] == "budget_exhausted"
    assert run["repair_cycle"]["ended_reason"] == "ci_inner_budget_exhausted"
    assert _run_repair_status(blocked)["code_modification_attempts"] == 0
    job = run["repair_job"]
    job["attempt_kind"] = "ordinary"
    job["review_budget"]["development_attempts"] = 1
    assert _run_repair_status(blocked)["code_modification_attempts"] == 1
    job["attempt_kind"] = "ci_repair"
    job["review_budget"]["development_attempts"] = 0

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
    assert recovered["run_acceptance"]["ci_recovery"]["rounds_used"] == 1
    assert recovered["run_acceptance"]["ci_recovery"]["inner_window"] == 2


def test_parent_only_ci_inner_resume_keeps_outer_round(git_repo: Path) -> None:
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
    assert job["blocked_reason"] == "ci_inner_budget_exhausted"
    assert job["ci_recovery"]["rounds_used"] == 1
    assert job["ci_recovery"]["repairs_used"] == 3
    ordinary_budget = job["review_budget"].copy()

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
    )
    assert resumed["parent_job"]["ci_recovery"]["inner_window"] == 2
    assert resumed["parent_job"]["ci_recovery"]["rounds_used"] == 1
    assert resumed["parent_job"]["review_budget"] == ordinary_budget
    passing_agents = ParentCIAgents(states.root / "worktrees" / run_id / "parent")
    recovered = ParentDeliveryEngine(
        git=git, states=states, github=publisher, agents=passing_agents,
    ).deliver(run_id)
    assert recovered["status"] == "parent_approval_pending"
    assert recovered["parent_job"]["pr_number"] == blocked["parent_job"]["pr_number"]
    assert recovered["parent_job"]["ci_recovery"]["rounds_used"] == 1
