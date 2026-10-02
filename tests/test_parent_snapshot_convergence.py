"""PR and branch reads may straddle another allowed remote append."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.executor_host import FakeExecutorHost
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from support.cli import load_only_run_state, stdout_json
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, git_command, ready_parent, update_agents
from support.workspace import managed_repo


@pytest.mark.parametrize("moving_ref", ["source", "target"])
def test_approve_rereads_a_parent_snapshot_split_by_remote_append(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, moving_ref: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    target = append_remote(git_repo, fixture, "main", "first.txt", "first advance\n")
    agents = update_agents(git_repo)
    observed: list[str] = []

    class MovingRefs(FixtureGitHubPublisher):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            snapshot = super().live_pull_request(pr_number)
            if not observed:
                branch = before["parent_job"]["parent_branch"] if moving_ref == "source" else "main"
                observed.append(append_remote(git_repo, fixture, branch, "later.txt", "later advance\n"))
                self.data = json.loads(fixture.read_text())
            return snapshot

    monkeypatch.setattr(cli, "FixtureGitHubPublisher", MovingRefs)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **_: FakeExecutorHost())
    result = invoke_cli_inprocess(
        git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["action"]["status"] == "updating"
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert state["status"] == "parent_approval_pending"
    assert state["run_id"] == before["run_id"]
    assert job["parent_branch"] == before["parent_job"]["parent_branch"]
    assert job["pr_number"] == before["parent_job"]["pr_number"]
    assert job.get("approval_grant") is None
    assert job["approval_update"][f"{moving_ref}_sha"] == observed[0]
    assert job["integration_resync"]["used"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert job["review_budget_history"][0]["review_budget"] == before["parent_job"]["review_budget"]
    assert job["ci_recovery"] == before["parent_job"]["ci_recovery"]
    git = GitRepository(managed_repo(git_repo))
    assert all(git.is_ancestor(sha, job["published_sha"]) for sha in (target, observed[0]))
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "OPEN"
    assert not delivery.get("closed_issues")


@pytest.mark.parametrize("change", ["source_rewrite", "target_rewrite", "foreign_pr"])
def test_snapshot_reread_preserves_identity_and_ancestry_gates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    target = append_remote(git_repo, fixture, "main", "first.txt", "first advance\n")
    observed: list[dict[str, Any]] = []

    class ChangedIdentity(FixtureGitHubPublisher):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            snapshot = super().live_pull_request(pr_number)
            if not observed:
                if change == "foreign_pr":
                    append_remote(git_repo, fixture, "main", "later.txt", "later advance\n")
                data = json.loads(fixture.read_text())
                delivery = data["delivery"]
                if change == "source_rewrite":
                    branch = before["parent_job"]["parent_branch"]
                    delivery["published_branches"][branch] = before["base"]["sha"]
                    delivery["pull_requests"][0]["head_sha"] = before["base"]["sha"]
                elif change == "target_rewrite":
                    managed = managed_repo(git_repo)
                    rewritten = git_command(managed, "commit-tree", f"{target}^{{tree}}", "-m", "unrelated history")
                    git_command(managed, "update-ref", "refs/heads/main", rewritten, target)
                    delivery["published_branches"]["main"] = rewritten
                    delivery["pull_requests"][0]["base_sha"] = rewritten
                    data["default_head_sha"] = rewritten
                else:
                    delivery["pull_requests"][0]["head_repository"] = "other/project"
                fixture.write_text(json.dumps(data))
                self.data = data
                observed.append(deepcopy(delivery))
            return snapshot

    monkeypatch.setattr(cli, "FixtureGitHubPublisher", ChangedIdentity)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **_: FakeExecutorHost())
    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 2, result.stdout + result.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "blocked"
    job = state["parent_job"]
    assert "approval_update" not in job
    for key in ("candidate_sha", "integration_resync", "review_budget", "ci_recovery"):
        assert job[key] == before["parent_job"][key]
    assert state["agent_invocation_history"] == before["agent_invocation_history"]
    after = json.loads(fixture.read_text())["delivery"]
    for key in ("pull_requests", "published_branches", "closed_issues", "mutations"):
        assert after.get(key) == observed[0].get(key)


def test_inconsistent_parent_reads_have_a_bounded_wait_and_can_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, before = ready_parent(git_repo)
    append_remote(git_repo, fixture, "main", "first.txt", "first advance\n")
    data = json.loads(fixture.read_text())
    data["supervision_clock_multiplier"] = 60
    fixture.write_text(json.dumps(data))
    stale: list[dict[str, Any]] = []
    lagging = True

    class LaggingPR(FixtureGitHubPublisher):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            snapshot = super().live_pull_request(pr_number)
            data = json.loads(fixture.read_text())
            data["parent_snapshot_reads"] = data.get("parent_snapshot_reads", 0) + 1
            fixture.write_text(json.dumps(data))
            self.data = data
            if lagging and not stale:
                stale.append(snapshot)
                append_remote(git_repo, fixture, "main", "later.txt", "later advance\n")
                self.data = json.loads(fixture.read_text())
            return stale[0] if lagging else snapshot

    monkeypatch.setattr(cli, "FixtureGitHubPublisher", LaggingPR)
    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 2, result.stdout + result.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "supervision_timeout"
    assert state["supervision_wait"]["kind"] == "github_convergence"
    assert 1 < json.loads(fixture.read_text())["parent_snapshot_reads"] <= 5
    assert "approval_update" not in state["parent_job"]
    for key in ("candidate_sha", "integration_resync", "review_budget", "ci_recovery"):
        assert state["parent_job"][key] == before["parent_job"][key]
    assert state["agent_invocation_history"] == before["agent_invocation_history"]
    assert json.loads(fixture.read_text())["delivery"]["pull_requests"][0]["state"] == "OPEN"

    lagging = False
    agents = update_agents(git_repo)
    resumed = invoke_cli_inprocess(
        git_repo, fixture, "resume", before["run_id"], "--agent-fixture", str(agents), "--json",
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = load_only_run_state(git_repo)
    assert final["status"] == "parent_approval_pending"
    assert final["parent_job"].get("approval_grant") is None
    assert final["parent_job"]["integration_resync"]["used"] == 1
    assert final["parent_job"]["pr_number"] == before["parent_job"]["pr_number"]
