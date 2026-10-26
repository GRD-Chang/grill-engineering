"""Current public defaults operations with independent expected values.

All paths are synthetic XDG directories. These tests exercise the actual
UserDefaultsStore and its filesystem transaction; no alternate config writer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest

from agent_run.config.defaults import UserDefaultsStore
from agent_run.cli.main import main


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


def test_builtin_personal_show_has_current_defaults_without_creating_files(personal: Path, capsys) -> None:
    new = _call(main, ["settings", "show", "--json"], capsys)
    assert new[1]["defaults"] == {}
    assert new[1]["source_path"] is None and new[1]["path"] == str(personal)
    assert new[1]["policy"] == {
        "parent_only_paired_rounds": 10, "run_repair_rounds": 3, "ticket_review_rounds": 3,
        "ticket_development_workers": 3, "ticket_integration_development_limit": 3,
        "ticket_ci_development_limit": 3, "parent_ci_development_limit": 3,
        "integration_resync_limit": 3, "ticket_ci_review": False,
        "required_checks_wait_seconds": 2700,
        "development_thread_policy": "reuse",
        "invocation_deadlines": {"development": 18000, "review": 7200, "publication": 3600},
    }
    assert new[1]["profile"]["preset"] == "economy"
    for role, model, effort, reference in (
        ("development", "gpt-5.6-luna", "xhigh", None),
        ("review", "gpt-6-astra", "low", None),
        ("publication", "gpt-5.6-luna", "xhigh", "development"),
    ):
        assert new[1]["profile"]["profiles"][role] == {
            "model": model, "reasoning_effort": effort, "reference": reference,
            "provenance": {"preset": "economy", "overrides": [], "reference": reference}}
    assert new[1]["notifications"] == {
        "enabled": False, "open_id": None, "profile": None, "app_id": None, "mode": "concise"}
    assert "legacy_ignored" not in new[1]
    assert new[0] == 0 and new[1]["source"] == "builtin"
    assert new[1]["scope"] == "future-runs" and new[1]["language"] == "zh"
    assert not personal.parent.exists()


@pytest.mark.parametrize("options,expected", [
    (["--ticket-review-round", "4", "--parent-only-paired-round", "5", "--run-repair-round", "2",
      "--ticket-development-workers", "2", "--ticket-integration-development-limit", "7",
      "--integration-resync-limit", "0", "--ticket-ci-development-limit", "6", "--ticket-ci-review",
      "--parent-ci-development-limit", "8", "--development-thread-policy", "new-per-attempt",
      "--development-timeout", "1.5h", "--review-duration", "90m", "--publication-deadline", "600"],
     {"policy": {"ticket_review_rounds": 4, "parent_only_paired_rounds": 5, "run_repair_rounds": 2,
                 "ticket_development_workers": 2, "ticket_integration_development_limit": 7,
                 "integration_resync_limit": 0, "ticket_ci_development_limit": 6, "ticket_ci_review": True,
                 "parent_ci_development_limit": 8, "development_thread_policy": "new-per-attempt",
                 "invocation_deadlines": {"development": "1.5h", "review": "90m", "publication": "600"}},
      "profile": {}}),
    (["--profile-preset", "premium", "--dev-model", "  original-model  ", "--dev-effort", "high",
      "--review-model", "original-review", "--review-reasoning-effort", "ultra",
      "--publication-reference", "development", "--language", "en"],
     {"policy": {}, "profile": {"preset": "premium", "development_model": "original-model",
                               "development_effort": "high", "review_model": "original-review",
                               "review_effort": "ultra", "publication_from_development": True}, "language": "en"}),
    (["--worker-github-source", "pat", "--runner-github-source", "pat", "--github-pat-env", "DELIVERY_TEST_PAT",
      "--notification-mode", "detailed", "--notification-open-id", "ou_EXPLICIT",
      "--notification-profile", "explicit-profile", "--notification-app-id", "cli_EXPLICIT"],
     {"policy": {}, "profile": {}, "github": {"worker_source": "pat", "runner_source": "pat",
                                               "pat": {"env": "DELIVERY_TEST_PAT"}},
      "notifications": {"enabled": True, "mode": "detailed", "open_id": "ou_EXPLICIT",
                        "profile": "explicit-profile", "app_id": "cli_EXPLICIT"}}),
    (["--worker-github-source", "app", "--runner-github-source", "app", "--github-app-id", "123",
      "--github-installation-id", "456", "--github-private-key-path", "/synthetic/unread-key",
      "--github-app-read-only", "--github-app-current-repository-only"],
     {"policy": {}, "profile": {}, "github": {"worker_source": "app", "runner_source": "app",
         "app": {"app_id": "123", "installation_id": "456", "private_key_path": "/synthetic/unread-key"},
         "app_read_only": True, "app_current_repository_only": True}}),
], ids=["options0", "options1", "options2", "options3"])

def test_public_settings_options_persist_current_values_and_receipts(
    personal: Path, capsys, options: list[str], expected: dict,
) -> None:
    arguments = ["settings", "configure", *options, "--json"]
    new = _call(main, arguments, capsys)
    saved = personal.read_bytes()
    assert new[0] == 0 and new[1]["source"] == "user-defaults"
    assert new[1]["scope"] == "future-runs" and new[1]["source_path"] == str(personal)
    assert new[1]["defaults"] == expected == json.loads(saved)
    assert "legacy_ignored" not in new[1]
    if options[0] == "--ticket-review-round":
        assert new[1]["policy"]["invocation_deadlines"] == {"development": 5400, "review": 5400, "publication": 600}
    assert personal.stat().st_mode & 0o777 == 0o600
    assert "DELIVERY_TEST_PAT" not in os.environ
    assert _call(main, ["settings", "show", "--json"], capsys)[1]["defaults"] == json.loads(saved)


def test_sparse_profile_reference_and_disabled_flags_use_one_authority(personal: Path, capsys) -> None:
    sequences = [
        ["--preset", "premium", "--publication-model", "independent", "--notifications", "--ticket-ci-review"],
        ["--publication-use-development", "--no-notifications", "--no-ticket-ci-review", "--review-deadline", "3h"],
        ["--publication-reasoning-effort", "max", "--github-pat-file", "/synthetic/unread-pat"],
    ]
    expected_profiles = [
        {"preset": "premium", "publication_model": "independent"},
        {"preset": "premium", "publication_from_development": True},
        {"preset": "premium", "publication_effort": "max"},
    ]
    for index, options in enumerate(sequences):
        code, receipt = _call(main, ["settings", "configure", *options, "--json"], capsys)
        assert code == 0 and receipt["scope"] == "future-runs"
        saved = json.loads(personal.read_bytes())
        assert receipt["defaults"] == saved
        assert saved["profile"] == expected_profiles[index]
        assert saved["notifications"] == {"enabled": index == 0}
        assert saved["policy"]["ticket_ci_review"] is (index == 0)
        if index > 0:
            assert saved["policy"]["invocation_deadlines"] == {"review": "3h"}
            assert receipt["policy"]["invocation_deadlines"]["review"] == 10800
        if index == 1:
            publication = receipt["profile"]["profiles"]["publication"]
            development = receipt["profile"]["profiles"]["development"]
            assert (publication["model"], publication["reasoning_effort"], publication["reference"]) == (
                development["model"], development["reasoning_effort"], "development")
        if index == 2:
            assert saved["github"] == {"pat": {"file": "/synthetic/unread-pat"}}
            assert receipt["profile"]["profiles"]["publication"]["reference"] is None
            assert receipt["profile"]["profiles"]["publication"]["reasoning_effort"] == "max"
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
        monkeypatch.setattr('agent_run.config.defaults.os.replace', fail)
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

    monkeypatch.setattr('agent_run.cli.selection._candidates', forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    assert main(["settings", "show"]) == 0
    output = capsys.readouterr().out
    assert "User text / 用户原文" in output
    assert "Legacy configuration ignored" not in output
    assert personal.read_bytes() == before


def test_run_settings_are_not_answered_with_current_personal_defaults(personal: Path, capsys) -> None:
    UserDefaultsStore().configure(language="en", profile={"development_model": "future-only"})
    before = personal.read_bytes()
    code, result = _call(main, ["settings", "show", "--run", "not-imported", "--json"], capsys)
    assert code == 2 and result["result"] == "error"
    assert result["diagnostics"][0]["code"] == "run_locator_missing"
    assert "future-only" not in json.dumps(result)
    assert personal.read_bytes() == before
