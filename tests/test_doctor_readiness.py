from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.runner_installer import _runtime_identity
from test_run_lifecycle import _file_snapshot, _isolated_environment


@pytest.mark.parametrize(
    ("installed", "codex_available", "systemd_available"),
    [(True, False, True), (True, True, False), (False, True, True), (True, True, True)],
)
def test_doctor_cli_separates_installation_and_execution_readiness(
    tmp_path: Path,
    installed: bool,
    codex_available: bool,
    systemd_available: bool,
) -> None:
    environment = _isolated_environment(tmp_path / "environment")
    home = Path(environment["HOME"])
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    environment["PATH"] = str(user_bin)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in (
        "git", "openssl", "bwrap", "gh", "systemd-run", "systemctl", "rm", "codex",
    ):
        if name == "codex" and not codex_available:
            continue
        executable = user_bin / name
        code = 1 if name == "systemctl" and not systemd_available else 0
        output = ""
        if name == "git":
            output = "git version 2.40.0"
        elif name == "gh":
            output = "--paginate --slurp --method --header"
        elif name == "codex":
            output = "--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color"
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {code}\n")
        executable.chmod(0o700)
    if installed:
        root = Path(environment["XDG_DATA_HOME"]) / "agent-run"
        snapshot = root / "snapshots" / "fixture"
        package = snapshot / "lib" / "python3" / "site-packages" / "agent_run"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (snapshot / "manifest.json").write_text(json.dumps({
            "content_identity": _runtime_identity(package),
        }))
        entry = snapshot / "bin" / "agent-run"
        entry.parent.mkdir()
        entry.write_text(
            f"#!{sys.executable}\n"
            "from agent_run.cli import main\nraise SystemExit(main())\n"
        )
        entry.chmod(0o700)
        generation = root / "generations" / "fixture"
        generation.mkdir(parents=True)
        (generation / "current").symlink_to(snapshot)
        (root / "active").symlink_to(generation)
        (user_bin / "agent-run").symlink_to(
            root / "active" / "current" / "bin" / "agent-run"
        )
        command = [str(user_bin / "agent-run")]
    else:
        command = [sys.executable, "-m", "agent_run"]

    before = _file_snapshot(tmp_path)
    result = subprocess.run(
        [*command, "doctor", "--json"], env=environment, cwd=tmp_path,
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["installation_readiness"]["status"] == (
        "ready" if installed else "issues"
    )
    assert "codex" not in report["installation_readiness"]["checks"]
    assert report["execution_readiness"]["status"] == (
        "ok" if codex_available and systemd_available else "unavailable"
    )
    assert "codex" in report["execution_readiness"]["checks"]
    assert report["status"] == (
        "ready" if installed and codex_available and systemd_available else "issues"
    )
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("dependency", "script", "status"),
    [
        ("git", "printf 'git version 2.39.5\\n'", "unsupported"),
        ("git", "printf 'not a version\\n'", "unsupported"),
        ("codex", "exit 0", "unsupported"),
        ("gh", "echo '--paginate --method --header'", "unsupported"),
        ("gh", "[ \"$1\" = auth ] && exit 1\necho '--paginate --slurp --method --header'", "not_logged_in"),
        ("codex", "[ \"$1\" = login ] && exit 1\nprintf '%s\\n' '--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color'", "not_logged_in"),
        ("bwrap", "[ \"$1\" = --version ] && exit 0\nexit 1", "unavailable"),
    ],
)
def test_doctor_rejects_version_only_or_incompatible_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], dependency: str, script: str, status: str,
) -> None:
    from agent_run.cli import main

    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / dependency
    executable.write_text("#!/bin/sh\n" + script + "\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    key = {"bwrap": "bubblewrap", "gh": "github"}.get(dependency, dependency)
    assert report["checks"][key]["status"] == status
    assert report["execution_readiness"]["status"] == "unavailable"
