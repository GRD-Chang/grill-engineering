"""Final PR 字段组合与应用入口：外部响应可控，身份、Git、落盘保持真实。"""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.git import GitRepository
from agent_run.run_publication import RunPublicationEngine
from agent_run.run_publication_shared import RunPublicationShared
from agent_run.state import StateStore
from support.published_run import prepare_accepted_context, prepare_published_context
from support.run_publication import InvocationRunPublicationAgents
from support.state import persist_run_fixture, snapshot_run
from support.seeding import write_fixture


@pytest.mark.parametrize("override", [
    {}, {"head_repository": "foreign/project"}, {"base_repository": "foreign/project"},
    {"head_branch": "foreign-ref"}, {"head_sha": "c" * 40},
    {"base_branch": "release"}, {"base_sha": "d" * 40},
    {"head_repository": "foreign/project", "head_branch": "foreign-ref"},
], ids=["valid", "head-repository", "base-repository", "head-ref", "head-sha",
        "base-ref", "base-sha", "repository-and-ref"])
def test_identity_fields(git_repo: Path, override: dict[str, str]) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    owner = RunPublicationShared(
        git=git, states=StateStore(git_repo / ".agent-run"),
        agents=InvocationRunPublicationAgents(),
        github=FixtureGitHubPublisher(fixture, git),
        default_branch="main", default_head_sha="b" * 40,
    )
    live = {
        "head_repository": "example/project", "base_repository": "example/project",
        "head_branch": "agent-run/reviewed", "head_sha": "a" * 40,
        "base_branch": "main", "base_sha": "b" * 40,
    } | override
    arguments = dict(live=live, run_head="a" * 40, branch="agent-run/reviewed",
                     repository="example/project", expected_base_sha="b" * 40)
    if override:
        with pytest.raises(GitHubReadError) as rejected:
            owner._require_final_pr_identity(**arguments)
        assert rejected.value.code == "foreign_run_pr"
    else:
        owner._require_final_pr_identity(**arguments)


@pytest.mark.parametrize(("operation", "identity_error"), [
    ("recovery", {"head_repository": "foreign/project"}),
    ("recovery", {"head_ref": "foreign-run-ref"}),
    ("approve", {"head_repository": "foreign/project"}),
    ("approve", {"head_ref": "foreign-run-ref"}),
    ("readback", {"head_repository": "foreign/project"}),
    ("readback", {"head_ref": "foreign-run-ref"}),
    ("readback", {"base_repository": "foreign/project"}),
], ids=["recovery-repository", "recovery-ref", "approve-repository", "approve-ref",
        "readback-head-repository", "readback-ref", "readback-base-repository"])
def test_identity_application_rejects_before_unauthorized_effects(
    git_repo: Path, operation: str, identity_error: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = prepare_published_context(git_repo)
    before = context.states.load_history_run(context.run_id)
    assert before is not None
    data = json.loads(context.fixture.read_text())
    if operation == "readback":
        data["delivery"]["normal_merge_readback_identity_error"] = identity_error
    else:
        final = next(pull for pull in data["delivery"]["pull_requests"]
                     if pull.get("scope") == "final_run")
        final.update(identity_error)
    context.fixture.write_text(json.dumps(data))
    expected_delivery = deepcopy(data["delivery"])
    if operation == "recovery":
        before.update(status="waiting_external", terminal_kind="waiting_external")
        before["run_publication"]["phase"] = "waiting_external"
        persist_run_fixture(context.states, context.run_id, before)
    # 独立落盘读取，不拿 Engine 返回的工作投影充当不变性证据。
    snapshot = snapshot_run((context.states.root, context.run_id))
    publisher = FixtureGitHubPublisher(context.fixture, context.git)
    writes: list[str] = []
    for name in ("normal_merge", "close_parent_issue", "ensure_run_pr", "publish_branch",
                 "record_agent_run_status"):
        real = getattr(publisher, name)

        def record(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            writes.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(publisher, name, record)
    agents = InvocationRunPublicationAgents()
    engine = RunPublicationEngine(
        git=context.git, states=context.states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=context.git.resolve("main"),
    )
    with pytest.raises(GitHubReadError) as rejected:
        if operation == "recovery":
            engine.publish(context.run_id)
        else:
            engine.approve(context.run_id)
    assert rejected.value.code == "foreign_run_pr"
    after = StateStore(context.workspace.state_root).load_history_run(context.run_id)
    assert after is not None
    assert after["agent_invocation_history"] == before["agent_invocation_history"]
    assert agents.requests == []
    assert after.get("action_application_receipt") == before.get("action_application_receipt")
    if operation == "recovery":
        assert snapshot_run((context.states.root, context.run_id)) == snapshot
        assert writes == []
    else:
        grant = after["run_publication"]["approval_grant"]
        expected = before["run_publication"]["record"]
        assert {key: grant[key] for key in (
            "repository", "pr_number", "head_branch", "head_sha", "base_branch", "base_sha",
        )} == {
            "repository": before["repository"],
            "pr_number": before["run_publication"]["pr_number"],
            "head_branch": before["run_branch"], "head_sha": expected["run_head_sha"],
            "base_branch": "main", "base_sha": expected["default_head_sha"],
        }
        without_grant = deepcopy(after["run_publication"])
        without_grant.pop("approval_grant")
        # readback 有真实 merge intent；其余身份拒绝只允许准确批准落盘。
        if operation == "approve":
            assert without_grant == before["run_publication"]
            expected_state = deepcopy(before)
            expected_state["run_publication"]["approval_grant"] = grant
            assert len(after["timeline"]) == len(before["timeline"]) + 1
            approval_event = {
                key: value for key, value in before["timeline"][-1].items()
                if key not in {"at", "required_checks_evidence"}
            }
            approval_event.update(
                at=after["timeline"][-1]["at"], approval_granted_at=grant["granted_at"],
            )
            expected_state["timeline"].append(approval_event)
            assert after == expected_state
            assert writes == []
        else:
            assert after["run_publication"]["merge_intent"]["attempts"] == 1
            assert writes == ["record_agent_run_status", "normal_merge"]
    durable = json.loads(context.fixture.read_text())
    assert durable["parent"].get("state") != "CLOSED"
    assert durable["delivery"]["closed_issues"] == [3]
    if operation == "readback":
        final = next(pull for pull in durable["delivery"]["pull_requests"]
                     if pull.get("scope") == "final_run")
        assert final["state"] == "MERGED"
        merge = context.git.resolve("main")
        assert context.git.commit_parents(merge) == [
            before["run_publication"]["record"]["default_head_sha"],
            before["run_publication"]["record"]["run_head_sha"],
        ]
    else:
        assert durable["delivery"] == expected_delivery


@pytest.mark.parametrize(
    ("base_branch", "head_sha", "identity_error"),
    [
        ("main", "f" * 40, {}),
        ("release", None, {}),
        ("main", None, {"head_repository": "foreign/project"}),
        ("main", None, {"head_ref": "foreign-run-ref"}),
    ],
    ids=("head", "base", "head-repository", "head-ref"),
)
def test_final_run_pr_drift_returns_to_fresh_acceptance_without_rewriting_pr(
    git_repo: Path,
    base_branch: str,
    head_sha: str | None,
    identity_error: dict[str, str],
) -> None:
    # Ticket delivery is historical setup; this test exercises Final PR publication.
    context = prepare_accepted_context(git_repo)
    fixture = context.fixture
    state = context.states.load_history_run(context.run_id)
    assert state is not None
    run_id = str(state["run_id"])
    assert state["status"] == "run_publication_pending"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    actual_head = state["run_acceptance"]["acceptance_record"]["reviewed_head_sha"]
    if base_branch != "main":
        subprocess.run(["git", "branch", base_branch, "main"], cwd=context.git.root, check=True)
    data.setdefault("delivery", {}).setdefault("pull_requests", []).append(
        {
            "number": 99,
            "branch": state["run_branch"],
            "base_branch": base_branch,
            "state": "OPEN",
            "scope": "final_run",
            "title": "preserve this title",
            "body": "preserve this body",
            "head_sha": head_sha or actual_head,
            **identity_error,
        }
    )
    data["delivery"].setdefault("published_branches", {})[state["run_branch"]] = (
        head_sha or actual_head
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")
    final_agents = git_repo / "final-agents.json"
    final_agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the completed delivery",
                        "pr_title": "feat(run): publish the completed delivery",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe completed Ticket needs one review boundary.\n\n"
                            "## Why This Change Was Made\n\nThe Run branch keeps the standard delivery route.\n\n"
                            "## User Impact\n\nMaintainers can approve the complete Parent delivery.\n\n"
                            "## Evidence\n\nThe independent expected-merge review passed."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    engine = RunPublicationEngine(
        git=context.git, states=context.states, agents=FixtureAgentBackend(final_agents),
        github=FixtureGitHubPublisher(fixture, context.git),
        default_branch="main", default_head_sha=context.git.resolve("main"),
    )
    with pytest.raises(GitHubReadError) as rejected:
        engine.publish(run_id)
    assert rejected.value.code == "foreign_run_pr"
    preserved = next(
        pull
        for pull in json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
        if pull["number"] == 99
    )
    assert preserved["title"] == "preserve this title"
    assert preserved["body"] == "preserve this body"
