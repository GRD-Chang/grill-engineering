"""最终 PR 决策组合；真实入口及身份回读由 CLI/Publication 测试覆盖。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.ci_recovery_budget import (
    mark_ci_repair,
    observe_repairable_ci_failure,
    reset_exhausted_ci_budget,
)
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication_shared import RunPublicationShared
from support.final_publication import final_publication_context
from support.run_publication import RunPublicationAgents



class ChecksPublisher(FixtureGitHubPublisher):
    evidence: dict[str, Any]
    pr_number: int
    expected_head: str
    expected_base: str
    run_branch: str

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None,
    ) -> dict[str, Any]:
        assert (pr_number, expected_head_sha) == (self.pr_number, self.expected_head)
        return deepcopy(self.evidence)

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        assert pr_number == self.pr_number
        return {
            "state": "OPEN", "head_sha": self.expected_head, "base_sha": self.expected_base,
            "head_branch": self.run_branch, "base_branch": "main",
            "head_repository": "example/project", "base_repository": "example/project",
        }


@pytest.fixture
def ci_context(git_repo: Path) -> tuple[RunPublicationShared, ChecksPublisher, dict[str, Any]]:
    # 真实合法 Run、SQLite 与 PR 身份；只替换当前 CI 观测读取。
    states, git, state = final_publication_context(git_repo, approved=False)
    publisher = ChecksPublisher(git_repo / "github.json", git)
    publisher.pr_number = int(state["run_publication"]["pr_number"])
    publisher.expected_head = git.resolve(str(state["run_branch"]))
    publisher.expected_base = git.resolve("main")
    publisher.run_branch = str(state["run_branch"])
    publisher.evidence = {
        "pr_number": publisher.pr_number, "head_sha": publisher.expected_head, "result": "fail",
        "checks": [{
            "workflow": "CI", "name": "quality", "link": "https://example.invalid/check/1",
            "bucket": "fail", "state": "FAILURE", "repairability": "code_failure",
        }],
    }
    engine = RunPublicationShared(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=publisher.expected_base,
    )
    loaded = engine._load(state["run_id"])
    assert loaded is not None
    return engine, publisher, loaded


@pytest.mark.parametrize("scenario", [
    "repairable", "wrong-pr", "wrong-head", "wrong-check", "cancelled",
    "platform", "unclassified", "mixed-pending", "pending", "unknown", "pass", "none",
])
def test_final_check_classification_preserves_unrelated_budgets(
    ci_context: tuple[RunPublicationShared, ChecksPublisher, dict[str, Any]],
    scenario: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, publisher, state = ci_context
    observation = deepcopy(publisher.evidence)
    evidence = publisher.evidence
    if scenario == "wrong-pr":
        evidence["pr_number"] = publisher.pr_number + 1
    elif scenario == "wrong-head":
        evidence["head_sha"] = "c" * 40
    elif scenario == "wrong-check":
        evidence["checks"][0]["link"] += "-different"
    elif scenario == "cancelled":
        evidence["checks"][0].update(bucket="cancel", state="CANCELLED")
        observation["checks"] = deepcopy(evidence["checks"])
    elif scenario == "platform":
        evidence["checks"][0]["state"] = "TIMED_OUT"
    elif scenario == "unclassified":
        evidence["checks"][0].pop("repairability")
    elif scenario == "mixed-pending":
        observation["checks"].append({"bucket": "pending"})
    elif scenario in {"pending", "unknown", "pass", "none"}:
        observation.update(result=scenario, checks=[] if scenario == "none" else [
            {"bucket": {"unknown": "unrecognized"}.get(scenario, scenario)},
        ])
    before = deepcopy(state["run_acceptance"])
    now = 10.25
    monkeypatch.setattr("agent_run.external_supervision.monotonic", lambda: now)
    snapshots = []
    for _ in range(2):
        result = engine._route_required_checks(
            state, state["run_publication"], publisher.pr_number, publisher.expected_head, observation,
        )
        if scenario in {"pass", "none"}:
            assert result is None
            assert state["run_acceptance"] == before
            return
        assert result is state
        assert engine._load(state["run_id"]) == state
        snapshots.append(deepcopy(state))
        now += 30
    assert snapshots[0] == snapshots[1]  # Same head/window cannot buy more budget.
    run = state["run_acceptance"]
    assert run["review_budget"] == before["review_budget"]
    assert run["modification_attempts"] == before["modification_attempts"]
    assert run["policy_snapshot"] == before["policy_snapshot"]
    if scenario in {"repairable", "cancelled", "platform", "unclassified"}:
        assert state["status"] == "run_acceptance_pending"
        assert run["repair_request"] == {"repair_source": "required_checks", "ci_evidence": evidence}
        assert run["ci_failed_evidence"] == evidence
        assert run["ci_recovery"]["window"] == 1
        assert run["ci_recovery"]["developments_used"] == 0  # Charged only on actual development.
        assert "repair_job" not in run
    else:
        assert run == before
        assert state["status"] == ("waiting_checks" if scenario == "pending" else "waiting_external")
        assert state["supervision_window"]["started_at"] == 10.25
        assert state["supervision_window"]["kind"] == (
            "required_checks" if scenario == "pending" else "github_convergence"
        )


@pytest.mark.parametrize("new_failure_versions", [False, True])
def test_final_ci_checkpoint_replay_and_renewal_preserve_exact_history(
    ci_context: tuple[RunPublicationShared, ChecksPublisher, dict[str, Any]],
    new_failure_versions: bool,
) -> None:
    engine, publisher, state = ci_context
    run = state["run_acceptance"]
    before_review = deepcopy(run["review_budget"])
    for index in range(3):
        observe_repairable_ci_failure(
            run, head_sha=f"failed-{index}" if new_failure_versions else publisher.expected_head,
            limit=3, is_ticket=False,
        )
        mark_ci_repair(run, limit=3, is_ticket=False)
    engine._route_required_checks(
        state, state["run_publication"], publisher.pr_number, publisher.expected_head, publisher.evidence,
    )
    reason = "ci_development_budget_exhausted"
    assert state["status"] == "ready_for_human"
    assert run["blocked_reason"] == state["diagnostics"][0]["code"] == reason
    assert state["run_publication"]["phase"] == "stale"
    assert run["ci_failed_evidence"] == publisher.evidence
    assert "repair_request" not in run
    paused = deepcopy(state)
    engine._route_required_checks(
        state, state["run_publication"], publisher.pr_number, publisher.expected_head, publisher.evidence,
    )
    assert state == paused == engine._load(state["run_id"])
    budget_before = deepcopy(run["ci_recovery"])
    reset_exhausted_ci_budget(run, limit=3, is_ticket=False)
    engine._route_required_checks(
        state, state["run_publication"], publisher.pr_number, publisher.expected_head, publisher.evidence,
    )
    budget = run["ci_recovery"]
    assert budget["history"][:-1] == budget_before["history"]
    assert budget["history"][-1] == {
        "kind": "resume", "reason": reason,
        **{key: budget_before[key] for key in (
            "window", "developments_used", "failure_head",
        )},
    }
    assert budget["failure_head"] == publisher.expected_head
    assert budget["window"] == 2
    assert budget["developments_used"] == 0
    assert run["review_budget"] == before_review
    assert state["status"] == "run_acceptance_pending"
    assert run["repair_request"]["ci_evidence"] == publisher.evidence
