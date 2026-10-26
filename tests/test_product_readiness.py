"""Preparation inspects selected identities without consuming their secrets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.cli.main import main
from support.readiness import fake_readiness_tools as _tools


def test_local_readiness_does_not_authenticate_or_read_pat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # An absent file is a configuration problem, never permission to use host gh.
    assert main(["settings", "configure", "--runner-github-source", "pat",
                 "--worker-github-source", "pat", "--github-pat-file", str(tmp_path / "missing"),
                 "--json"]) == 0
    capsys.readouterr()
    calls = _tools(tmp_path, monkeypatch)
    assert main(["doctor", "--local", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verification"]["scope"] == "local"
    assert report["verification"]["model_call"] is False
    assert report["identities"]["runner"]["source"] == "pat"
    assert report["identities"]["runner"]["status"] == "unavailable"
    assert report["verification"]["repository_permissions"] == "not_verified"
    assert "gh auth" not in calls.read_text()
    assert "gh api --hostname" not in calls.read_text()
    assert "openssl dgst" not in calls.read_text()


@pytest.mark.parametrize("mode", ["--local", "--online", None])
def test_codex_login_is_only_checked_outside_local_mode(tmp_path, monkeypatch, capsys, mode) -> None:
    calls = _tools(tmp_path, monkeypatch)
    arguments = ["doctor", "--json"] + ([mode] if mode else [])
    assert main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    commands = calls.read_text().splitlines()
    assert "codex --version" in commands
    assert "codex exec --help" in commands
    assert "codex exec resume --help" in commands
    assert ("codex login status" in commands) is (mode != "--local")
    if mode == "--local":
        assert report["checks"]["codex"]["authentication"] == "not_checked"
        assert report["checks"]["codex"]["logged_in"] is None
    else:
        assert report["checks"]["codex"]["logged_in"] is True


def test_non_host_configuration_is_not_rejected_by_host_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SYNTHETIC_READINESS_PAT", "do-not-print-this")
    assert main(["settings", "configure", "--runner-github-source", "pat",
                 "--worker-github-source", "pat", "--github-pat-env", "SYNTHETIC_READINESS_PAT",
                 "--json"]) == 0
    capsys.readouterr()
    calls = _tools(tmp_path, monkeypatch)
    assert main(["doctor", "--local", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checks"]["github"]["status"] == "ok"
    assert report["identities"]["runner"]["status"] == "ok"
    assert report["identities"]["worker"]["status"] == "ok"
    assert report["identities"]["runner"]["authentication"] == "not_verified"
    assert "do-not-print-this" not in output
    assert "gh auth" not in calls.read_text()


def test_foreground_is_not_advertised_when_host_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _tools(tmp_path, monkeypatch)
    from agent_run.runtime.foreground_host import ForegroundHost
    def unavailable(self):raise RuntimeError('Controlled unavailable namespace observation')
    monkeypatch.setattr(ForegroundHost, 'preflight', unavailable)
    assert main(["doctor", "--local", "--mode", "foreground", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["execution_readiness"]["status"] == "unavailable"
    assert report["execution_readiness"]["mode"] == "foreground"
    assert report["modes"]["foreground"]["status"] == "unavailable"
    assert report["modes"]["foreground"]["business_execution"] == "not_verified"


@pytest.mark.parametrize("version,supported", [
    ((3, 51, 3), True), ((3, 50, 7), True), ((3, 44, 6), True),
    ((3, 51, 2), False), ((3, 50, 6), False), ((3, 45, 0), False),
])
def test_readiness_uses_the_storage_sqlite_gate(
    tmp_path, monkeypatch, capsys, version, supported,
) -> None:
    import sqlite3

    _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(sqlite3, "sqlite_version", ".".join(map(str, version)))
    assert main(["doctor", "--local", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["sqlite"]["status"] == ("ok" if supported else "unsupported")
    if not supported:
        assert report["installation_readiness"]["status"] == "issues"
        assert report["execution_readiness"]["status"] == "unavailable"


def test_local_app_only_checks_key_metadata_and_preserves_user_files(tmp_path, monkeypatch, capsys) -> None:
    key = tmp_path / "private.pem"
    key.write_text("invalid key contents must not be read or signed")
    key.chmod(0o600)
    assert main(["settings", "configure", "--runner-github-source", "app", "--worker-github-source", "app",
                 "--github-app-id", "123", "--github-installation-id", "456",
                 "--github-private-key-path", str(key), "--json"]) == 0
    capsys.readouterr()
    calls = _tools(tmp_path, monkeypatch)
    before = key.read_bytes()
    assert main(["doctor", "--local", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert all(check["status"] == "ok" for check in report["identities"].values())
    assert "invalid key contents" not in output
    assert "openssl dgst" not in calls.read_text()
    assert key.read_bytes() == before


def test_skills_are_reported_without_initializing_or_overwriting(tmp_path, monkeypatch, capsys) -> None:
    _tools(tmp_path, monkeypatch)
    root = Path.home() / ".agents" / "skills"
    personal = Path.home() / ".config" / "agent-run" / "prompts" / "zh" / "development.md"
    personal.parent.mkdir(parents=True)
    personal.write_text("my custom method")
    for name in ("implement", "code-review"):
        skill = root / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("my custom skill")
    before = {str(p): p.read_bytes() for p in Path.home().rglob("*") if p.is_file()}
    assert main(["doctor", "--local", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["skills"]["status"] == "ok"
    assert report["skills"]["dependencies"] == "not_verified"
    assert {str(p): p.read_bytes() for p in Path.home().rglob("*") if p.is_file()} == before


def test_explicit_online_uses_selected_native_identity_and_only_verifies_reads(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SYNTHETIC_ONLINE_PAT", "synthetic-online-secret")
    assert main(["settings", "configure", "--runner-github-source", "pat", "--worker-github-source", "pat",
                 "--github-pat-env", "SYNTHETIC_ONLINE_PAT", "--json"]) == 0
    capsys.readouterr()
    calls = _tools(tmp_path, monkeypatch)
    gh = tmp_path / "tools" / "gh"
    gh.write_text("#!/bin/sh\n" + f"echo gh \"$@\" >> '{calls}'\n" + """
case "$1:$2" in
    --version:*) echo 'gh version 2.65.0';;
    api:--help) echo '--paginate --slurp --method --header';;
    api:--hostname) printf 'HTTP/2 200\\r\\n\\r\\n';;
    repo:view) echo '{"nameWithOwner":"example/project","defaultBranchRef":{"name":"main"}}';;
    api:repos/example/project/git/ref/heads/main) echo '{"ref":"refs/heads/main","object":{"type":"commit","sha":"0123456789012345678901234567890123456789"}}';;
    *) exit 1;;
esac
""")
    assert main(["doctor", "--online", "--repo", "example/project", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert all(value["authentication"] == "verified" for value in report["identities"].values())
    assert report["verification"]["repository_read"] == {"status": "verified", "repository": "example/project"}
    assert report["verification"]["repository_permissions"] == "not_verified"
    assert report["verification"]["worker_environment"] == "not_verified"
    assert report["verification"]["model_call"] is False
    assert "synthetic-online-secret" not in output
    commands = calls.read_text()
    assert "gh auth" not in commands
    assert commands.count("gh api --hostname github.com user --include --silent") == 2
    assert "--method POST" not in commands and "git push" not in commands


def test_doctor_run_selection_keeps_frozen_identity_and_state(git_repo, tmp_path, monkeypatch, capsys) -> None:
    from support.cli_fixtures import _published_profile_task

    kernel = _published_profile_task(git_repo)
    assert main(["settings", "configure", "--runner-github-source", "pat",
                 "--github-pat-env", "UNSET_NEW_DEFAULT", "--json"]) == 0
    capsys.readouterr()
    before = kernel.storage.path.read_bytes()
    _tools(tmp_path, monkeypatch)
    assert main(["doctor", "--local", "--run", "profile-public", "--repo", "example/project", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["configuration"]["scope"] == "run"
    assert report["configuration"]["run_id"] == "profile-public"
    assert report["identities"]["runner"]["source"] == "host"
    assert report["identities"]["worker"]["source"] == "host"
    assert kernel.storage.path.read_bytes() == before


def test_incompatible_gh_never_starts_explicit_authentication(tmp_path, monkeypatch, capsys) -> None:
    calls = _tools(tmp_path, monkeypatch)
    gh = tmp_path / "tools" / "gh"
    gh.write_text(f"#!/bin/sh\necho gh \"$@\" >> '{calls}'\necho '--paginate --method --header'\n")
    assert main(["doctor", "--online", "--repo", "example/project", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["github"]["status"] == "unsupported"
    assert report["execution_readiness"]["status"] == "unavailable"
    assert all(value["authentication"] == "not_verified" for value in report["identities"].values())
    assert "--hostname" not in calls.read_text()
