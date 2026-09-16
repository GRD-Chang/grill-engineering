from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.task_control import TaskControlStore, TaskKey
from conftest import seed_idle_control, seed_run, write_fixture
from test_cli import run_cli, issue, stdout_json


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("inherited_git_location", [False, True])
def test_run_does_not_write_user_checkout(
    git_repo: Path, tmp_path: Path, inherited_git_location: bool,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": issue(2, labels=[])},
        repository="Example/Project",
    ).rename(tmp_path / "github.json")
    (git_repo / "README.md").write_text("user's unfinished edit\n")
    (git_repo / "private-note.txt").write_text("untracked user file\n")
    before = _files(git_repo)

    environment = {
        "GIT_DIR": str(git_repo / ".git"),
        "GIT_WORK_TREE": str(git_repo),
        "GIT_INDEX_FILE": str(git_repo / ".git" / "index"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(git_repo),
    } if inherited_git_location else {}
    result = run_cli(git_repo, fixture, "run", "1", extra_env=environment)

    assert result.returncode == 2, result.stderr
    assert stdout_json(result)["status"] == "progress_exhausted"
    assert _files(git_repo) == before


def test_two_clones_and_outside_repo_commands_reuse_one_task(
    git_repo: Path, tmp_path: Path,
) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/project.git"],
        cwd=git_repo, check=True, capture_output=True,
    )
    second_clone = tmp_path / "second-clone"
    subprocess.run(
        ["git", "clone", "--no-local", str(git_repo), str(second_clone)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/example/project.git"],
        cwd=second_clone, check=True, capture_output=True,
    )
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": issue(2, labels=[])}
    ).rename(tmp_path / "github.json")
    (git_repo / "README.md").write_text("first user's unfinished edit\n")
    (second_clone / "private-note.txt").write_text("second user's untracked note\n")
    originals = {repo: _files(repo) for repo in (git_repo, second_clone)}
    outside = tmp_path / "outside"
    outside.mkdir()
    started = run_cli(git_repo, fixture, "run", "1")
    assert started.returncode == 2, started.stderr
    run_id = stdout_json(started)["run_id"]

    from_second_clone = run_cli(second_clone, fixture, "run", "1")
    assert from_second_clone.returncode == 2, from_second_clone.stderr
    assert stdout_json(from_second_clone)["run_id"] == run_id
    inferred_status = subprocess.run(
        [sys.executable, "-m", "agent_run", "status", "--parent", "1", "--json"],
        cwd=second_clone,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True, capture_output=True, check=False,
    )
    assert inferred_status.returncode == 0, inferred_status.stderr
    assert stdout_json(inferred_status)["run_id"] == run_id
    for command in ("status", "run", "resume"):
        if command == "resume":
            stopped = run_cli(outside, fixture, "stop", "1", "--repo", "example/project")
            assert stopped.returncode == 0, stopped.stderr
            assert stdout_json(stopped)["status"] == "operator_stopped"
        selector = ("--parent", "1") if command == "status" else ("1",)
        result = run_cli(
            outside, fixture, command, *selector, "--repo", "example/project", "--json",
        )
        assert result.returncode == (0 if command == "status" else 2), result.stderr
        output = stdout_json(result)
        assert output.get("run_id") == run_id, f"{command}: {json.dumps(output, ensure_ascii=False)}"

    workspace = ManagedWorkspace.for_repository("example/project")
    assert [path.stem for path in (workspace.state_root / "runs").glob("*.json")] == [run_id]
    assert all(_files(repo) == before for repo, before in originals.items())
    assert not list(outside.iterdir())


def test_different_parents_keep_independent_unfinished_tasks(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixtures: dict[int, Path] = {}
    for parent, ticket in ((1, 2), (10, 11)):
        fixtures[parent] = write_fixture(
            git_repo / f"github-{parent}.json",
            issues={str(ticket): issue(ticket, labels=[])},
            parent={
                "number": parent,
                "title": f"Parent {parent}",
                "body": "Deliver this independent task.",
                "sub_issues": [ticket],
                "sub_issue_order_reliable": True,
            },
        ).rename(tmp_path / f"github-{parent}.json")
    before = _files(git_repo)
    run_ids: dict[int, str] = {}

    for parent, fixture in fixtures.items():
        result = run_cli(git_repo, fixture, "run", str(parent))
        assert result.returncode == 2, result.stderr
        output = stdout_json(result)
        assert output["status"] == "progress_exhausted"
        run_ids[parent] = output["run_id"]

    assert len(set(run_ids.values())) == 2
    workspace = ManagedWorkspace.for_repository("example/project")
    assert {path.stem for path in (workspace.state_root / "runs").glob("*.json")} == set(run_ids.values())
    for parent, fixture in fixtures.items():
        status = run_cli(
            tmp_path, fixture, "status", "--repo", "example/project", "--parent", str(parent), "--json",
        )
        assert status.returncode == 0, status.stderr
        output = stdout_json(status)
        assert output["run_id"] == run_ids[parent]
        assert output["parent"]["number"] == parent
        assert output["status"] == "progress_exhausted"
    assert _files(git_repo) == before


def test_state_override_cannot_write_into_user_repository(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": issue(2, labels=[])}
    ).rename(tmp_path / "github.json")
    override = git_repo / ".agent-run"
    before = _files(git_repo)

    result = run_cli(git_repo, fixture, "run", "1", "--state-dir", str(override))

    assert result.returncode == 2, result.stderr
    output = stdout_json(result)
    assert any("统一状态目录" in item["message"] for item in output["diagnostics"])
    assert not override.exists()
    assert _files(git_repo) == before
    workspace = ManagedWorkspace.for_repository("example/project")
    assert not list((workspace.state_root / "runs").glob("*.json"))


@pytest.mark.parametrize("command", ["stop", "abandon"])
def test_control_rejects_a_record_bound_to_external_state(
    git_repo: Path, tmp_path: Path, command: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = seed_run(git_repo, fixture)
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    workspace = ManagedWorkspace.for_repository("example/project")
    external = tmp_path / "external-state"
    external.mkdir()
    (external / "untouched.txt").write_text("user-owned state\n")
    seed_idle_control(
        TaskControlStore(workspace.state_root),
        TaskKey(workspace.repository_root, "example/project", 1),
        run_id,
        state_dir=external,
    )
    snapshots = {path: _files(path) for path in (git_repo, workspace.state_root, external)}

    result = run_cli(git_repo, fixture, command, run_id)

    assert result.returncode == 2, result.stderr
    assert "统一状态目录" in result.stdout
    assert all(_files(path) == before for path, before in snapshots.items())
