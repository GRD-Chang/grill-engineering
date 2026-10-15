"""Automatic updates never authorize scope, identity or history changes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.runner_credentials import RunnerCredentialError
from agent_run.operator_action_facts import operator_action_facts
from agent_run.operator_gate import operator_gate_identity_is_consistent
from agent_run.state_contract import require_current_run_state
from support.cli import load_only_run_state, stdout_json
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
    if gate in {"foreign_pr", "permission"}:
        expected_status = "deterministic_contradiction" if gate == "foreign_pr" else "execution_failed"
        assert after["status"] == expected_status
        require_current_run_state(after)
        fact, = operator_action_facts(after)
        # 共享权威与凭据故障归 Run；保留的 Publication 不是故障拥有者。
        assert fact["work_subject"] == f"run:{before['run_id']}"
        assert fact["scope"] == "global"
        assert fact["phase"] == expected_status
        assert fact["status_code"] == expected_status
        assert fact["action_kind"] == (
            "deterministic_contradiction" if gate == "foreign_pr" else "execution_failure"
        )
        expected_code = "foreign_run_pr" if gate == "foreign_pr" else "runner_credential_failed"
        assert fact["reason"] == expected_code
        assert after["diagnostics"][0]["operator_gate"] == {
            "work_subject": f"run:{before['run_id']}",
            "phase": expected_status,
            "action_kind": fact["action_kind"],
            "reason": expected_code,
        }
        assert after["run_publication"]["phase"] == before["run_publication"]["phase"]
        assert after["run_publication"]["record"] == before["run_publication"]["record"]
        for command in ("status", "history"):
            queried = invoke_cli_inprocess(git_repo, fixture, command, before["run_id"], "--json")
            assert queried.returncode == 0, queried.stdout + queried.stderr
            action, = stdout_json(queried)["operator_actions"]
            assert action["work_subject"] == fact["work_subject"]
            assert action["scope"] == "global"
            assert action["status_code"] == expected_status
            assert action["action_kind"] == fact["action_kind"]
            assert action["reasons"] == [after["diagnostics"][0]["message"]]
        assert load_only_run_state(git_repo) == after
    external = json.loads(fixture.read_text())["delivery"]
    for field in ("pull_requests", "published_branches", "closed_issues", "mutations"):
        assert external.get(field) == data["delivery"].get(field)


@pytest.mark.parametrize("binding", ["missing", "wrong_subject", "wrong_phase", "wrong_action"])
def test_retained_approval_rejects_inaccurate_fault_binding(binding: str) -> None:
    evidence = {
        "work_subject": "run-publication:r1", "phase": "ready_for_approval",
        "action_kind": "deterministic_contradiction", "reason": "foreign_pr",
    }
    if binding != "missing":
        field = {"wrong_subject": "work_subject", "wrong_phase": "phase", "wrong_action": "action_kind"}[binding]
        evidence[field] = "different"
    state = {
        "run_id": "r1", "status": "deterministic_contradiction",
        "run_publication": {"phase": "ready_for_approval"},
        "diagnostics": [] if binding == "missing" else [{"operator_gate": evidence}],
    }
    assert not operator_gate_identity_is_consistent(state)
