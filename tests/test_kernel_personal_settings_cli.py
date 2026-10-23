"""Real public defaults operations, compared with the unchanged 02 CLI.

All paths are synthetic XDG directories. These tests exercise the actual
UserDefaultsStore and its filesystem transaction; no alternate config writer.
"""
from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel.query_cli import main


def old_main(arguments: list[str]) -> int:
    """Execute the unchanged oracle in another process, never native dead code."""
    configured = os.environ.get("AGENT_RUN_SETTINGS_ORACLE_SRC")
    if not configured:
        pytest.fail("Set AGENT_RUN_SETTINGS_ORACLE_SRC to the immutable unchanged-02 CLI source; no oracle is skipped")
    source = Path(configured)
    expected = {
        "cli.py": "bb705bec1cb40b890876f5ac4c7c2389af6f9419405e9e7ad2c7b5be24efda7c",
        "agent_profiles.py": "e4c2adb94ae5c348a6f1c54012f0d712831fd4d0bf410590cfe48eccffd486be",
        "delivery_policy.py": "f5da6897e2095066214105f1dc219d3eae2c8ff1540c4d3904f06768fc0a1027",
        "user_defaults.py": "069233d34f2cbc1fe30d87144a4eb7caa85987f0c3831c08a3af2b5241cf72c6",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((source / "agent_run" / name).read_bytes()).hexdigest() == digest
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    result = subprocess.run([sys.executable, "-m", "agent_run.cli", *arguments],
        env=environment, text=True, capture_output=True, timeout=20, check=False)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


@pytest.fixture
def personal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("DELIVERY_TEST_PAT", raising=False)
    return config / "agent-run" / "user-defaults.json"


def _call(entry: Callable, arguments: list[str], capsys) -> tuple[int, dict]:
    try:
        code = entry(arguments)
    except SystemExit as error:
        code = error.code
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_builtin_personal_show_matches_real_old_cli_without_creating_files(personal: Path, capsys) -> None:
    old = _call(old_main, ["settings", "show", "--json"], capsys)
    new = _call(main, ["settings", "show", "--json"], capsys)
    # The retired legacy-file display field is no longer part of settings.
    assert new == (old[0], {key: value for key, value in old[1].items() if key != "legacy_ignored"})
    assert "legacy_ignored" not in new[1]
    assert new[0] == 0 and new[1]["source"] == "builtin"
    assert new[1]["scope"] == "future-runs" and new[1]["language"] == "zh"
    assert not personal.parent.exists()


@pytest.mark.parametrize("options", [
    ["--ticket-review-round", "4", "--parent-only-paired-round", "5", "--run-repair-round", "2",
     "--ticket-development-workers", "2", "--ticket-integration-development-limit", "7",
     "--integration-resync-limit", "0", "--ticket-ci-development-limit", "6", "--ticket-ci-review",
     "--parent-ci-development-limit", "8", "--development-thread-policy", "new-per-attempt",
     "--development-timeout", "1.5h", "--review-duration", "90m", "--publication-deadline", "600"],
    ["--profile-preset", "premium", "--dev-model", "  original-model  ", "--dev-effort", "high",
     "--review-model", "original-review", "--review-reasoning-effort", "ultra",
     "--publication-reference", "development", "--language", "en"],
    ["--worker-github-source", "pat", "--runner-github-source", "pat", "--github-pat-env", "DELIVERY_TEST_PAT",
     "--notification-mode", "detailed", "--notification-open-id", "ou_EXPLICIT",
     "--notification-profile", "explicit-profile", "--notification-app-id", "cli_EXPLICIT"],
    ["--worker-github-source", "app", "--runner-github-source", "app", "--github-app-id", "123",
     "--github-installation-id", "456", "--github-private-key-path", "/synthetic/unread-key",
     "--github-app-read-only", "--github-app-current-repository-only"],
])
def test_public_settings_options_preserve_old_receipt_and_persisted_values(
    personal: Path, capsys, options: list[str],
) -> None:
    arguments = ["settings", "configure", *options, "--json"]
    old = _call(old_main, arguments, capsys)
    assert old[0] == 0
    saved = personal.read_bytes()
    personal.unlink()
    new = _call(main, arguments, capsys)
    assert new == (old[0], {key: value for key, value in old[1].items() if key != "legacy_ignored"})
    assert "legacy_ignored" not in new[1]
    assert personal.read_bytes() == saved
    assert personal.stat().st_mode & 0o777 == 0o600
    assert "DELIVERY_TEST_PAT" not in os.environ
    assert _call(main, ["settings", "show", "--json"], capsys)[1]["defaults"] == json.loads(saved)


def test_sparse_profile_reference_and_disabled_flags_use_one_authority(personal: Path, capsys) -> None:
    sequences = [
        ["--preset", "premium", "--publication-model", "independent", "--notifications", "--ticket-ci-review"],
        ["--publication-use-development", "--no-notifications", "--no-ticket-ci-review", "--review-deadline", "3h"],
        ["--publication-reasoning-effort", "max", "--github-pat-file", "/synthetic/unread-pat"],
    ]
    expected = []
    for options in sequences:
        result = _call(old_main, ["settings", "configure", *options, "--json"], capsys)
        assert result[0] == 0
        expected.append((result, personal.read_bytes()))
    personal.unlink()
    for options, (receipt, raw) in zip(sequences, expected, strict=True):
        assert _call(main, ["settings", "configure", *options, "--json"], capsys) == receipt
        assert personal.read_bytes() == raw
    assert UserDefaultsStore().load()["profile"]["publication_effort"] == "max"


@pytest.mark.parametrize("raw", ['{broken', '{"language":"ja"}', '{"policy":{"ticket_review_rounds":null}}'])
def test_invalid_personal_settings_fail_without_repair_but_help_remains_available(
    personal: Path, capsys, raw: str,
) -> None:
    personal.parent.mkdir(parents=True)
    personal.write_text(raw)
    original = personal.read_bytes()
    for arguments in (["settings", "show", "--json"], ["settings", "configure", "--language", "en", "--json"]):
        code, output = _call(main, list(arguments), capsys)
        assert code == 2 and output["result"] == "error"
        assert output["diagnostics"][0]["code"] == "command_failed"
        assert str(personal) in output["diagnostics"][0]["message"]
        assert personal.read_bytes() == original
    assert main(["settings", "--help"]) == 0
    assert "show" in capsys.readouterr().out
    assert personal.read_bytes() == original


@pytest.mark.parametrize("failure", ["replace", "directory-sync"])
def test_public_configure_preserves_original_on_atomic_storage_failure(
    personal: Path, capsys, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    UserDefaultsStore().configure(policy={"ticket_review_rounds": 5})
    original = personal.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("simulated " + failure + " failure")

    if failure == "replace":
        monkeypatch.setattr("agent_run.user_defaults.os.replace", fail)
    else:
        monkeypatch.setattr(UserDefaultsStore, "_sync_directory", fail)
    code, output = _call(main, ["settings", "configure", "--preset", "premium", "--json"], capsys)
    assert code == 2 and output["diagnostics"][0]["code"] == "command_failed"
    assert failure in output["diagnostics"][0]["message"]
    assert personal.read_bytes() == original
    assert not list(personal.parent.glob("*.tmp"))


def test_personal_settings_preserve_old_policy_materials_without_import_or_old_routes(personal: Path, capsys) -> None:
    personal.parent.mkdir(parents=True)
    legacy = personal.with_name("delivery-policy.json")
    legacy.write_text(json.dumps({"ticket_review_rounds": 7, "review_deadline": "3h"}))
    original = legacy.read_bytes()
    code, result = _call(main, ["settings", "show", "--json"], capsys)
    assert code == 0 and result["source"] == "builtin"
    assert result["defaults"] == {} and "legacy_ignored" not in result
    assert not personal.exists() and legacy.read_bytes() == original
    assert _call(main, ["settings", "configure", "--language", "en", "--json"], capsys)[0] == 0
    assert UserDefaultsStore().load() == {"language": "en", "policy": {}, "profile": {}}
    assert legacy.read_bytes() == original
    before = personal.read_bytes()
    for arguments in (["policy", "show"], ["policy", "configure", "--ticket-review-rounds", "8"]):
        assert main(arguments) == 2
        rejected = capsys.readouterr()
        assert "policy" in rejected.err and rejected.out == ""
        assert personal.read_bytes() == before and legacy.read_bytes() == original
    assert _call(main, ["settings", "configure", "--ticket-review-rounds", "8", "--json"], capsys)[0] == 0
    assert UserDefaultsStore().load()["policy"]["ticket_review_rounds"] == 8
    assert legacy.read_bytes() == original


def test_explicit_app_migration_keeps_nonsecret_identity_and_private_material(personal: Path, capsys) -> None:
    personal.parent.mkdir(parents=True, mode=0o700)
    key = personal.parent / "key.pem"
    key.write_text("private material must never appear in the public receipt")
    key.chmod(0o600)
    legacy = personal.with_name("github-app.json")
    legacy.write_text(json.dumps({"app_id": "123", "installation_id": "456", "private_key_path": str(key)}))
    legacy.chmod(0o600)
    original = legacy.read_bytes()
    code, result = _call(main, ["settings", "configure", "--migrate-github-app", "--json"], capsys)
    assert code == 0 and result["github"]["worker"]["source"] == "app"
    assert key.read_text() not in json.dumps(result)
    assert legacy.read_bytes() == original and key.exists()
    saved = personal.read_bytes()
    assert _call(main, ["settings", "show", "--json"], capsys)[1]["github"]["worker"] == result["github"]["worker"]
    assert personal.read_bytes() == saved


def test_personal_plain_output_uses_real_language_and_does_not_send_or_open_runs(
    personal: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    UserDefaultsStore().configure(language="en", profile={"development_model": "User text / 用户原文"})
    before = personal.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Personal settings attempted Run/online work")

    monkeypatch.setattr("agent_run_kernel.query_cli._candidates", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    assert main(["settings", "show"]) == 0
    output = capsys.readouterr().out
    assert "User text / 用户原文" in output
    assert "Legacy configuration ignored" not in output
    assert "User text / 用户原文" in old
    assert personal.read_bytes() == before


def test_run_settings_are_not_answered_with_current_personal_defaults(personal: Path, capsys) -> None:
    UserDefaultsStore().configure(language="en", profile={"development_model": "future-only"})
    before = personal.read_bytes()
    code, result = _call(main, ["settings", "show", "--run", "not-imported", "--json"], capsys)
    assert code == 2 and result["result"] == "error"
    assert result["diagnostics"][0]["code"] == "run_locator_missing"
    assert "future-only" not in json.dumps(result)
    assert personal.read_bytes() == before
