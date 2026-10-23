"""Default discovery follows managed state roots, never source checkout contents."""
from pathlib import Path
from dataclasses import replace
import json
import shutil

import pytest

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.contracts.task import TaskKey
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.core.kernel import Kernel
from support.offline import OfflinePort, Scenario
from agent_run.cli.main import main
from agent_run.storage.tasks import bind_task, locked_task, task_path
from tests.test_kernel_query_cli import _create, _snapshot


def _published(root: Path, run_id: str = "run-query", *, repository: str = "example/project",
               repository_root: Path | None = None, parent: int = 1) -> Kernel:
    delivery = Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "requirement", Commit("base", "tree"),
                        "refs/heads/parent", "refs/heads/main")
    spec = RunSpec(run_id, repository, parent, "parent", "graph", (delivery,), language="en",
                   repository_root=str(repository_root or root.parent / "repository"))
    return Kernel.create_task(root / run_id / "kernel.sqlite3", spec,
                              requirements=Requirements(ParentIssue(parent, "Parent", "Requirement"), ()))


def test_default_discovery_does_not_walk_a_large_real_managed_clone(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = ManagedWorkspace.for_repository("example/project")
    checkout = workspace.ensure_path(str(git_repo))
    for number in range(4097):
        (checkout / "source" / str(number)).mkdir(parents=True)
    kernel = _published(workspace.state_root, repository_root=checkout)
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
    managed = _published(data / "agent-run/repositories/example/project/state", run_id="same")
    snapshot = _create(data / "agent-run/snapshots", run_id="same")
    before = _snapshot(data)
    assert main(["status", "same", "--repo", "example/project", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repository_root"] == str(ManagedWorkspace.for_repository("example/project").repository_root)
    assert main(["status", "same", "--state-dir", str(snapshot.storage.path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repository_root"] == str(snapshot.storage.path.parent.parent / "checkout")
    assert _snapshot(data) == before


def test_default_discovery_keeps_cross_repository_exact_id_ambiguity(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    for repository in ("example/first", "example/second"):
        _published(data / "agent-run/repositories" / repository / "state", run_id="same", repository=repository)
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
    kernel = _published(workspace.state_root, repository="example/.github")
    before = _snapshot(workspace.root)
    assert main(["status", "--repo", "example/.github", "--parent", "1", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == "run-query" and result["repository"] == "example/.github"
    assert result["repository_root"] == str(workspace.repository_root)
    assert _snapshot(workspace.root) == before


def test_default_discovery_ignores_unpublished_creation_but_exact_snapshot_stays_read_only(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    root = ManagedWorkspace.for_repository("example/project").state_root
    published = _published(root, "published")
    orphan = _create(root, "unpublished")
    shell = root / "failed-creation" / "kernel.sqlite3"
    shell.parent.mkdir()
    shell.touch()
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "published"
    assert main(["runs", "--repo", "example/project", "--json"]) == 0
    assert [item["run_id"] for item in json.loads(capsys.readouterr().out)["runs"]] == ["published"]
    assert main(["status", "unpublished", "--state-dir", str(orphan.storage.path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "unpublished"
    assert main(["status", "--state-dir", str(shell), "--json"]) == 2
    diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert diagnostic["code"] == "unsupported_state"
    assert diagnostic["path"] == str(shell) and diagnostic["reason"] == "uncertain_state"
    assert published.storage.path.exists() and _snapshot(data) == before


@pytest.mark.parametrize("missing", ["database", "state-directory"])
def test_default_discovery_diagnoses_a_missing_published_database_without_repair(
    tmp_path: Path, monkeypatch, capsys, missing: str,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    root = ManagedWorkspace.for_repository("example/project").state_root
    published = _published(root, "published")
    if missing == "database":
        published.storage.path.unlink()
    else:
        shutil.rmtree(root)
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    diagnostic = output["diagnostics"][0]
    assert diagnostic["code"] == "unsupported_state"
    assert diagnostic["path"] == str(published.storage.path)
    assert diagnostic["reason"] == "unreadable_state" and diagnostic["recovery_hint"]
    assert output["selection"] == {"repo": "example/project", "parent": 1}
    assert output["candidates"][0]["run_id"] == "published"
    assert not published.storage.path.exists() and _snapshot(data) == before


@pytest.mark.parametrize("fault", ["malformed", "symbolic"])
@pytest.mark.parametrize("scope", ["other-repository", "other-parent"])
def test_default_discovery_isolates_an_unrelated_invalid_task_pointer(
    tmp_path: Path, monkeypatch, capsys, fault: str, scope: str,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    _published(ManagedWorkspace.for_repository("example/project").state_root, "published")
    other_repository = "example/other" if scope == "other-repository" else "example/project"
    other_parent = 1 if scope == "other-repository" else 9
    other = ManagedWorkspace.for_repository(other_repository)
    _published(other.state_root, "other", repository=other_repository, parent=other_parent)
    pointer = task_path(TaskKey(other.repository_root, other_repository, other_parent))
    if fault == "malformed":
        pointer.write_bytes(b"{")
    else:
        pointer.unlink()
        replacement = tmp_path / "unrelated-pointer.json"
        replacement.write_bytes(b"{}")
        pointer.symlink_to(replacement)
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "published"
    assert main(["runs", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    assert [item["run_id"] for item in json.loads(capsys.readouterr().out)["runs"]] == ["published"]
    assert _snapshot(data) == before


@pytest.mark.parametrize("fault", ["malformed", "symbolic"])
def test_default_discovery_diagnoses_its_exact_task_pointer_without_repair(
    tmp_path: Path, monkeypatch, capsys, fault: str,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    root = ManagedWorkspace.for_repository("example/project").state_root
    _published(root, "published")
    pointer = task_path(TaskKey(root.parent / "repository", "example/project", 1))
    if fault == "malformed":
        pointer.write_bytes(b"{")
    else:
        pointer.unlink()
        replacement = tmp_path / "invalid-pointer.json"
        replacement.write_bytes(b"{}")
        pointer.symlink_to(replacement)
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] in {"task_location_invalid", "run_locator_invalid"}
    assert _snapshot(data) == before


def test_unpublished_shells_do_not_consume_the_default_run_candidate_limit(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    root = ManagedWorkspace.for_repository("example/project").state_root
    _published(root, "published")
    for number in range(257):
        shell = root / f"failed-{number}" / "kernel.sqlite3"
        shell.parent.mkdir()
        shell.touch()
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "published"
    assert _snapshot(data) == before


def test_default_parent_selects_the_exact_published_run_and_retains_terminal_history(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    workspace = ManagedWorkspace.for_repository("example/repository")
    root = workspace.state_root
    original_create = OfflinePort.create

    def managed_port(path, *args, **kwargs):
        port = original_create(path, *args, **kwargs)
        # Place the fresh physical Git before prepare freezes its identities.
        port.git.root.rename(workspace.repository_root)
        port.git.root = workspace.repository_root
        return port

    with monkeypatch.context() as creation:
        creation.setattr(OfflinePort, "create", staticmethod(managed_port))
        scenario = Scenario(root / "historical", DeliveryKind.PARENT_ONLY)
    assert Path(scenario.spec.repository_root) == scenario.port.git.root == workspace.repository_root
    assert scenario.run(approve=True).kind == "complete"
    active = _published(root, "published", repository="example/repository")
    before = _snapshot(data)
    assert main(["status", "--repo", "example/repository", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "published"
    assert main(["runs", "--repo", "example/repository", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert {item["run_id"]: item["is_terminal"] for item in output["runs"]} == {
        "offline-run": True, "published": False}
    assert main(["history", "offline-run", "--repo", "example/repository", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "offline-run"
    assert main(["status", "--repo", "example/repository", "--parent", "1",
                 "--data-root", str(root), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_conflict"
    assert active.storage.path.exists() and _snapshot(data) == before
    task = TaskKey(workspace.repository_root, "example/repository", 1)
    duplicate = root / "history-copy" / "kernel.sqlite3"
    duplicate.parent.mkdir()
    shutil.copyfile(scenario.kernel.storage.path, duplicate)
    # A completed Run's original publication remains an exact fact even when
    # a copied historical database has the same Run ID. Use the formal writer.
    with locked_task(task) as pointer:
        bind_task(pointer, scenario.spec.run_id, scenario.kernel.storage.path)
    before = _snapshot(data)
    assert main(["status", "--repo", "example/repository", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "offline-run"
    assert _snapshot(data) == before
    pointer.unlink()
    before = _snapshot(data)
    assert main(["status", "--repo", "example/repository", "--parent", "1", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_conflict"
    assert _snapshot(data) == before


@pytest.mark.parametrize("fault", ["damaged", "missing"])
@pytest.mark.parametrize("selector", ["parent", "exact"])
def test_exact_default_query_isolates_another_parents_bound_database_failure(
    tmp_path: Path, monkeypatch, capsys, fault: str, selector: str,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    root = ManagedWorkspace.for_repository("example/project").state_root
    _published(root, "published")
    other = _published(root, "other", parent=9)
    if fault == "damaged":
        other.storage.path.write_bytes(b"not a SQLite database")
    else:
        other.storage.path.unlink()
    before = _snapshot(data)
    arguments = ["--parent", "1"] if selector == "parent" else ["published"]
    assert main(["status", *arguments, "--repo", "example/project", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "published"
    assert main(["status", "--parent", "9", "--repo", "example/project", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    diagnostic = output["diagnostics"][0]
    assert diagnostic["code"] == "unsupported_state"
    assert diagnostic["path"] == str(other.storage.path)
    assert diagnostic["reason"] == ("uncertain_state" if fault == "damaged" else "unreadable_state")
    assert diagnostic["recovery_hint"] and output["selection"] == {"repo": "example/project", "parent": 9}
    assert output["candidates"][0]["run_id"] == "other"
    assert main(["runs", "--repo", "example/project", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["path"] == str(other.storage.path)
    assert _snapshot(data) == before
