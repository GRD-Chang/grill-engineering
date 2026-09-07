from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.run_locator import RunLocatorError, RunLocatorIndex


def test_locator_reads_mixed_routes_without_migrating_old_entries(tmp_path: Path) -> None:
    locator = RunLocatorIndex(tmp_path / "locator.json")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    locator.register(run_id="old", repository_root=tmp_path, state_dir=state_dir)
    locator.register(
        run_id="new", repository_root=tmp_path, state_dir=state_dir,
        repository="owner/project", parent_number=195,
    )
    before = (locator.path.read_bytes(), locator.path.stat().st_mtime_ns)
    entries = {entry["run_id"]: entry for entry in locator.entries()}
    assert set(entries["old"]) == {"run_id", "repository_root", "state_dir", "updated_at"}
    assert entries["new"]["repository"] == "owner/project"
    assert entries["new"]["parent_number"] == 195
    assert (locator.path.read_bytes(), locator.path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("route", [
    {"repository": "owner/project"}, {"parent_number": 1},
    {"repository": "invalid", "parent_number": 1},
    {"repository": "owner/project", "parent_number": "1"},
    {"repository": "owner/project", "parent_number": True},
    {"repository": "owner/project", "parent_number": 0},
    {"repository": "owner/project", "parent_number": -1},
    {"repository": "owner/project", "parent_number": 1, "status": "active"},
])
def test_locator_rejects_partial_or_malformed_routes_read_only(
    tmp_path: Path, route: dict[str, object],
) -> None:
    path = tmp_path / "locator.json"
    path.write_text(json.dumps({"entries": [{
        "run_id": "old", "repository_root": str(tmp_path),
        "state_dir": str(tmp_path / "state"), "updated_at": "2026-09-08T00:00:00Z",
        **route,
    }]}))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(RunLocatorError) as error:
        RunLocatorIndex(path).entries()
    assert error.value.code == "run_locator_invalid"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_locator_rejects_contradictory_routes_for_the_same_run(tmp_path: Path) -> None:
    path = tmp_path / "locator.json"
    entries = [{
        "run_id": "same-run", "repository_root": str(tmp_path),
        "state_dir": str(tmp_path / "missing"), "updated_at": "2026-09-08T00:00:00Z",
        "repository": "owner/project", "parent_number": parent,
    } for parent in (1, 2)]
    path.write_text(json.dumps({"entries": entries}))
    before = path.read_bytes()
    with pytest.raises(RunLocatorError) as error:
        RunLocatorIndex(path).entries()
    assert error.value.code == "run_locator_conflict"
    assert path.read_bytes() == before
