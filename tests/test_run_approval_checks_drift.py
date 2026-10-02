"""Checks reads cannot lose an approved Run's automatic update handoff."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication import RunPublicationEngine
from support.cli import load_only_run_state, stdout_json
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, control_record, git_command
from support.published_run import prepare_published_run
from support.run_approval_update import update_agents
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run


@pytest.mark.parametrize("boundary", ["snapshot", "snapshot-result", "failure-evidence"])
def test_public_approve_updates_append_observed_during_checks(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    fixture, before = prepare_published_run(git_repo)
    agents = update_agents(git_repo)
    data = json.loads(fixture.read_text())
    data["delivery"].update(
        required_checks=["fail", "pass"] if boundary == "failure-evidence" else ["pass"],
        check_position=0,
    )
    fixture.write_text(json.dumps(data))
    method = "required_check_evidence" if boundary == "failure-evidence" else "required_checks_snapshot"
    original = getattr(FixtureGitHubPublisher, method)
    observed_path = git_repo / "checks-append.json"

    def move_heads(self: FixtureGitHubPublisher, pr_number: int, **kwargs: Any) -> dict[str, Any]:
        if observed_path.exists():
            return original(self, pr_number, **kwargs)
        result = original(self, pr_number, **kwargs) if boundary == "snapshot-result" else None
        source = append_remote(
            git_repo, fixture, before["run_branch"], "source.txt", "Run append during checks\n",
        )
        target = append_remote(
            git_repo, fixture, "main", "target.txt", "Default append during checks\n",
        )
        observed_path.write_text(json.dumps({"source": source, "target": target}))
        self.data = json.loads(fixture.read_text())
        if result is not None:
            result["head_sha"] = source
            return result
        return original(self, pr_number, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, method, move_heads)

    result = invoke_cli_inprocess(
        git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    appended = json.loads(observed_path.read_text())
    assert stdout_json(result)["action"]["status"] == "updating"
    after = load_only_run_state(git_repo)
    assert after["status"] == "run_approval_pending"
    assert after["run_id"] == before["run_id"]
    assert after["run_branch"] == before["run_branch"]
    assert after["ticket_jobs"] == before["ticket_jobs"]
    publication = after["run_publication"]
    assert publication["pr_number"] == before["run_publication"]["pr_number"]
    assert publication.get("approval_grant") is None
    assert publication["approval_update"]["source_sha"] == appended["source"]
    assert publication["approval_update"]["target_sha"] == appended["target"]
    run = after["run_acceptance"]
    assert run["acceptance_record"]["reviewed_default_base_sha"] == appended["target"]
    assert run["acceptance_record"]["reviewed_head_sha"] == publication["published_head_sha"]
    assert run["review_budget"]["reviewer_invocations"] == 1
    assert run["review_budget"]["development_attempts"] == 0
    assert run["ci_recovery"] == before["run_acceptance"]["ci_recovery"]
    assert run["integration_resync"]["used"] == 1
    assert publication["required_checks_evidence"]["result"] == "pass"
    control = control_record(git_repo)
    assert control["action"]["result_status"] == "updating"
    assert control["executor"]["status"] == "exited"
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 2
    assert delivery["pull_requests"][-1]["state"] == "OPEN"
    assert 1 not in delivery["closed_issues"]


@pytest.mark.parametrize("gate", [
    "foreign-pr", "source-rewrite", "target-rewrite", "ref-unavailable",
])
def test_checks_head_drift_rechecks_identity_history_and_read_availability(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    published = engine.publish(state["run_id"])
    original = publisher.required_checks_snapshot

    def changed_snapshot(pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
        source = state["base"]["sha"] if gate == "source-rewrite" else git_command(
            git_repo, "commit-tree", expected_head_sha + "^{tree}",
            "-p", expected_head_sha, "-m", "External source append during checks",
        )
        delivery = publisher.data["delivery"]
        delivery["published_branches"][state["run_branch"]] = source
        if gate == "foreign-pr":
            delivery["pull_requests"][-1]["head_repository"] = "another/project"
        elif gate == "target-rewrite":
            delivery["published_branches"]["main"] = git_command(
                git_repo, "commit-tree", git.resolve("main^{tree}"), "-m", "Unrelated target history",
            )
        elif gate == "ref-unavailable":
            def unavailable(*_args: Any) -> Any:
                raise OSError("Remote refs unavailable during checks reconciliation")
            monkeypatch.setattr(publisher, "live_branch_heads", unavailable)
        publisher._save()
        return original(pr_number, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(publisher, "required_checks_snapshot", changed_snapshot)

    result = engine.approve(state["run_id"])

    expected = {
        "foreign-pr": "run_acceptance_pending",
        "source-rewrite": "ready_for_human",
        "target-rewrite": "ready_for_human",
        "ref-unavailable": "waiting_external",
    }
    assert result["status"] == expected[gate]
    assert result["run_publication"].get("approval_update") is None
    assert result["ticket_jobs"] == published["ticket_jobs"]
    assert result["run_acceptance"]["ci_recovery"] == published["run_acceptance"]["ci_recovery"]
    assert "repair_request" not in result["run_acceptance"]
    assert len(agents.requests) == 1  # Only the initial publication, no update or repair.
    assert publisher.data["delivery"]["pull_requests"][-1]["state"] == "OPEN"
    assert 1 not in publisher.data["delivery"]["closed_issues"]
    if gate == "ref-unavailable":
        assert result["run_publication"]["approval_grant"]["head_sha"] == published["run_publication"]["published_head_sha"]
    else:
        assert result["run_publication"].get("approval_grant") is None
