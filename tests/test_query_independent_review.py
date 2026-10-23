"""Retired locator material cannot override an exact native Task publication."""
import json
from pathlib import Path
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey
from agent_run_kernel.query import task_binding
from agent_run_kernel.query_cli import main
from test_kernel_query_cli import _snapshot as _bytes
from test_kernel_query_discovery import _published


def test_retired_empty_run_locator_cannot_override_exact_native_task_binding(tmp_path, capsys):
    root = ManagedWorkspace.for_repository("example/project").state_root
    selected = _published(root, run_id="native-exact")
    other = _published(root, run_id="other-parent", parent=9)
    locator = app_data_root() / "run-locator.json"
    locator.write_text(json.dumps({"entries": [{
        "run_id": "",
        "repository_root": str(tmp_path / "old"),
        "state_dir": "invalid-relative-path",
        "updated_at": "2026-10-22T00:00:00Z",
        "repository": "example/project",
        "parent_number": 1,
    }]}))
    before = _bytes(tmp_path)
    code = main([
        "status", "native-exact", "--repo", "example/project", "--json",
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0, {
        "actual_code": code,
        "returned_run": output.get("run_id"),
    }
    assert output["run_id"] == "native-exact" and output["parent"]["number"] == 1
    assert output["repository"] == "example/project"
    assert task_binding(TaskKey(Path(output["repository_root"]), "example/project", 1)) == (
        "native-exact", selected.storage.path)
    assert main(["status", "--parent", "9", "--repo", "example/project", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "other-parent" and output["parent"]["number"] == 9
    assert task_binding(TaskKey(Path(output["repository_root"]), "example/project", 9)) == (
        "other-parent", other.storage.path)
    assert main(["status", "not-published", "--repo", "example/project", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_missing"
    assert _bytes(tmp_path) == before
