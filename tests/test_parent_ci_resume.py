"""Resume Parent CI exhaustion bound to its actual Final Run PR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import (
    _candidate_finding_artifact,
    _passing_artifact,
)
from support.run_scenarios import _accepted_run
from support.run_publication import RunPublicationAgents
from agent_run.run_publication import RunPublicationEngine
from copy import deepcopy


class FinalCIAgents(ScriptedRunAgents):
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
            thread_id=str(request.get("thread_id") or f"final-ci-developer-{self.reviewer_offset + len(self.development_requests)}"), summary="Repaired Run candidate"
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        assert Path(str(request["checkout"])).is_dir()
        return ReviewResult(
            thread_id=f"final-ci-reviewer-{self.reviewer_offset + len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )


@pytest.mark.parametrize("exhaustion", ["review_findings", "new_failed_head"])
def test_parent_ci_exhaustion_resumes_with_exact_final_pr_evidence(
    git_repo: Path, exhaustion: str,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    fixture = git_repo / "github.json"
    publisher.data["delivery"]["required_checks"] = (
        ["fail", "pass"] if exhaustion == "review_findings" else ["fail"] * 4 + ["pass"]
    )
    publisher._save()
    ordinary_budget = deepcopy(state["run_acceptance"]["review_budget"])
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    failed = publication.publish(run_id)
    final_pr = failed["run_publication"]["pr_number"]
    agents = FinalCIAgents(
        [_candidate_finding_artifact()] * 3 if exhaustion == "review_findings"
        else [_passing_artifact()] * 3,
    )
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    if exhaustion == "review_findings":
        blocked = engine.accept(run_id)
    else:
        for _ in range(3):
            assert engine.accept(run_id)["status"] == "run_publication_pending"
            blocked = publication.publish(run_id)
    assert blocked["status"] == "ready_for_human"
    run = blocked["run_acceptance"]
    assert run["blocked_reason"] == "ci_development_budget_exhausted"
    assert run["ci_recovery"]["developments_used"] == 3
    failed_head = run["ci_recovery"]["failure_head"]
    evidence = run["ci_failed_evidence"]
    assert evidence["checks"][0]["job"]["head_sha"] == failed_head
    assert evidence["pr_number"] == final_pr
    if exhaustion == "review_findings":
        assert run["repair_job"]["required_checks_origin"]["head_sha"] == failed_head
        assert "pr_number" not in run["repair_job"]
    assert run["review_budget"] == ordinary_budget

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
    )
    resumed_run = resumed["run_acceptance"]
    assert resumed_run["review_budget"] == ordinary_budget
    assert resumed_run["ci_recovery"]["failure_head"] == failed_head
    assert resumed_run["ci_recovery"]["window"] == 2
    assert resumed_run["ci_recovery"]["developments_used"] == 0
    assert resumed_run["ci_recovery"]["history"][-1]["developments_used"] == 3
    passing_agents = FinalCIAgents(
        [_passing_artifact()], reviewer_offset=len(agents.review_requests),
    )
    recovered = RunAcceptanceEngine(
        git=git, states=states, agents=passing_agents, github=publisher,
    ).accept(run_id)
    assert recovered["status"] == "run_publication_pending"
    assert recovered["run_acceptance"]["review_budget"] == ordinary_budget
    assert recovered["run_acceptance"]["ci_recovery"]["window"] == 2
    assert recovered["run_acceptance"]["ci_recovery"]["developments_used"] == 1
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    assert publisher.data["delivery"]["pull_requests"][0]["number"] == final_pr
    assert len(passing_agents.development_requests) == 1
    assert len(passing_agents.review_requests) == 1
