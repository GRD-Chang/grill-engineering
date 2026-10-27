"""Default discovery follows managed state roots, never source checkout contents."""

from support.repository_fixture import create_kernel
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
from agent_run.storage.tasks import task_path
from agent_run.storage.routing import RepositoryRoute
from tests.test_kernel_query_cli import _create
from support.environment import durable_snapshot as _snapshot


def _published(root: Path, run_id: str = "run-query", *, repository: str = "example/project",
               repository_root: Path | None = None, parent: int = 1) -> Kernel:
    delivery = Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "requirement", Commit("base", "tree"),
                        "refs/heads/parent", "refs/heads/main")
    spec = RunSpec(run_id, repository, parent, "parent", "graph", (delivery,), language="en",
                   repository_root=str(repository_root or (ManagedWorkspace.for_repository(repository).repository_root
                       if root == ManagedWorkspace.for_repository(repository).state_root else root.parent / "repository")))
    from support.offline import fixture_inputs
    spec = replace(spec, inputs=fixture_inputs())
    return create_kernel(root / "kernel.sqlite3", spec,
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
    assert json.loads(capsys.readouterr().out)["repository_root"] == str(snapshot.storage.path.parent / "checkout")
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


def test_default_discovery_ignores_unpublished_creation_but_exact_snapshot_stays_read_only(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    workspace = ManagedWorkspace.for_repository("example/project")
    published = _published(workspace.state_root, "published")
    # A competing proposal for the same active Task reuses the original complete Run.
    from support.offline import fixture_inputs
    delivery = Delivery('other-subject', DeliveryKind.PARENT_ONLY, 1, 'unread', Commit('base', 'tree'), 'source', 'target')
    proposed = RunSpec('unpublished', workspace.repository, 1, 'new', 'new', (delivery,), language='zh',
        repository_root=str(workspace.repository_root), inputs=fixture_inputs())
    winner = create_kernel(published.storage.path, proposed, requirements=Requirements(ParentIssue(1, 'Changed', 'Changed'), ()))
    assert winner.storage.run_id == 'published' and winner.storage.path == published.storage.path
    shell = workspace.state_root / 'failed-creation' / 'kernel.sqlite3'
    shell.parent.mkdir()
    shell.touch()
    before = _snapshot(data)
    assert main(['status', '--repo', workspace.repository, '--parent', '1', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == 'published'
    assert main(['runs', '--repo', workspace.repository, '--json']) == 0
    assert [item['run_id'] for item in json.loads(capsys.readouterr().out)['runs']] == ['published']
    assert main(['status', 'unpublished', '--state-dir', str(winner.storage.path), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'run_locator_missing'
    assert main(['status', 'unpublished', '--state-dir', str(shell), '--json']) == 2
    diagnostic = json.loads(capsys.readouterr().out)['diagnostics'][0]
    assert diagnostic['code'] == 'unsupported_state' and diagnostic['path'] == str(shell)
    assert published.storage.path.exists() and _snapshot(data) == before


@pytest.mark.parametrize("missing", ["database", "state-directory"])
def test_default_discovery_diagnoses_a_missing_published_database_without_repair(tmp_path, monkeypatch, capsys, missing):
    data = tmp_path / 'data'
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    root = ManagedWorkspace.for_repository('example/project').state_root
    published = _published(root, 'published')
    if missing == 'database':
        published.storage.path.unlink()
    else:
        shutil.rmtree(root)
    before = _snapshot(data)
    assert main(['status', '--repo', 'example/project', '--parent', '1', '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    diagnostic = output['diagnostics'][0]
    if missing == 'database':
        assert diagnostic['code'] == 'unsupported_state'
        assert diagnostic['path'] == str(published.storage.path)
        assert diagnostic['reason'] == 'unreadable_state' and diagnostic['recovery_hint']
        assert output['selection'] == {'repo': 'example/project', 'parent': 1}
    else:
        # Removing the entire sole authority erases its identity. No retired JSON
        # is decoded into a fictitious current Run, and no replacement is created.
        assert diagnostic['code'] == 'run_locator_missing'
    assert 'candidates' not in output
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
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if fault == "malformed":
        pointer.write_bytes(b"{")
    else:
        pointer.unlink(missing_ok=True)
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
    pointer = task_path(TaskKey(ManagedWorkspace.for_repository("example/project").repository_root, "example/project", 1))
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if fault == "malformed":
        pointer.write_bytes(b"{")
    else:
        pointer.unlink(missing_ok=True)
        replacement = tmp_path / "invalid-pointer.json"
        replacement.write_bytes(b"{}")
        pointer.symlink_to(replacement)
    before = _snapshot(data)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "published" and output["parent"]["number"] == 1
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


def test_default_parent_selects_the_exact_published_run_and_retains_terminal_history(tmp_path, monkeypatch, capsys):
    from agent_run.model import DeliveryKind
    scenario = Scenario(tmp_path / 'history', DeliveryKind.PARENT_ONLY)
    assert scenario.run(approve=True).kind == 'complete'
    scenario.kernel.confirm_executor_exit(scenario.execution)
    original = scenario.kernel
    successor_spec = replace(scenario.spec, run_id='published')
    successor = create_kernel(original.storage.path, successor_spec, requirements=scenario.requirements)
    assert original.storage.path == successor.storage.path and successor.storage.run_id == 'published'
    before = _snapshot(tmp_path)
    args = ['--repo', scenario.spec.repository, '--state-dir', str(original.storage.path), '--json']
    assert main(['status', '--parent', str(scenario.spec.parent_issue), *args]) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == 'published'
    assert main(['runs', *args]) == 0
    runs = json.loads(capsys.readouterr().out)['runs']
    assert {item['run_id']: item['is_terminal'] for item in runs} == {'offline-run': True, 'published': False}
    assert main(['history', 'offline-run', *args]) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == 'offline-run'
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("fault", ["damaged", "missing"])
@pytest.mark.parametrize("selector", ["parent", "exact"])
def test_exact_default_query_isolates_another_parents_bound_database_failure(tmp_path, monkeypatch, capsys, fault, selector):
    data = tmp_path / 'data'
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    root = ManagedWorkspace.for_repository('example/project').state_root
    selected = _published(root, 'published')
    other = _published(root, 'other', parent=9)
    assert selected.storage.path == other.storage.path
    if fault == 'damaged':
        other.storage.path.write_bytes(b'not a SQLite database')
    else:
        other.storage.path.unlink()
    before = _snapshot(data)
    arguments = ['--parent', '1'] if selector == 'parent' else ['published']
    # Physical loss of the sole repository authority affects both Tasks. A
    # reader must refuse, never synthesize facts from the retired JSON pointer.
    assert main(['status', *arguments, '--repo', 'example/project', '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    diagnostic = output['diagnostics'][0]
    assert diagnostic['code'] == 'unsupported_state'
    assert diagnostic['path'] == str(other.storage.path)
    assert diagnostic['reason'] == ('uncertain_state' if fault == 'damaged' else 'unreadable_state')
    assert diagnostic['recovery_hint']
    assert 'candidates' not in output
    assert main(['status', '--parent', '9', '--repo', 'example/project', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['path'] == str(other.storage.path)
    assert main(['runs', '--repo', 'example/project', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['path'] == str(other.storage.path)
    assert _snapshot(data) == before
