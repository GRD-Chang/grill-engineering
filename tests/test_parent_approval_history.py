from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from agent_run.controller import Controller
from agent_run.delivery_history import history_records
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import StateStore
from conftest import write_fixture
from test_delivery import ScriptedAgents


def test_parent_approval_is_persisted_as_an_independent_history_fact(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    engine = ParentDeliveryEngine(
        git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
        agents=ScriptedAgents(states.root / "worktrees" / run_id / "parent"),
    )
    awaiting = engine.deliver(run_id)
    assert awaiting["status"] == "parent_approval_pending"
    before_approval = deepcopy(awaiting["timeline"])

    completed = engine.approve(run_id)

    assert completed["status"] == "completed"
    granted_at = completed["parent_job"]["approval_grant"]["granted_at"]
    assert any(event.get("approval_granted_at") == granted_at for event in completed["timeline"])
    nodes = history_records(completed, {"timeline": completed["timeline"]})
    approved = [node for node in nodes if node["status_text"] == "已批准"]
    assert len(approved) == 1
    assert approved[0]["ended_at"] == granted_at
    assert approved[0]["event_record"] is True
    assert not any(
        node["status_text"] == "已批准"
        for node in history_records(completed, {"timeline": before_approval})
    ), "A current approval cannot backfill earlier timeline observations"
