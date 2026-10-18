"""Final PR 身份的独立进程入口代表；字段矩阵见 test_run_pr_identity。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_run.task_control import TaskControlStore, TaskKey
from support.cli import run_cli, stdout_json
from support.cli_payloads import final_run_agents
from support.inprocess_cli import invoke_cli_inprocess
from support.published_run import prepare_published_context
from support.state import persist_run_fixture


def test_persisted_final_run_pr_recovery_rejects_foreign_identity_without_effects(
    git_repo: Path,
) -> None:
    identity_error = {"head_repository": "foreign/project"}
    context = prepare_published_context(git_repo)
    fixture = context.fixture
    published = context.states.load_history_run(context.run_id)
    assert published is not None
    agents = git_repo / "final-run-identity-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    assert published["status"] == "run_approval_pending"
    before_state = published
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    final.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed_publication = json.loads(json.dumps(before_state))
    resumed_publication["status"] = "waiting_external"
    resumed_publication["terminal_kind"] = "waiting_external"
    resumed_publication["run_publication"]["phase"] = "waiting_external"
    persist_run_fixture(context.states, str(before_state["run_id"]), resumed_publication)

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents), extra_env=context.environment)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_state = context.states.load_history_run(context.run_id)
    assert after_state["terminal_kind"] == "deterministic_contradiction"
    assert after_state["diagnostics"][0]["code"] == "foreign_run_pr"
    assert after_state["run_publication"]["pr_number"] == before_state["run_publication"]["pr_number"]
    assert after_state["agent_invocation_history"] == before_state["agent_invocation_history"]
    status = invoke_cli_inprocess(
        git_repo, fixture, "status", str(before_state["run_id"]), "--json"
    )
    history = invoke_cli_inprocess(
        git_repo, fixture, "history", str(before_state["run_id"]), "--json"
    )
    assert stdout_json(status)["status"] == "deterministic_contradiction"
    assert "agent-run run" not in stdout_json(status)["next_action"]
    assert "agent-run resume" in stdout_json(history)["next_action"]

    control = TaskControlStore(context.states.root).load(TaskKey(
        context.workspace.repository_root, "example/project", 1,
    ))
    assert control is not None
    action = control["action"]
    receipt = after_state["action_application_receipt"]
    assert {key: receipt[key] for key in (
        "action_id", "kind", "payload_digest", "run_id", "executor_generation",
    )} == {
        "action_id": action["action_id"], "kind": action["kind"],
        "payload_digest": action["payload_digest"], "run_id": context.run_id,
        "executor_generation": control["executor"]["generation"],
    }



def test_explicit_approval_rejects_foreign_final_run_pr_identity_without_external_writes(
    git_repo: Path,
) -> None:
    identity_error = {"head_ref": "foreign-run-ref"}
    context = prepare_published_context(git_repo)
    fixture = context.fixture
    published = context.states.load_history_run(context.run_id)
    assert published is not None

    assert published["status"] == "run_approval_pending"
    before_state = published
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    final.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")

    failed = run_cli(git_repo, fixture, "approve", context.run_id, extra_env=context.environment)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    state = context.states.load_history_run(context.run_id)
    assert state["diagnostics"][0]["code"] == "foreign_run_pr"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_state = state
    publication_without_grant = dict(after_state["run_publication"])
    approval_grant = publication_without_grant.pop("approval_grant")
    assert publication_without_grant == before_state["run_publication"]
    assert approval_grant["pr_number"] == before_state["run_publication"]["pr_number"]
    assert after_state["action_application_receipt"]["kind"] == "approve"
    assert after_state["agent_invocation_history"] == before_state["agent_invocation_history"]

    control = TaskControlStore(context.states.root).load(TaskKey(
        context.workspace.repository_root, "example/project", 1,
    ))
    assert control is not None
    action = control["action"]
    receipt = after_state["action_application_receipt"]
    assert {key: receipt[key] for key in (
        "action_id", "kind", "payload_digest", "run_id", "executor_generation",
    )} == {
        "action_id": action["action_id"], "kind": action["kind"],
        "payload_digest": action["payload_digest"], "run_id": context.run_id,
        "executor_generation": control["executor"]["generation"],
    }
    grant = after_state["run_publication"]["approval_grant"]
    record = published["run_publication"]["record"]
    assert {key: grant[key] for key in (
        "repository", "pr_number", "head_branch", "head_sha", "base_branch", "base_sha",
    )} == {
        "repository": "example/project", "pr_number": published["run_publication"]["pr_number"],
        "head_branch": published["run_branch"], "head_sha": record["run_head_sha"],
        "base_branch": "main", "base_sha": record["default_head_sha"],
    }



def test_post_merge_readback_rejects_foreign_identity_before_parent_closeout(
    git_repo: Path,
) -> None:
    identity_error = {"base_repository": "foreign/project"}
    context = prepare_published_context(
        git_repo, delivery={"normal_merge_readback_identity_error": identity_error},
    )
    fixture = context.fixture
    published = context.states.load_history_run(context.run_id)
    assert published is not None

    run_id = published["run_id"]
    failed = run_cli(git_repo, fixture, "approve", run_id, extra_env=context.environment)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    state = context.states.load_history_run(context.run_id)
    assert state["diagnostics"][0]["code"] == "foreign_run_pr"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    assert final["state"] == "MERGED"
    assert data["parent"].get("state") != "CLOSED"
    assert data["delivery"].get("closed_issues", []) == [3]

    control = TaskControlStore(context.states.root).load(TaskKey(
        context.workspace.repository_root, "example/project", 1,
    ))
    assert control is not None
    action = control["action"]
    receipt = state["action_application_receipt"]
    assert {key: receipt[key] for key in (
        "action_id", "kind", "payload_digest", "run_id", "executor_generation",
    )} == {
        "action_id": action["action_id"], "kind": action["kind"],
        "payload_digest": action["payload_digest"], "run_id": context.run_id,
        "executor_generation": control["executor"]["generation"],
    }
    grant = state["run_publication"]["approval_grant"]
    record = published["run_publication"]["record"]
    assert {key: grant[key] for key in (
        "repository", "pr_number", "head_branch", "head_sha", "base_branch", "base_sha",
    )} == {
        "repository": "example/project", "pr_number": published["run_publication"]["pr_number"],
        "head_branch": published["run_branch"], "head_sha": record["run_head_sha"],
        "base_branch": "main", "base_sha": record["default_head_sha"],
    }
