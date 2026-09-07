from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_run.cli import main
from agent_run.run_locator import RunLocatorIndex
from conftest import seed_run, write_fixture
from test_cli import run_cli, stdout_json


def test_repository_parent_queries_ignore_a_deleted_unrelated_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator")}
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
    shutil.rmtree(other)
    locator = tmp_path / "locator" / "agent-run" / "run-locator.json"
    before = locator.read_bytes()
    state = git_repo / ".agent-run" / "runs" / f"{target}.json"
    state_before = state.read_bytes()

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
    assert state.read_bytes() == state_before


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
    assert main(["status", "--parent", "1", "--json"]) == 0
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
    state_path = git_repo / ".agent-run" / "runs" / f"{target}.json"
    if malformed_state:
        state = json.loads(state_path.read_text())
        state.pop("timeline")
        state_path.write_text(json.dumps(state))
    state_before = state_path.read_bytes()
    monkeypatch.chdir(git_repo)
    assert main(["status", "--parent", "1", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "run_locator_stale"
    assert "不一致" in output["diagnostics"][0]["message"]
    assert locator_path.read_bytes() == before
    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_other_parent_legacy_state_in_the_same_directory_does_not_block_queries(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    explicit_directory: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    target = stdout_json(seed_run(git_repo, fixture))["run_id"]
    state_dir = git_repo / ".agent-run"
    legacy = state_dir / "runs" / "legacy-run.json"
    # Historical identity is sufficient for exclusion; it is not a current
    # lifecycle payload and has no checkout identity or registration to repair.
    legacy.write_text(json.dumps({
        "run_id": "legacy-run", "repository": "example/project",
        "parent": {"number": 2}, "schema_version": 1,
    }))
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    directory = ["--state-dir", str(state_dir)] if explicit_directory else []
    for command in ("status", "history", "runs"):
        assert main([command, "--parent", "1", *directory, "--json"]) == 0
        output = json.loads(capsys.readouterr().out)
        if command == "runs":
            assert [candidate["run_id"] for candidate in output["runs"]] == [target]
        else:
            assert output["run_id"] == target
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
        (git_repo / ".agent-run" / "runs" / "unavailable-run.json").write_text("{")
    locator["entries"].append(entry)
    locator_path.write_text(json.dumps(locator))
    before = _snapshot(git_repo / ".agent-run", locator_path.parent)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history"):
        assert main([command, "--repo", "example/project", "--parent", "1", "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["diagnostics"][0]["code"] == "run_locator_stale"
        assert "unavailable-run" in {item["run_id"] for item in output["diagnostics"][0]["candidates"]}
        assert main([command, target, "--state-dir", str(git_repo / ".agent-run"), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == target
    assert main(["runs", "--parent", "1", "--json"]) == 0
    candidates = json.loads(capsys.readouterr().out)["runs"]
    assert any(item["run_id"] == "unavailable-run" and "error" in item for item in candidates)
    assert _snapshot(git_repo / ".agent-run", locator_path.parent) == before


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
    state_dir = git_repo / ".agent-run"
    (state_dir / "runs" / "legacy-run.json").write_text(json.dumps(legacy))
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    assert main(["status", "--parent", "1", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_stale"
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before


def _snapshot(*roots: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for root in roots for path in root.rglob("*") if path.is_file()
    }


def test_unregistered_local_run_without_origin_still_detects_another_clone(
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
    before = _snapshot(git_repo / ".agent-run", clone / ".agent-run", locator_path.parent)
    monkeypatch.chdir(git_repo)
    assert main(["status", "--parent", "1", "--json"]) == 2
    diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_ambiguous"
    assert {candidate["run_id"] for candidate in diagnostic["candidates"]} == {target, other}
    assert _snapshot(git_repo / ".agent-run", clone / ".agent-run", locator_path.parent) == before


def test_invalid_locator_query_failures_leave_all_state_unchanged(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    locator_path = RunLocatorIndex.default().path
    locator = json.loads(locator_path.read_text())
    locator["entries"][0].pop("parent_number")
    locator_path.write_text(json.dumps(locator))
    before = _snapshot(git_repo / ".agent-run", locator_path.parent)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history", "runs"):
        assert main([command, "--parent", "1", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_invalid"
    assert _snapshot(git_repo / ".agent-run", locator_path.parent) == before


def test_explicit_state_directory_does_not_disambiguate_multiple_matching_runs(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    first = stdout_json(seed_run(git_repo, fixture))["run_id"]
    second = stdout_json(seed_run(git_repo, fixture, reuse_existing=False))["run_id"]
    state_dir = git_repo / ".agent-run"
    before = _snapshot(state_dir, RunLocatorIndex.default().path.parent)
    monkeypatch.chdir(git_repo)
    for command in ("status", "history"):
        assert main([command, "--parent", "1", "--state-dir", str(state_dir), "--json"]) == 2
        diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
        assert diagnostic["code"] == "run_selector_ambiguous"
        assert {item["run_id"] for item in diagnostic["candidates"]} == {first, second}
    assert _snapshot(state_dir, RunLocatorIndex.default().path.parent) == before
