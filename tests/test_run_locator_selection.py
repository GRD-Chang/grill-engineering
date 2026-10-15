from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_run.cli import main
from agent_run.run_locator import RunLocatorIndex
from agent_run.run_selection import read_state_directory
from agent_run.state import StateStore
from agent_run.state_errors import IncompatibleRunStateError
from support.seeding import seed_run, write_fixture
from support.cli import run_cli, stdout_json
from support.workspace import managed_repo, managed_state

from support.state import include_in_state_snapshot, read_run, snapshot_run, write_run_fixture


def test_discovery_reports_incompatible_sqlite_protocol_without_writes(tmp_path: Path) -> None:
    states = StateStore(tmp_path)
    states.save_run("run-1", {"version": 1})
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_meta SET value = 'sqlite-run-objects-v1'")
    before = database.read_bytes()
    with pytest.raises(IncompatibleRunStateError, match="protocol"):
        read_state_directory(tmp_path, None)
    assert database.read_bytes() == before


def test_empty_checkout_without_origin_does_not_list_another_repository(
    git_repo: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    empty = tmp_path / "empty-checkout"
    subprocess.run(["git", "init", "-b", "main", str(empty)], check=True, capture_output=True)
    before = _snapshot(managed_state(git_repo), RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(empty)

    assert main(["runs", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_selector_context"
    assert _snapshot(managed_state(git_repo), RunLocatorIndex.default().path.parent) == before


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_readable_legacy_state_with_unknown_parent_requires_disambiguation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], explicit_directory: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    state_dir = managed_state(git_repo)
    target_state = read_run((state_dir, target))
    legacy = {
        "run_id": "legacy-unknown-parent", "repository": "example/project",
        "schema_version": 1, "checkout_identity": target_state["checkout_identity"],
    }
    (state_dir / "runs").mkdir()
    (state_dir / "runs" / "legacy-unknown-parent.json").write_text(json.dumps(legacy))
    with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
        StateStore(state_dir).load_run("legacy-unknown-parent")
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    directory = ["--state-dir", str(state_dir)] if explicit_directory else []

    for command in ("status", "history"):
        assert main([command, "--repo", "example/project", "--parent", "1", *directory, "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["diagnostics"][0]["code"] == "run_locator_stale"
        assert main([command, target, *directory, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == target
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


def test_repository_parent_queries_ignore_a_deleted_unrelated_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_DATA_HOME": str(tmp_path / "locator")}
    target = stdout_json(seed_run(git_repo, fixture, extra_env=locator_env))["run_id"]
    other = tmp_path / "deleted-checkout"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(other)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:other/project.git"],
        cwd=other,
        check=True,
    )
    other_fixture = write_fixture(other / "github.json", issues={}, repository="other/project")
    assert seed_run(other, other_fixture, extra_env=locator_env).returncode == 0
    shutil.rmtree(managed_repo(other, extra_env=locator_env))
    shutil.rmtree(other)
    locator = tmp_path / "locator" / "agent-run" / "run-locator.json"
    before = locator.read_bytes()
    state = (managed_state(git_repo, extra_env=locator_env), target)
    state_before = snapshot_run(state)

    for command, cwd, selector in (
        ("status", git_repo, ["--parent", "1"]),
        ("history", tmp_path, ["--repo", "example/project", "--parent", "1"]),
        ("runs", tmp_path, ["--repo", "example/project"]),
    ):
        result = run_cli(cwd, fixture, command, *selector, "--json", extra_env=locator_env)
        assert result.returncode == 0, result.stdout
        if command != "runs":
            assert stdout_json(result)["run_id"] == target
    assert locator.read_bytes() == before
    assert snapshot_run(state) == state_before


def test_parent_query_excludes_a_known_other_parent_with_missing_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    RunLocatorIndex.default().register(
        run_id="missing-other-parent", repository_root=git_repo,
        state_dir=git_repo / "missing", repository="example/project", parent_number=2,
    )
    monkeypatch.chdir(git_repo)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == target


@pytest.mark.parametrize("malformed_state", [False, True])
def test_selector_rejects_conflicting_locator_and_state_identity(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    malformed_state: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    locator_path = RunLocatorIndex.default().path
    locator = json.loads(locator_path.read_text())
    locator["entries"][0]["parent_number"] = 2
    locator_path.write_text(json.dumps(locator))
    before = locator_path.read_bytes()
    state_ref = (managed_state(git_repo), target)
    if malformed_state:
        state = read_run(state_ref)
        state.pop("timeline")
        write_run_fixture(state_ref, state)
    state_before = snapshot_run(state_ref)
    monkeypatch.chdir(git_repo)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "run_locator_stale"
    assert "不一致" in output["diagnostics"][0]["message"]
    assert locator_path.read_bytes() == before
    assert snapshot_run(state_ref) == state_before


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_other_parent_legacy_json_is_rejected_without_guessing_its_identity(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    explicit_directory: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    state_dir = managed_state(git_repo)
    (state_dir / "runs").mkdir()
    legacy = state_dir / "runs" / "legacy-run.json"
    # Unsupported JSON is never read to infer its Parent identity.
    legacy.write_text(json.dumps({
        "run_id": "legacy-run", "repository": "example/project",
        "parent": {"number": 2}, "schema_version": 1,
    }))
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    directory = ["--state-dir", str(state_dir)] if explicit_directory else []
    with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
        StateStore(state_dir).load_run("legacy-run")
    for command in ("status", "history"):
        assert main([command, "--repo", "example/project", "--parent", "1", *directory, "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_stale"
        assert main([command, target, *directory, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == target
    assert main(["runs", "--repo", "example/project", "--parent", "1", *directory, "--json"]) == 0
    candidates = json.loads(capsys.readouterr().out)["runs"]
    assert any(item["run_id"] == "legacy-run" and "error" in item for item in candidates)
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


@pytest.mark.parametrize("unavailable", ["old-missing", "old-deleted-checkout", "matching-missing", "matching-corrupt"])
def test_unknown_or_potentially_matching_records_fail_closed_but_exact_id_works(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    unavailable: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    locator_path = RunLocatorIndex.default().path
    locator = json.loads(locator_path.read_text())
    entry = dict(locator["entries"][0], run_id="unavailable-run")
    if unavailable.startswith("old-"):
        entry.pop("repository")
        entry.pop("parent_number")
    if unavailable == "old-deleted-checkout":
        entry["repository_root"] = str(git_repo / "deleted-checkout")
    if unavailable == "matching-corrupt":
        corrupt_root = git_repo / "corrupt-state"
        corrupt_root.mkdir()
        (corrupt_root / "state.sqlite3").write_bytes(b"broken SQLite database")
        entry["state_dir"] = str(corrupt_root)
    locator["entries"].append(entry)
    locator_path.write_text(json.dumps(locator))
    snapshot_roots = [managed_state(git_repo), locator_path.parent]
    if unavailable == "matching-corrupt":
        snapshot_roots.append(corrupt_root)
    before = _snapshot(*snapshot_roots)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history"):
        assert main([command, "--repo", "example/project", "--parent", "1", "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["diagnostics"][0]["code"] == "run_locator_stale"
        assert "unavailable-run" in {item["run_id"] for item in output["diagnostics"][0]["candidates"]}
        assert main([command, target, "--state-dir", str(managed_state(git_repo)), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == target
    assert main(["runs", "--repo", "example/project", "--parent", "1", "--json"]) == 0
    candidates = json.loads(capsys.readouterr().out)["runs"]
    assert any(item["run_id"] == "unavailable-run" and "error" in item for item in candidates)
    assert _snapshot(*snapshot_roots) == before


@pytest.mark.parametrize("contradiction", ["checkout-repository", "checkout-identity", "run-id", "invalid-parent", "invalid-repository"])
def test_unregistered_legacy_identity_cannot_hide_conflicts_as_an_unrelated_parent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    contradiction: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:example/project.git"], cwd=git_repo, check=True)
    legacy: dict[str, object] = {
        "run_id": "legacy-run", "repository": "example/project",
        "parent": {"number": 2}, "schema_version": 1,
    }
    if contradiction == "checkout-repository":
        legacy["repository"] = "other/project"
    elif contradiction == "checkout-identity":
        legacy["checkout_identity"] = "another-clone"
    elif contradiction == "run-id":
        legacy["run_id"] = "different-run"
    elif contradiction == "invalid-parent":
        legacy["parent"] = {"number": False}
    else:
        legacy["repository"] = "invalid"
    state_dir = managed_state(git_repo)
    (state_dir / "runs").mkdir()
    (state_dir / "runs" / "legacy-run.json").write_text(json.dumps(legacy))
    with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
        StateStore(state_dir).load_run("legacy-run")
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_stale"
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


def _snapshot(*roots: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for root in roots for path in root.rglob("*") if path.is_file() and include_in_state_snapshot(path)
    }


def test_user_clones_reuse_the_same_unregistered_managed_run(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    clone_fixture = write_fixture(clone / "github.json", issues={})
    other = stdout_json(seed_run(clone, clone_fixture))["run_id"]
    locator_path = RunLocatorIndex.default().path
    locator = json.loads(locator_path.read_text())
    locator["entries"] = [entry for entry in locator["entries"] if entry["run_id"] != target]
    locator_path.write_text(json.dumps(locator))
    assert other == target
    assert managed_repo(git_repo) == managed_repo(clone)
    before = _snapshot(git_repo, clone, locator_path.parent)
    for directory in (git_repo, clone, tmp_path):
        monkeypatch.chdir(directory)
        assert main(["status", "--repo", "example/project", "--parent", "1", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == target
    assert _snapshot(git_repo, clone, locator_path.parent) == before


def test_invalid_locator_query_failures_leave_all_state_unchanged(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    locator_path = RunLocatorIndex.default().path
    locator = json.loads(locator_path.read_text())
    locator["entries"][0].pop("parent_number")
    locator_path.write_text(json.dumps(locator))
    before = _snapshot(managed_state(git_repo), locator_path.parent)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history", "runs"):
        assert main([command, "--repo", "example/project", "--parent", "1", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_invalid"
    assert _snapshot(managed_state(git_repo), locator_path.parent) == before


def test_explicit_state_directory_does_not_disambiguate_multiple_matching_runs(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    first = stdout_json(seed_run(git_repo, fixture))["run_id"]
    second = stdout_json(seed_run(git_repo, fixture, reuse_existing=False))["run_id"]
    state_dir = managed_state(git_repo)
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history"):
        assert main([command, "--repo", "example/project", "--parent", "1", "--state-dir", str(state_dir), "--json"]) == 2
        diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
        assert diagnostic["code"] == "run_selector_ambiguous"
        assert {item["run_id"] for item in diagnostic["candidates"]} == {first, second}
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


def test_exact_mutations_reject_a_state_record_for_another_run_without_writes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    state_dir = managed_state(git_repo)
    # The payload is valid, but its persisted record key selects a different Run.
    alias = "wrong-run"
    write_run_fixture((state_dir, alias), read_run((state_dir, target)))
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)

    for command in ("resume", "stop", "abandon"):
        assert main([
            command, alias, "--github-fixture", str(fixture), "--json",
        ]) == 2
        diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
        assert diagnostic["code"] == "run_selector_not_found"
        assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


def test_snapshot_keeps_durable_sqlite_files_and_ignores_coordination_files(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = root / "state.sqlite3"
    wal = root / "state.sqlite3-wal"
    shm = root / "state.sqlite3-shm"
    other = root / "other.sqlite3-wal"
    other_shm = root / "other.sqlite3-shm"
    directory = root / "nested" / "state.sqlite3-shm"
    directory.mkdir(parents=True)
    assert include_in_state_snapshot(directory)
    other_shm.write_bytes(b"unrelated file")
    database.write_bytes(b"database")
    wal.write_bytes(b"")
    shm.write_bytes(b"coordination index")
    other.write_bytes(b"")

    before = _snapshot(root)
    assert set(before) == {database, other, other_shm}
    assert before[database] == (b"database", database.stat().st_mtime_ns)
    shm.write_bytes(b"updated coordination index")
    assert _snapshot(root) == before

    wal.write_bytes(b"committed data")
    after = _snapshot(root)
    assert set(after) == {database, wal, other, other_shm}
    assert after[wal] == (b"committed data", wal.stat().st_mtime_ns)
    assert after[database] == before[database]
