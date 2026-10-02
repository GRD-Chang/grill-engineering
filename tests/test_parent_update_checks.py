"""A Required Checks race must use the ordinary Parent integration gate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.executor_host import FakeExecutorHost
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from support.cli import load_only_run_state
from support.cli_payloads import parent_publication, passing_acceptance
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, ready_parent, update_agents
from support.workspace import managed_repo


@pytest.mark.parametrize("change", ["append", "rewrite", "foreign_pr"])
def test_source_change_during_checks_reconciles_only_allowed_append(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    append_remote(git_repo, fixture, "main", "target.txt", "target\n")
    agents = update_agents(git_repo)
    script = json.loads(agents.read_text())
    script["reviews"].append(passing_acceptance("latest-reviewer", "Latest combination passed"))
    script["publications"].append(parent_publication())
    agents.write_text(json.dumps(script))
    observed: list[str] = []

    class MovingSource(FixtureGitHubPublisher):
        def required_checks_snapshot(self, pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
            if not observed:
                branch = before["parent_job"]["parent_branch"]
                head = append_remote(git_repo, fixture, branch, "later.txt", "later contribution\n")
                data = json.loads(fixture.read_text())
                if change == "rewrite":
                    head = before["base"]["sha"]
                    data["delivery"]["published_branches"][branch] = head
                    data["delivery"]["pull_requests"][0]["head_sha"] = head
                elif change == "foreign_pr":
                    data["delivery"]["pull_requests"][0]["head_repository"] = "other/project"
                fixture.write_text(json.dumps(data))
                self.data = data
                observed.append(head)
            return super().required_checks_snapshot(pr_number, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(cli, "FixtureGitHubPublisher", MovingSource)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **_: FakeExecutorHost())
    result = invoke_cli_inprocess(
        git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
    )
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert observed
    assert job["pr_number"] == before["parent_job"]["pr_number"]
    assert not job.get("approval_grant")
    if change == "append":
        assert result.returncode == 0, result.stdout + result.stderr
        assert state["status"] == "parent_approval_pending"
        assert job["integration_resync"]["used"] == 2
        assert job["acceptance_record"]["reviewed_candidate_sha"] == job["candidate_sha"]
        assert GitRepository(managed_repo(git_repo)).is_ancestor(observed[0], job["published_sha"])
    else:
        assert result.returncode == 2, result.stdout + result.stderr
        assert state["status"] == "blocked"
        assert job["integration_resync"]["used"] == 1
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "OPEN"
