"""Default discovery follows managed state roots, never source checkout contents."""
from pathlib import Path
import json

from agent_run.managed_workspace import ManagedWorkspace
from agent_run_kernel.query_cli import main
from tests.test_kernel_query_cli import _create, _snapshot


def test_default_discovery_does_not_walk_a_large_real_managed_clone(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = ManagedWorkspace.for_repository("example/project")
    checkout = workspace.ensure_path(str(git_repo))
    for number in range(4097):
        (checkout / "source" / str(number)).mkdir(parents=True)
    kernel = _create(workspace.state_root, repository_root=checkout)
    before = _snapshot(workspace.root)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "run-query"
    assert output["repository"] == "example/project"
    assert _snapshot(workspace.root) == before


def test_default_discovery_ignores_nonmanaged_database_but_explicit_scope_can_read_it(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    managed = _create(data / "agent-run/repositories/example/project/state", run_id="same")
    snapshot = _create(data / "agent-run/snapshots", run_id="same")
    before = _snapshot(data)
    assert main(["status", "same", "--repo", "example/project", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repository_root"] == str(managed.storage.path.parent.parent / "checkout")
    assert main(["status", "same", "--state-dir", str(snapshot.storage.path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repository_root"] == str(snapshot.storage.path.parent.parent / "checkout")
    assert _snapshot(data) == before


def test_default_discovery_keeps_cross_repository_exact_id_ambiguity(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    for repository in ("example/first", "example/second"):
        _create(data / "agent-run/repositories" / repository / "state", run_id="same", repository=repository)
    before = _snapshot(data)
    assert main(["status", "same", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"][0]["code"] == "run_locator_conflict"
    assert {item["repository"] for item in result["candidates"]} == {"example/first", "example/second"}
    assert _snapshot(data) == before


def test_default_discovery_accepts_the_existing_dot_github_repository_contract(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = ManagedWorkspace.for_repository("example/.github")
    kernel = _create(workspace.state_root, repository="example/.github")
    before = _snapshot(workspace.root)
    assert main(["status", "--repo", "example/.github", "--parent", "1", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == "run-query" and result["repository"] == "example/.github"
    assert result["repository_root"] == str(workspace.state_root / "checkout")
    assert _snapshot(workspace.root) == before
