"""Default public run preserves the original unresolved v6 Task owner."""
import hashlib
import json
from pathlib import Path

from agent_run.managed_workspace import ManagedWorkspace, read_git_identity
from agent_run.task_identity import TaskKey
from agent_run_kernel import host_adapter, lifecycle_cli
from agent_run_kernel.query_cli import main
from agent_run_kernel.task_location import read_task, task_path
from tests.test_kernel_host_adapter import controlled_manager
from tests.test_kernel_legacy_admission import old_owner, unchanged_v6_source


def test_public_run_refuses_the_original_unknown_v6_task_before_creation_or_launch(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys, unchanged_v6_source: Path,
) -> None:
    workspace = ManagedWorkspace.for_repository("example/project")
    workspace.ensure_path(str(git_repo), identity=read_git_identity(git_repo))
    manager, _request, _response, _calls = controlled_manager(tmp_path)
    monkeypatch.setattr(lifecycle_cli, "_active_runner", lambda: True)
    monkeypatch.setattr(host_adapter, "SystemdHost", lambda **_kwargs: manager)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Default run crossed the original v6 Task admission fence")

    monkeypatch.setattr(lifecycle_cli, "prepare_creation", forbidden)
    monkeypatch.setattr(lifecycle_cli, "attach", forbidden)
    monkeypatch.setattr(lifecycle_cli, "submit", forbidden)
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    with old_owner(unchanged_v6_source, workspace.repository_root, workspace.state_root, "unknown"):
        before = hashlib.sha256((workspace.state_root / "state.sqlite3").read_bytes()).hexdigest()
        assert main(["run", "1", "--repo", "example/project", "--json"]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["result"] == "error"
        assert result["diagnostics"][0]["code"] == "legacy_requires_offline_migration"
        assert hashlib.sha256((workspace.state_root / "state.sqlite3").read_bytes()).hexdigest() == before
        assert read_task(task_path(task)) is None
        assert list(workspace.state_root.glob("*/kernel.sqlite3")) == []
