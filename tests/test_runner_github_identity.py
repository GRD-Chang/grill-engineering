"""Runner identity across real public CLI restarts and fixture delivery boundaries.

The lifecycle fixture keeps remote writes local. Transport credential assertions
live in test_runner_transport; this module protects frozen configuration,
startup failure, approval, and recovery from the same external PAT reference.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_run.user_defaults import UserDefaultsStore
from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_supervision import _parent_only_agents
from support.seeding import write_fixture
from support.user_defaults import settings
from support.workspace import managed_state

from support.state import run_refs, run_state_text


@pytest.mark.parametrize("reference", ["env", "file"])
def test_public_runner_pat_reference_failure_and_resume(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str,
) -> None:
    """One full lifecycle is necessary to verify executor restart and approval."""
    monkeypatch.chdir(git_repo)
    tools = tmp_path / "tools"
    tools.mkdir()
    capture = tmp_path / "authentication.jsonl"
    gh = tools / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ["api", "--hostname", "github.com", "user"]
token = os.environ.get("GH_TOKEN")
with Path(os.environ["RUNNER_AUTH_CAPTURE"]).open("a") as stream:
    stream.write(json.dumps({"token": token}) + "\\n")
raise SystemExit(0 if token in ("runner-first", "runner-repaired") else 1)
''')
    gh.chmod(0o700)
    environment = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_AUTH_CAPTURE": str(capture),
        # Explicit Runner PAT must replace both possible ambient token names.
        "GH_TOKEN": "unavailable-host", "GITHUB_TOKEN": "unavailable-host",
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_only_agents(git_repo / "agents.json")
    token_file = tmp_path / "runner-pat.txt"
    token_file.write_text("runner-first\n")
    environment["TASK_RUNNER_PAT"] = "runner-first"
    reference_value = "TASK_RUNNER_PAT" if reference == "env" else str(token_file)
    assert settings("configure", "--runner-github-source", "host")[0] == 0
    defaults = UserDefaultsStore().path.read_bytes()

    def call(*arguments: str):
        result = run_cli(git_repo, fixture, *arguments, extra_env=environment)
        for secret in ("runner-first", "runner-repaired", "unavailable-host"):
            assert secret not in result.stdout + result.stderr
        return result

    started = call("run", "1", "--agent-fixture", str(agents),
                   "--runner-github-source", "pat", f"--github-pat-{reference}", reference_value)
    assert started.returncode == 0, started.stdout + started.stderr
    assert stdout_json(started)["status"] == "parent_approval_pending"
    original = load_only_run_state(git_repo)
    run_id = original["run_id"]
    expected = {"source": "pat", "pat": {reference: reference_value}}
    assert original["runner_github_identity"] == expected
    assert original["github_identity"] == {"source": "host"}
    assert original["creation_configuration"]["runner_github_identity"] == expected
    assert UserDefaultsStore().path.read_bytes() == defaults
    assert settings("show", "--run", run_id)[1]["github"]["runner"] == expected
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "OPEN"

    if reference == "env":
        environment["TASK_RUNNER_PAT"] = ""
    else:
        token_file.unlink()
    failed_result = call("approve", run_id)
    assert failed_result.returncode == 2, failed_result.stdout + failed_result.stderr
    failed = load_only_run_state(git_repo)
    assert failed["status"] == "execution_failed", failed_result.stdout + failed_result.stderr
    assert failed["runner_github_identity"] == expected
    assert failed["parent_job"]["review_budget"] == original["parent_job"]["review_budget"]
    assert failed["parent_job"]["candidate_sha"] == original["parent_job"]["candidate_sha"]
    status = call("status", run_id, "--json")
    assert status.returncode == 0, status.stdout + status.stderr
    assert stdout_json(status)["status"] == "execution_failed"

    if reference == "env":
        environment["TASK_RUNNER_PAT"] = "runner-repaired"
    else:
        token_file.write_text("runner-repaired\n")
    resumed = call("resume", run_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert stdout_json(resumed)["status"] == "parent_approval_pending"
    repaired = load_only_run_state(git_repo)
    assert repaired["run_id"] == run_id
    assert repaired["runner_github_identity"] == expected
    assert repaired["parent_job"]["review_budget"] == original["parent_job"]["review_budget"]
    assert repaired["parent_job"]["candidate_sha"] == original["parent_job"]["candidate_sha"]
    approved = call("approve", run_id)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "MERGED"
    tokens = [json.loads(line)["token"] for line in capture.read_text().splitlines()]
    assert set(tokens) == {"runner-first", "runner-repaired"}
    texts = [path.read_text() for path in managed_state(git_repo).rglob("*.json")]
    texts.extend(run_state_text(reference) for reference in run_refs(managed_state(git_repo)))
    assert all(secret not in text for text in texts
               for secret in ("runner-first", "runner-repaired", "unavailable-host"))


def test_public_runner_missing_pat_has_no_recoverable_run(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    result = run_cli(git_repo, fixture, "run", "1", "--runner-github-source", "pat",
                     "--github-pat-file", str(tmp_path / "missing-pat"))
    assert result.returncode == 2
    assert "PAT" in result.stdout
    assert not run_refs(managed_state(git_repo))


@pytest.mark.parametrize("worker_source", ["host", "pat"])
def test_codex_process_receives_worker_identity_without_runner_reference(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, worker_source: str,
) -> None:
    """Credential plumbing uses the real backend; only tool execution is fake."""
    import subprocess
    from typing import Any

    from agent_run import codex

    monkeypatch.setenv("GH_TOKEN", "worker-host-token")
    monkeypatch.setenv("TASK_RUNNER_PAT", "runner-only-secret")
    monkeypatch.setenv("TASK_WORKER_PAT", "worker-selected-token")
    monkeypatch.setattr(codex, "validate_worker_auth", lambda *args, **kwargs: None)
    received: list[dict[str, str]] = []

    def worker(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        received.append(dict(options["environment"]))
        Path(arguments[arguments.index("--output-last-message") + 1]).write_text("ok")
        return subprocess.CompletedProcess(
            arguments, 0, '{"type":"thread.started","thread_id":"isolated-worker"}', "",
        )

    monkeypatch.setattr(codex, "run_worker_process", worker)
    identity: dict[str, Any] = {"source": worker_source}
    if worker_source == "pat":
        identity["pat"] = {"env": "TASK_WORKER_PAT"}
    backend = codex.CodexCliBackend(
        worker_identity=identity,
        runner_identity={"source": "pat", "pat": {"env": "TASK_RUNNER_PAT"}},
    )
    _, thread_id = backend._invoke(prompt="test", checkout=git_repo, thread_id=None)
    assert thread_id == "isolated-worker"
    assert received[0]["GH_TOKEN"] == (
        "worker-host-token" if worker_source == "host" else "worker-selected-token"
    )
    assert "TASK_RUNNER_PAT" not in received[0]
    assert "runner-only-secret" not in received[0].values()


@pytest.mark.parametrize("status", [403, 404])
def test_public_runner_repo_denial_does_not_create_provisional_run(
    git_repo: Path, tmp_path: Path, status: int,
) -> None:
    """A valid PAT can authenticate /user yet lack access to the repository."""
    import subprocess

    fixture = write_fixture(git_repo / "github.json", issues={})
    tools = tmp_path / "repo-denial-tools"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(f"#!{sys.executable}\n" + f'''import os, sys
assert os.environ.get("GH_TOKEN") == "valid-but-unscoped"
if sys.argv[1:] == ["api", "--hostname", "github.com", "user"]:
    raise SystemExit(0)
print("HTTP {status}: repository access denied", file=sys.stderr)
raise SystemExit(1)
''')
    gh.chmod(0o700)
    driver = tmp_path / "repo-denial-cli.py"
    driver.write_text('''import sys
from agent_run import cli
from agent_run.github import GhGitHubReader
class Reader(GhGitHubReader):
    def __init__(self, path):
        super().__init__("example/project")
cli.FixtureGitHubReader = Reader
raise SystemExit(cli.main(sys.argv[1:]))
''')
    environment = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                       PYTHONPATH=str(Path(__file__).parents[1] / "src"),
                       TASK_RUNNER_PAT="valid-but-unscoped")
    result = subprocess.run([
        sys.executable, str(driver), "run", "1", "--repo", "example/project",
        "--github-fixture", str(fixture), "--runner-github-source", "pat",
        "--github-pat-env", "TASK_RUNNER_PAT", "--json",
    ], cwd=git_repo, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not run_refs(managed_state(git_repo))
    assert f"HTTP {status}" in result.stdout + result.stderr
    assert "valid-but-unscoped" not in result.stdout + result.stderr


def test_runner_initial_fetch_failure_does_not_create_run(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from agent_run.controller import Controller
    from agent_run.git import GitError, GitRepository
    from agent_run.github_fixture import FixtureGitHubReader
    from agent_run.state import StateStore

    fixture = write_fixture(git_repo / "github.json", issues={}, default_head_sha="a" * 40)
    identity = {"source": "pat", "pat": {"env": "TASK_RUNNER_PAT"}}
    received = []

    def denied_fetch(arguments, **options):
        assert arguments[:2] == ["git", "fetch"]
        received.append(options["runner_identity"])
        return subprocess.CompletedProcess(arguments, 1, "", "HTTP 403: access denied")

    monkeypatch.setattr("agent_run.git.run_read_command", denied_fetch)
    states = StateStore(tmp_path / "startup-state")
    controller = Controller(FixtureGitHubReader(fixture),
                            GitRepository(git_repo, runner_identity=identity), states)
    with pytest.raises(GitError, match="HTTP 403"):
        controller.start_or_resume_unfinished(1)
    assert received == [identity]
    assert not run_refs(states.root)


@pytest.mark.parametrize("failure_mode", ["initial", "renewal"])
def test_public_runner_app_failure_repaired_reference_and_resume(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str,
) -> None:
    """Real CLI restarts protect frozen App binding and durable approval recovery."""
    import subprocess

    monkeypatch.chdir(git_repo)
    key = tmp_path / "app-key"
    key.write_text("first")
    capture = tmp_path / "app-calls.jsonl"
    driver = tmp_path / "app-cli.py"
    driver.write_text('''import json, os, sys, time, threading
from pathlib import Path
from types import SimpleNamespace
from agent_run import cli, github_auth, runner_credentials
from agent_run.credential_lifecycle import ReadCredential
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.runner_transport import runner_environment
renewal_failed = threading.Event()
expired = threading.Event()
class Provider:
    def __init__(self, profile, *, read_only=False, repository=None):
        self.profile, self.read_only, self.repository = profile, read_only, repository
    def __call__(self):
        try:
            token = self.profile.private_key_path.read_text().strip()
        except OSError:
            renewal_failed.set()
            raise github_auth.GitHubCredentialError("key unavailable", authorization_failed=True) from None
        if token not in ("first", "repaired"):
            raise github_auth.GitHubCredentialError("invalid key", authorization_failed=True)
        with Path(os.environ["APP_CAPTURE"]).open("a") as stream:
            stream.write(json.dumps({"token": token, "read_only": self.read_only,
                                     "repository": self.repository}) + "\\n")
        return ReadCredential("test-app-" + token, time.time() + 3600)
    def cancel(self):
        expired.set()
github_auth._GitHubAppCredentialProvider = Provider
runner_credentials._GitHubAppCredentialProvider = Provider
original_merge = FixtureGitHubPublisher.normal_merge
def merge(self, **arguments):
    if os.environ.get("APP_EXPIRE_DURING_MERGE"):
        # Exercise the real credential consumer at the remote-write boundary.
        # The existing fixture retains all remote facts and merge behavior.
        assert runner_environment(self.git.runner_identity)["GH_TOKEN"].startswith("test-app-")
        credentials = runner_credentials._scope.get()
        assert credentials and len(credentials) == 1
        credential = next(iter(credentials.values()))
        current = credential.lifecycle._credential
        assert current is not None
        now = [current.expires_at - 200]
        credential.lifecycle._clock = lambda: now[0]
        runner_credentials.time = SimpleNamespace(time=lambda: now[0])
        Path(self.git.runner_identity["app_profile"]["private_key_path"]).unlink()
        expired.clear()
        credential.lifecycle.wake()
        assert renewal_failed.wait(2), "renewal did not attempt the failed reference"
        now[0] = current.expires_at + 1
        credential.lifecycle.wake()
        assert expired.wait(2), "expired credential was not cancelled"
        Path(os.environ["APP_EXPIRY_PROOF"]).write_text("renewal failed and expired")
        runner_environment(self.git.runner_identity)
        raise AssertionError("expired Runner credential unexpectedly remained usable")
    return original_merge(self, **arguments)
FixtureGitHubPublisher.normal_merge = merge
raise SystemExit(cli.main(sys.argv[1:]))
''')
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_only_agents(git_repo / "agents.json")
    assert settings("configure", "--runner-github-source", "app", "--worker-github-source", "app",
                    "--github-app-id", "123", "--github-installation-id", "456",
                    "--github-private-key-path", str(key), "--github-app-read-only",
                    "--github-app-current-repository-only")[0] == 0
    environment = dict(os.environ, APP_CAPTURE=str(capture), GH_TOKEN="host-must-not-be-used",
                       PYTHONPATH=str(Path(__file__).parents[1] / "src"))

    def call(*arguments: str):
        result = subprocess.run([sys.executable, str(driver), *arguments,
                                 "--github-fixture", str(fixture), "--json"], cwd=git_repo,
                                env=environment, capture_output=True, text=True, timeout=30)
        assert "host-must-not-be-used" not in result.stdout + result.stderr
        assert "test-app-" not in result.stdout + result.stderr
        return result

    started = call("run", "1", "--agent-fixture", str(agents))
    assert started.returncode == 0, started.stdout + started.stderr
    original = load_only_run_state(git_repo)
    assert original["status"] == "parent_approval_pending"
    run_id = original["run_id"]
    identity = original["runner_github_identity"]
    assert identity["source"] == "app"
    assert identity["app_read_only"] is False
    assert identity["app_current_repository_only"] is False
    assert original["github_identity"]["app_read_only"] is True
    assert original["github_identity"]["app_current_repository_only"] is True
    assert original["github_identity"]["app_profile"] == identity["app_profile"]
    assert settings("show", "--run", run_id)[1]["github"]["runner"] == identity
    # Changed personal defaults must not switch the active Run to host.
    assert settings("configure", "--runner-github-source", "host")[0] == 0
    if failure_mode == "initial":
        key.unlink()
    else:
        environment["APP_EXPIRE_DURING_MERGE"] = "1"
        environment["APP_EXPIRY_PROOF"] = str(tmp_path / "expired-proof")
    failed = call("approve", run_id)
    environment.pop("APP_EXPIRE_DURING_MERGE", None)
    if failure_mode == "renewal":
        assert (tmp_path / "expired-proof").read_text() == "renewal failed and expired"
    assert failed.returncode == 2, failed.stdout + failed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "execution_failed", failed.stdout + failed.stderr
    failed_grant = state["parent_job"].get("approval_grant")
    if failure_mode == "renewal":
        assert failed_grant and failed_grant["head_sha"] == original["parent_job"]["publication_sha"]
        assert "runner_credential_renewal_failed" in state["diagnostics"][0]["message"]
        assert json.loads(fixture.read_text())["delivery"]["pull_requests"][0]["state"] == "OPEN"
    assert state["runner_github_identity"] == identity
    assert state["parent_job"]["candidate_sha"] == original["parent_job"]["candidate_sha"]
    assert stdout_json(call("status", run_id))["status"] == "execution_failed"
    key.write_text("repaired")
    resumed = call("resume", run_id)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    state = load_only_run_state(git_repo)
    assert state["runner_github_identity"] == identity
    assert state["parent_job"]["review_budget"] == original["parent_job"]["review_budget"]
    assert state["parent_job"]["candidate_sha"] == original["parent_job"]["candidate_sha"]
    if failure_mode == "initial":
        assert state["status"] == "parent_approval_pending"
        approved = call("approve", run_id)
        assert approved.returncode == 0, approved.stdout + approved.stderr
        assert stdout_json(approved)["status"] == "completed"
    else:
        # Approval committed before expiry; resume reuses that exact grant.
        assert state["status"] == "completed", resumed.stdout + resumed.stderr
        assert state["parent_job"]["approval_grant"] == failed_grant
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "MERGED"
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert {entry["token"] for entry in calls} == {"first", "repaired"}
    assert {(entry["read_only"], entry["repository"]) for entry in calls} == {
        (False, None), (True, original["repository"]),
    }
