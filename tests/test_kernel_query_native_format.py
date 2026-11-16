"""Unsupported input remains located and untouched; it is never empty history."""
from pathlib import Path
import json

import pytest

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.queries.read_facts import QueryError
from agent_run.cli.main import main
from agent_run.config.paths import app_data_root


def test_explicit_unsupported_state_keeps_its_location_and_bytes(tmp_path: Path, capsys) -> None:
    state = tmp_path / "unsupported"
    state.mkdir()
    database = state / "state.sqlite3"
    database.write_bytes(b"Not a current native database\n")
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    for command in ("status", "history", "runs"):
        assert main([command, "--repo", "example/project", "--state-dir", str(state), "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["result"] == "error"
        diagnostic = output["diagnostics"][0]
        assert diagnostic["code"] == "unsupported_state"
        assert diagnostic["path"] == str(database.resolve())
        assert diagnostic["protocol"] is None
        assert diagnostic["reason"] and diagnostic["recovery_hint"]
        assert output["selection"]["state_dir"] == str(state)
        assert output["selection"]["repo"] == "example/project"
        assert "history" not in output and "runs" not in output
        assert {path.name: path.read_bytes() for path in state.iterdir()} == before


def test_default_discovery_reports_encountered_unsupported_state(tmp_path: Path, capsys) -> None:
    state = ManagedWorkspace.for_repository("example/project").state_root
    state.mkdir(parents=True)
    database = state / "state.sqlite3"
    database.write_bytes(b"Not a current native database\n")
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    assert list((app_data_root() / "native-task-locations").glob("*.json")) == []
    assert main(["history", "--repo", "example/project", "--parent", "1", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    # Automatic discovery isolates this unbound file; it is not the selected
    # Task. Explicitly selecting the same bytes must still report unsupported.
    assert output["diagnostics"][0]["code"] == "unsupported_state"
    assert "timeline" not in output and "runs" not in output
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before
    assert list((app_data_root() / "native-task-locations").glob("*.json")) == []
    assert main(["history", "--repo", "example/project", "--parent", "1",
                 "--state-dir", str(state), "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "unsupported_state"
    assert output["diagnostics"][0]["path"] == str(database.resolve())
    assert output["selection"] == {"repo": "example/project", "parent": 1, "state_dir": str(state)}
    assert "timeline" not in output and "runs" not in output
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before


def test_unsupported_publication_keeps_only_its_known_identity(tmp_path: Path, capsys) -> None:
    database = tmp_path / "kernel.sqlite3"
    database.write_bytes(b"Not a current native database\n")
    before = database.read_bytes()
    # Known request selectors remain request metadata, never facts recovered
    # from a retired JSON locator when the sole database cannot be interpreted.
    assert main(['status', 'known-run', '--repo', 'example/project', '--state-dir', str(tmp_path), '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['diagnostics'][0]['code'] == 'unsupported_state'
    assert output['diagnostics'][0]['path'] == str(database.resolve())
    assert output['selection'] == {'run_id': 'known-run', 'repo': 'example/project', 'state_dir': str(tmp_path)}
    assert 'candidates' not in output and 'history' not in output
    assert database.read_bytes() == before
