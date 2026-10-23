"""Unknown old locator material cannot silently authorize an unrelated native selection."""
import json
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.paths import app_data_root
from agent_run_kernel.query_cli import main
from test_kernel_query_cli import _snapshot as _bytes
from test_kernel_query_discovery import _published


def test_empty_run_id_cannot_prove_bad_locator_unrelated(tmp_path, capsys):
    _published(
        ManagedWorkspace.for_repository("example/project").state_root,
        run_id="native-exact",
    )
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
    assert _bytes(tmp_path) == before
    assert code == 2, {
        "actual_code": code,
        "returned_run": output.get("run_id"),
    }
    assert output["diagnostics"][0]["code"] == "run_locator_invalid"
