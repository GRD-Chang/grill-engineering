from __future__ import annotations

from support.run_scenarios import _accepted_run, _completed_run

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_run.agent_profiles import AgentProfileStore
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.run_locator import RunLocatorIndex
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.seeding import seed_idle_control
from support.workspace import prepare_workspace
from support.run_publication import InvocationRunPublicationAgents


@dataclass(frozen=True)
class FinalRunContext:
    """本例的准确位置与引用；不缓存状态、Git 安全或授权结论。"""

    workspace: ManagedWorkspace
    git: GitRepository
    states: StateStore
    fixture: Path
    run_id: str
    environment: dict[str, str]


def prepare_completed_run(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """准备历史 Ticket 已交付、尚未进行 Run Acceptance 的真实 CLI 起点。"""
    workspace = prepare_workspace(repo)
    state, _, _ = _completed_run(
        workspace.repository_root, ticket_number=ticket_number,
        state_root=workspace.state_root,
    )
    return _register_historical_run(workspace, state, ticket_number, delivery)


def _prepare_accepted_context(
    repo: Path, *, ticket_number: int, delivery: dict[str, Any] | None,
) -> tuple[FinalRunContext, dict[str, Any]]:
    workspace = prepare_workspace(repo)
    state, states, git, _ = _accepted_run(
        workspace.repository_root, ticket_number=ticket_number,
        state_root=workspace.state_root,
    )
    fixture, state = _register_historical_run(
        workspace, state, ticket_number, delivery, git=git, states=states,
    )
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, environment.get("PYTHONPATH"))))
    return FinalRunContext(
        workspace, git, states, fixture, str(state["run_id"]), environment,
    ), state


def prepare_accepted_context(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> FinalRunContext:
    """Final Publication 前的有限前置；后续直接消费准确引用。"""
    context, _ = _prepare_accepted_context(repo, ticket_number=ticket_number, delivery=delivery)
    return context


def prepare_accepted_run(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """准备已验收 Run；保留已有需要初始状态的调用方。"""
    context, state = _prepare_accepted_context(repo, ticket_number=ticket_number, delivery=delivery)
    return context.fixture, state


def _register_historical_run(
    workspace: ManagedWorkspace, state: dict[str, Any], ticket_number: int,
    delivery: dict[str, Any] | None, *,
    git: GitRepository | None = None, states: StateStore | None = None,
) -> tuple[Path, dict[str, Any]]:
    """登记同一历史 Ticket PR、Locator 和 idle ownership，不推进被测动作。"""
    git = git if git is not None else workspace.open()
    run_id = str(state["run_id"])
    states = states if states is not None else StateStore(workspace.state_root)
    complete = states.load_current_run(run_id)
    assert complete is not None
    assert complete == state
    assert complete["repository"].lower() == workspace.repository
    # 身份由真实 Controller.start 固定；登记只复用已知引用。
    # test_published_run_scenarios 独立重开验证当前 Git 身份。
    # Controller.start in the historical scenario already initialized this
    # profile. Registration must preserve that configuration and Run state.
    assert AgentProfileStore(workspace.state_root).load(run_id) is not None
    fixture = workspace.repository_root / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).update(delivery or {})
    data["delivery"]["closed_issues"] = [ticket_number]
    integration = state["ticket_jobs"][str(ticket_number)]["deterministic_integration_record"]
    pulls = data["delivery"].setdefault("pull_requests", [])
    # FixtureGitHubPublisher allocates len(pulls) + 1. Preserve the historical
    # Ticket PR so the Final PR cannot reuse its integration identity.
    assert integration["pr_number"] == integration["pr"]["number"] == len(pulls) + 1
    pulls.append({
        **integration["pr"],
        "scope": "ticket", "state": "MERGED", "primary_ticket": ticket_number,
        "branch": f"agent-run/{state['run_id']}/ticket-{ticket_number}",
        "base_branch": state["run_branch"],
        "head_repository": state["repository"], "base_repository": state["repository"],
        "head_sha": integration["publication_sha"], "base_sha": integration["base_sha"],
        "integrated_sha": integration["integrated_sha"],
        "title": f"Completed Ticket #{ticket_number}", "body": "Accepted historical delivery.",
    })
    fixture.write_text(json.dumps(data), encoding="utf-8")

    RunLocatorIndex.default().register(
        run_id=run_id, repository_root=workspace.repository_root,
        state_dir=workspace.state_root, repository=str(complete["repository"]),
        parent_number=int(complete["parent"]["number"]),
    )
    seed_idle_control(
        TaskControlStore(workspace.state_root),
        TaskKey(
            workspace.repository_root, str(complete["repository"]),
            int(complete["parent"]["number"]),
        ),
        run_id, state_dir=workspace.state_root,
    )
    return fixture, complete


def prepare_published_context(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> FinalRunContext:
    """一次准备真实 Final PR，后续消费者直接使用本例引用与子进程环境。"""
    context, state = _prepare_accepted_context(
        repo, ticket_number=ticket_number, delivery=delivery,
    )
    git, states, fixture = context.git, context.states, context.fixture
    published = RunPublicationEngine(
        git=git, states=states,
        agents=InvocationRunPublicationAgents(),
        github=FixtureGitHubPublisher(fixture, git),
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert published["status"] == "run_approval_pending"
    historical_pr = state["ticket_jobs"][str(ticket_number)]["deterministic_integration_record"]["pr_number"]
    assert published["run_publication"]["pr_number"] != historical_pr
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
    assert len({pull["number"] for pull in pulls}) == len(pulls)
    return context


def prepare_published_run(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """兼容需要初始落盘状态的现有消费者。"""
    context = prepare_published_context(repo, ticket_number=ticket_number, delivery=delivery)
    complete = context.states.load_current_run(context.run_id)
    assert complete is not None
    return context.fixture, complete
