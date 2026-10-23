from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.adapters.github_commands import run_write_command
from agent_run.adapters.auth.runner_transport import runner_git_environment


def test_pat_git_rewrites_ssh_and_resets_host_helper(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_PAT", "selected-pat")
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:example/project.git"],
                   cwd=git_repo, check=True)
    identity = {"source": "pat", "pat": {"env": "RUNNER_PAT"}}
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=git_repo,
                            env=runner_git_environment(identity, cwd=git_repo), capture_output=True, text=True, check=True)
    assert remote.stdout.strip() == "https://github.com/example/project.git"
    for scope in ("--local", "--global"):
        subprocess.run(["git", "config", scope, "credential.https://github.com.helper",
                        "!echo password=host-secret"], cwd=git_repo, check=True)
        subprocess.run(["git", "config", scope, "http.https://github.com/example/.extraHeader",
                        "Authorization: host-secret"], cwd=git_repo, check=True)
    environment = runner_git_environment(identity, cwd=git_repo)
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    headers = subprocess.run(["git", "config", "--get-urlmatch", "http.extraHeader",
                              "https://github.com/example/project.git"], cwd=git_repo,
                             env=environment, capture_output=True, text=True, check=True)
    assert not headers.stdout.strip()
    assert environment["GIT_ALLOW_PROTOCOL"] == "https"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert "selected-pat" not in json.dumps({key: value for key, value in environment.items()
                                           if key.startswith("GIT_CONFIG_")})
    subprocess.run(["git", "config", "credential.helper", "!echo password=host-secret"],
                   cwd=git_repo, check=True)
    credential = subprocess.run(["git", "credential", "fill"], cwd=git_repo, env=environment,
                                input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, check=True)
    assert "password=selected-pat" in credential.stdout
    assert "host-secret" not in credential.stdout
    original = subprocess.run(["git", "config", "remote.origin.url"], cwd=git_repo,
                              capture_output=True, text=True, check=True)
    assert original.stdout.strip() == "git@github.com:example/project.git"


def test_transport_redacts_failed_and_timed_out_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_PAT", "test-secret")
    identity = {"source": "pat", "pat": {"env": "RUNNER_PAT"}}
    result = run_write_command(
        [sys.executable, "-c", "import os,sys; print(os.environ['GH_TOKEN']); "
         "print(os.environ['GH_TOKEN'], file=sys.stderr); sys.exit(1)"],
        runner_identity=identity,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == "[REDACTED]\n"
    monkeypatch.setattr('agent_run.adapters.github_commands.READ_TIMEOUT_SECONDS', 0.1)
    result = run_write_command(
        [sys.executable, "-c", "import os,time; print(os.environ['GH_TOKEN'], flush=True); time.sleep(5)"],
        runner_identity=identity,
    )
    assert result.returncode == 1
    assert "test-secret" not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stdout


@pytest.mark.parametrize("source", ["pat", "app"])
def test_selected_initial_clone_uses_https_without_host_login(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    import shutil

    from agent_run.adapters.workspace import ManagedWorkspace

    real_git = shutil.which("git")
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "clone-calls.json"
    tool = tools / "git"
    tool.write_text(f'''#!{sys.executable}
import json, os, pathlib, shutil, subprocess, sys
if sys.argv[1] != "clone": os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
pathlib.Path({str(calls)!r}).write_text(json.dumps({{"args": sys.argv[1:],
    "token": os.environ.get("GH_TOKEN"), "protocol": os.environ.get("GIT_ALLOW_PROTOCOL")}}))
shutil.copytree({str(git_repo)!r}, sys.argv[-1])
subprocess.run([{real_git!r}, "config", "remote.origin.url", sys.argv[-2]], cwd=sys.argv[-1], check=True)
''')
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("RUNNER_PAT", "clone-pat")
    workspace = ManagedWorkspace.for_repository("example/project")
    identity = {"source": "pat", "pat": {"env": "RUNNER_PAT"}}
    if source == "app":
        from agent_run.adapters.auth.identity import resolve_runner_identity
        identity = resolve_runner_identity({"runner_source": "app", "app": {
            "app_id": "123", "installation_id": "456", "private_key_path": str(tmp_path / "key")}})
        monkeypatch.setattr('agent_run.adapters.auth.runner_credentials.runner_app_token', lambda _identity: "clone-pat")
    checkout = workspace.ensure_path(runner_identity=identity)
    assert checkout == workspace.repository_root
    call = json.loads(calls.read_text())
    assert call["args"][-2] == "https://github.com/example/project.git"
    assert call["token"] == "clone-pat"
    assert call["protocol"] == "https"
    assert "clone-pat" not in (workspace.root / ".agent-run-workspace.json").read_text()
    assert not list(workspace.root.parent.glob(".project-*"))
