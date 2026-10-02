"""Automatic updates never authorize scope, identity or history changes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.runner_credentials import RunnerCredentialError
from support.cli import load_only_run_state
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, git_command
from support.published_run import prepare_published_run
from support.workspace import managed_repo


@pytest.mark.parametrize("gate", [
    "requirement", "graph", "foreign_pr", "source_rewrite", "target_rewrite", "permission",
])
def test_run_approval_update_preserves_hard_gates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    fixture, before = prepare_published_run(git_repo)
    append_remote(git_repo, fixture, "main", "target.txt", "new target\n")
    data = json.loads(fixture.read_text())
    final_pr = data["delivery"]["pull_requests"][-1]
    if gate == "requirement":
        data["parent"]["body"] += "\nChanged acceptance requirement."
    elif gate == "graph":
        data["parent"]["sub_issues"] = []
    elif gate == "foreign_pr":
        final_pr["head_repository"] = "another/project"
    elif gate == "source_rewrite":
        data["delivery"]["published_branches"][before["run_branch"]] = before["base"]["sha"]
        final_pr["head_sha"] = before["base"]["sha"]
    elif gate == "target_rewrite":
        root = managed_repo(git_repo)
        head = git_command(root, "commit-tree", before["base"]["sha"] + "^{tree}", "-m", "unrelated history")
        git_command(root, "update-ref", "refs/heads/main", head)
        data["default_head_sha"] = head
        data["delivery"]["published_branches"]["main"] = head
        final_pr["base_sha"] = head
    else:
        def denied(*_args: Any, **_kwargs: Any) -> Any:
            raise RunnerCredentialError("Runner repository permission was revoked")
        monkeypatch.setattr(FixtureGitHubPublisher, "live_branch_heads", denied)
    fixture.write_text(json.dumps(data))

    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 2, result.stdout + result.stderr
    after = load_only_run_state(git_repo)
    assert after["status"] in {"ready_for_human", "unsupported_scope_change", "deterministic_contradiction", "execution_failed"}
    assert after["ticket_jobs"] == before["ticket_jobs"]
    assert after["agent_invocation_history"] == before["agent_invocation_history"]
    assert after["run_acceptance"]["review_budget"] == before["run_acceptance"]["review_budget"]
    assert after["run_acceptance"]["ci_recovery"] == before["run_acceptance"]["ci_recovery"]
    assert "approval_update" not in after["run_publication"]
    external = json.loads(fixture.read_text())["delivery"]
    for field in ("pull_requests", "published_branches", "closed_issues", "mutations"):
        assert external.get(field) == data["delivery"].get(field)
