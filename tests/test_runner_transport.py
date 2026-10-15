from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.github_retry import run_read_command, run_write_command
from agent_run.runner_transport import runner_git_environment


@pytest.mark.parametrize("source", ["pat", "app"])
def test_selected_delivery_transports_and_lost_delete_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """Real subprocess boundaries cover API reads/writes and Git cleanup recovery."""
    token_file = tmp_path / "pat"
    token_file.write_text("selected-pat")
    identity = {"source": "pat", "pat": {"file": str(token_file)}}
    if source == "app":
        from agent_run.credential_lifecycle import ReadCredential
        from agent_run.github_identity import resolve_runner_identity
        import time
        class Provider:
            def __init__(self, profile):
                self.profile = profile
            def __call__(self):
                return ReadCredential(token_file.read_text(), time.time() + 3600)
            def cancel(self):
                pass
        monkeypatch.setattr("agent_run.runner_credentials._GitHubAppCredentialProvider", Provider)
        identity = resolve_runner_identity({"runner_source": "app", "app": {
            "app_id": "123", "installation_id": "456", "private_key_path": str(token_file)}})
    log = tmp_path / "calls.jsonl"
    state = tmp_path / "remote-head"
    state.write_text("a" * 40)
    program = f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as stream:
    stream.write(json.dumps({{"tool": pathlib.Path(sys.argv[0]).name, "args": args,
        "token": os.environ.get("GH_TOKEN"), "other": os.environ.get("GITHUB_TOKEN"),
        "protocol": os.environ.get("GIT_ALLOW_PROTOCOL")}}) + "\\n")
state = pathlib.Path({str(state)!r})
if args[:2] == ["repo", "view"]:
    print(json.dumps({{"nameWithOwner": "example/project", "defaultBranchRef": {{"name": "main"}}}}))
elif args[:1] == ["api"]:
    print(json.dumps({{"sha": "a" * 40, "login": "selected-user"}}))
elif args[:1] == ["ls-remote"]:
    if state.exists(): print(state.read_text() + "\\trefs/heads/agent-run/test/run")
elif args[:1] == ["push"]:
    state.unlink()
    print("response lost " + os.environ["GH_TOKEN"], file=sys.stderr)
    sys.exit(1)
elif args[:1] == ["rev-parse"]:
    print("a" * 40)
'''
    for name in ("git", "gh"):
        tool = tmp_path / name
        tool.write_text(program)
        tool.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "other-host-token")
    reader = GhGitHubReader("example/project", runner_identity=identity)
    assert reader.repository().default_head_sha == "a" * 40
    git = GitRepository(tmp_path, runner_identity=identity)
    assert git.resolve("HEAD") == "a" * 40
    # Exercise the same API write dispatch used by Publisher, without retries.
    publisher = GhGitHubPublisher("example/project", git)
    monkeypatch.setattr(GhGitHubReader, "delivery_graph", lambda reader, _number: reader.repository())
    monkeypatch.setattr("agent_run.github_publish.effective_revision_from_graph",
                        lambda repository, _number: repository.default_head_sha)
    assert publisher.current_effective_revision(parent_number=1, ticket_number=2,
                                                expected_revision="old") == "a" * 40
    assert publisher._run("api", "repos/example/project/issues/1", "--method", "PATCH").returncode == 0
    token_file.write_text("replacement-pat")
    run_read_command(["git", "fetch", "origin", "main"], cwd=tmp_path, runner_identity=identity)
    publisher.delete_managed_branch("agent-run/test/run", expected_head_sha="a" * 40)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    network_calls = [row for row in calls if row["args"][0] not in {"rev-parse", "config"}]
    assert {row["token"] for row in network_calls} == {"selected-pat", "replacement-pat"}
    assert all(row["other"] is None for row in network_calls)
    assert all(row["protocol"] == "https" for row in network_calls if row["tool"] == "git")
    assert sum(row["args"][0] == "push" for row in calls) == 1
    assert os.environ["GH_TOKEN"] == "host-token"


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
    monkeypatch.setattr("agent_run.github_retry.READ_TIMEOUT_SECONDS", 0.1)
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

    from agent_run.managed_workspace import ManagedWorkspace

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
        from agent_run.github_identity import resolve_runner_identity
        identity = resolve_runner_identity({"runner_source": "app", "app": {
            "app_id": "123", "installation_id": "456", "private_key_path": str(tmp_path / "key")}})
        monkeypatch.setattr("agent_run.runner_credentials.runner_app_token", lambda _identity: "clone-pat")
    git = workspace.ensure(runner_identity=identity)
    assert git.root == workspace.repository_root
    call = json.loads(calls.read_text())
    assert call["args"][-2] == "https://github.com/example/project.git"
    assert call["token"] == "clone-pat"
    assert call["protocol"] == "https"
    assert "clone-pat" not in (workspace.root / ".agent-run-workspace.json").read_text()
    assert not list(workspace.root.parent.glob(".project-*"))


@pytest.mark.parametrize("setting", [
    "remote.origin.url", "remote.origin.pushurl",
    "url.https://prior-user:prior-token@github.com/.insteadOf",
])
def test_runner_pat_rejects_embedded_remote_credentials(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, setting: str,
) -> None:
    monkeypatch.setenv("RUNNER_PAT", "selected-pat")
    value = ("https://github.com/" if setting.startswith("url.")
             else "https://prior-user:prior-token@github.com/example/project.git")
    subprocess.run(["git", "config", setting, value], cwd=git_repo, check=True)
    repository = GitRepository(git_repo, runner_identity={
        "source": "pat", "pat": {"env": "RUNNER_PAT"},
    })
    # No remote contact: the credential-bearing configuration fails before push.
    with pytest.raises(GitError, match="remote URLs containing credentials") as caught:
        repository._run("push", "origin", "HEAD")
    assert "prior-token" not in str(caught.value)
    assert "selected-pat" not in str(caught.value)
    assert subprocess.run(["git", "config", setting], cwd=git_repo, check=True,
                          capture_output=True, text=True).stdout.strip() == value
