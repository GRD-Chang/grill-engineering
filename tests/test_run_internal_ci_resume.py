"""Resume exhausted CI recovery when the failed PR belongs to Run Repair."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.review_budget import review_budget_owner

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import (
    _candidate_finding_artifact,
    _passing_artifact,
    _repair_artifact,
)
from support.run_scenarios import _completed_run


class InternalCIAgents(ScriptedRunAgents):
    def __init__(self, reviews: list[dict[str, object]], *, reviewer_offset: int = 0) -> None:
        super().__init__()
        self._reviews = reviews
        self.reviewer_offset = reviewer_offset

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        target = Path(str(request["checkout"])) / "run-repair.txt"
        prior = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(
            prior + f"repair {len(self.development_requests)}\n", encoding="utf-8"
        )
        return DevelopmentResult(
            thread_id="internal-ci-developer", summary="Repaired Run candidate"
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        assert Path(str(request["checkout"])).is_dir()
        return ReviewResult(
            thread_id=f"internal-ci-reviewer-{self.reviewer_offset + len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )


@pytest.mark.parametrize("exhaustion", ["inner", "outer"])
def test_internal_repair_ci_exhaustion_resumes_without_final_pr_evidence(
    git_repo: Path, exhaustion: str
) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = str(state["run_id"])
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.data["delivery"]["required_checks"] = (
        ["fail", "pass"] if exhaustion == "inner" else ["fail"] * 4 + ["pass"]
    )
    publisher._save()
    ci_reviews = (
        [_candidate_finding_artifact()] * 3
        if exhaustion == "inner"
        else [_passing_artifact()] * 3
    )
    agents = InternalCIAgents([_repair_artifact(), _passing_artifact(), *ci_reviews])
    blocked = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    ).accept(run_id)

    assert blocked["status"] == "ready_for_human"
    run = blocked["run_acceptance"]
    assert run["blocked_reason"] == f"ci_{exhaustion}_budget_exhausted"
    assert "ci_failed_evidence" not in run
    job = run["repair_job"]
    failed_pr = job["pr_number"]
    failed_head = job["publication_sha"]
    assert job["required_checks_origin"]["head_sha"] == failed_head
    assert job["required_checks_origin"]["pr_number"] == failed_pr
    if exhaustion == "inner":
        assert "ci_evidence" not in job
        assert job["repair_source"] == "acceptance"
    else:
        assert job["ci_evidence"]["head_sha"] == failed_head
        assert job["ci_evidence"]["pr_number"] == failed_pr
    assert run["ci_recovery"]["failure_head"] == failed_head
    assert run["ci_recovery"] == job["ci_recovery"]
    assert review_budget_owner(run)["review_budget"]["development_attempts"] == 1
    ordinary_budget = review_budget_owner(run)["review_budget"].copy()

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
    )
    resumed_run = resumed["run_acceptance"]
    assert resumed_run["repair_cycle"]["status"] == "active"
    assert review_budget_owner(resumed_run)["review_budget"] == ordinary_budget
    assert resumed_run["ci_recovery"] == resumed_run["repair_job"]["ci_recovery"]
    assert resumed_run["ci_recovery"]["failure_head"] == failed_head
    assert resumed_run["ci_recovery"]["rounds_used"] == 1
    assert resumed_run["ci_recovery"]["outer_window"] == (2 if exhaustion == "outer" else 1)
    assert resumed_run["ci_recovery"]["inner_window"] == (2 if exhaustion == "inner" else 1)
    assert "repair_request" not in resumed_run

    passing_agents = InternalCIAgents(
        [_passing_artifact()], reviewer_offset=len(agents.review_requests)
    )
    recovered = RunAcceptanceEngine(
        git=git, states=states, agents=passing_agents, github=publisher,
    ).accept(run_id)
    assert recovered["status"] == "run_publication_pending"
    assert recovered["run_acceptance"]["review_budget"] == ordinary_budget
    assert recovered["run_acceptance"]["ci_recovery"]["rounds_used"] == 1
    assert recovered["run_acceptance"]["completed_repair_jobs"][-1]["repair_branch"] == job["repair_branch"]
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    assert len(passing_agents.development_requests) == 1
    assert len(passing_agents.review_requests) == 1
