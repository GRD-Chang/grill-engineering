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


def test_final_context_cli_preserves_source_and_exact_run_with_inherited_pythonpath(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.published_run import prepare_accepted_context
    from support.state import snapshot_run

    # 完整环境快照覆盖 run_cli 的临时 PYTHONPATH 时仍须加载当前源码。
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "unrelated-pythonpath"))
    context = prepare_accepted_context(git_repo)
    before = snapshot_run((context.states.root, context.run_id))
    result = run_cli(
        tmp_path, context.fixture, "status", context.run_id, "--json",
        extra_env=context.environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["run_id"] == context.run_id
    assert context.states.list_run_ids() == [context.run_id]
    assert snapshot_run((context.states.root, context.run_id)) == before


def test_final_context_does_not_authorize_replaced_workspace(
    git_repo: Path, tmp_path: Path,
) -> None:
    import subprocess

    from agent_run.managed_workspace import ManagedWorkspaceError
    from support.control_state import snapshot_control
    from support.published_run import prepare_accepted_context
    from support.state import snapshot_run

    context = prepare_accepted_context(git_repo)
    control = TaskControlStore(context.states.root)
    task = TaskKey(context.git.root, "example/project", 1)
    before = snapshot_run((context.states.root, context.run_id))
    before_control = snapshot_control(control, task)
    # Context 已准备好之后替换 origin；生产重开不能继承先前的安全结论。
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "replacement")],
        cwd=context.git.root, check=True,
    )
    with pytest.raises(ManagedWorkspaceError, match="Git"):
        context.workspace.open()
    failed = run_cli(
        tmp_path, context.fixture, "run", "1", "--repo", "example/project",
        extra_env=context.environment,
    )
    assert failed.returncode == 2, failed.stdout + failed.stderr
    assert "Git" in stdout_json(failed)["diagnostics"][0]["message"]
    assert snapshot_run((context.states.root, context.run_id)) == before
    assert snapshot_control(control, task) == before_control
