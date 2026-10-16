"""历史 Run 登记保留真实身份、Profile、idle ownership 和公开选择器。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_profiles import AgentProfileStore
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.cli import run_cli, stdout_json
from support.published_run import (
    _register_historical_run,
    prepare_accepted_run,
    prepare_completed_run,
    prepare_published_run,
)
from support.run_scenarios import _completed_run
from support.workspace import managed_workspace, prepare_workspace


@pytest.mark.parametrize(
    ("prepare", "expected_status"),
    [
        (prepare_completed_run, "run_acceptance_pending"),
        (prepare_accepted_run, "run_publication_pending"),
        (prepare_published_run, "run_approval_pending"),
    ],
    ids=("completed", "accepted", "published"),
)
def test_historical_run_registration_preserves_state_and_exact_selectors(
    git_repo: Path,
    tmp_path: Path,
    prepare: Callable[..., tuple[Path, dict[str, Any]]],
    expected_status: str,
) -> None:
    fixture, state = prepare(git_repo, ticket_number=7)
    workspace = managed_workspace(git_repo)
    git = workspace.open()
    states = StateStore(workspace.state_root)
    run_id = str(state["run_id"])
    assert state["status"] == expected_status
    assert states.load_current_run(run_id) == state
    assert states.find_run("example/project", 1) == state
    assert fixture == workspace.repository_root / "github.json"
    assert workspace.repository_root != git_repo
    assert state["checkout_identity"] == git.checkout_identity()
    historical = state["ticket_jobs"]["7"]["deterministic_integration_record"]
    assert historical["integrated_sha"] == git.resolve(str(state["run_branch"]))

    profiles = AgentProfileStore(workspace.state_root)
    profile = profiles.load(run_id)
    assert profile is not None
    assert profile["run_id"] == run_id
    assert profile["profile_revision"] == 1
    locator = RunLocatorIndex.default()
    assert locator.resolve_state_dir(run_id) == workspace.state_root
    entry, = locator.entries()
    assert entry["run_id"] == run_id
    assert entry["repository"] == "example/project"
    assert entry["parent_number"] == 1
    assert entry["repository_root"] == str(workspace.repository_root)
    assert entry["state_dir"] == str(workspace.state_root)
    locator_before = locator.path.read_bytes()

    control = TaskControlStore(workspace.state_root)
    task = TaskKey(workspace.repository_root, "example/project", 1)
    ownership = control.load(task)
    assert ownership is not None
    assert ownership["task"] == task.identity
    assert ownership["run_id"] == run_id
    assert ownership["run_state_dir"] == str(workspace.state_root)
    assert ownership["next_generation"] == 1
    assert ownership["action"] is ownership["executor"] is None
    assert ownership["action_history"] == []

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    for command, selector in (
        ("status", (run_id,)),
        ("history", ("--repo", "example/project", "--parent", "1")),
    ):
        queried = run_cli(elsewhere, fixture, command, *selector, "--json")
        assert queried.returncode == 0, queried.stdout + queried.stderr
        assert stdout_json(queried)["run_id"] == run_id
    assert states.load_current_run(run_id) == state
    assert control.load(task) == ownership
    assert profiles.load(run_id) == profile
    assert locator.path.read_bytes() == locator_before
    assert not (elsewhere / ".agent-run").exists()


def test_historical_registration_preserves_an_existing_profile_revision(git_repo: Path) -> None:
    workspace = prepare_workspace(git_repo)
    state, states, _git = _completed_run(
        workspace.repository_root, ticket_number=7, state_root=workspace.state_root,
    )
    run_id = str(state["run_id"])
    profiles = AgentProfileStore(workspace.state_root)
    configured = profiles.configure(run_id, preset="premium")
    assert configured["profile_revision"] == 2
    _, registered = _register_historical_run(workspace, state, 7, None)
    assert registered == states.load_current_run(run_id) == state
    assert profiles.load(run_id) == configured
