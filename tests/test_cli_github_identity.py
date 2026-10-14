"""Native identity delivery across public CLI restarts with external-tool doubles."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.user_defaults import UserDefaultsStore
from support.cli import load_only_run_state
from support.seeding import write_fixture
from support.user_defaults import settings
from support.workspace import managed_state

from support.state import only_run_ref, read_run, run_refs, run_state_text, snapshot_run, write_run_fixture


@pytest.mark.parametrize("reference", ["env", "file", "app"])
def test_public_native_identity_failure_and_same_reference_resume(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str,
) -> None:
    """Real CLI/fork/restart/state; gh and Codex alone are external doubles."""
    monkeypatch.chdir(git_repo)
    tools = tmp_path / "tools"
    tools.mkdir()
    capture = tmp_path / "received.jsonl"
    gh = tools / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
value = os.environ.get("GH_TOKEN")
if value is None:
    config = Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
    value = json.loads(config.read_text())["github.com"]["oauth_token"]
if sys.argv[1:] == ["api", "--hostname", "github.com", "user"]:
    raise SystemExit(0 if value in ("pat-first", "pat-repaired") else 1)
assert sys.argv[1:] == ["api", "--method", "POST", "repos/other/project/dispatches"]
with Path(os.environ["IDENTITY_CAPTURE"]).open("a") as out:
    out.write(json.dumps({"token":value,"args":sys.argv[1:]}) + "\\n")
''')
    gh.chmod(0o700)
    driver = tmp_path / "candidate-cli.py"
    driver.write_text('''import json, subprocess, sys
from pathlib import Path
from agent_run import cli, codex, github_auth
from agent_run.credential_lifecycle import ReadCredential
import time

class ControlledAppProvider:
    def __init__(self, profile, *, read_only=False, repository=None):
        self.profile = profile
        assert read_only is True
        assert repository is None
    def __call__(self):
        try:
            token = self.profile.private_key_path.read_text().strip()
        except OSError:
            raise github_auth.GitHubCredentialError("key unavailable") from None
        if token not in ("pat-first", "pat-repaired"):
            raise github_auth.GitHubCredentialError("key invalid")
        return ReadCredential(token, time.time() + 3600)
    def cancel(self):
        pass

github_auth._GitHubAppCredentialProvider = ControlledAppProvider
codex._GitHubAppCredentialProvider = ControlledAppProvider

def worker(arguments, **options):
    assert not any("gh-adapter" in item for item in arguments)
    subprocess.run(["gh", "api", "--method", "POST", "repos/other/project/dispatches"],
                   env=options["environment"], check=True)
    Path(arguments[arguments.index("--output-last-message") + 1]).write_text(json.dumps({
        "result_kind":"human_blocker", "summary":None,
        "human_blockers":["测试边界：需要输入"]
    }))
    return subprocess.CompletedProcess(arguments, 0,
        '{"type":"thread.started","thread_id":"identity-thread"}\\n', "")
codex.run_worker_process = worker
raise SystemExit(cli.main(sys.argv[1:]))
''')
    fixture = write_fixture(git_repo / "github.json", issues={})
    token_file = tmp_path / "pat.txt"
    token_file.write_text("pat-first\n")
    environment = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                       PYTHONPATH=str(Path(__file__).parents[1] / "src"),
                       IDENTITY_CAPTURE=str(capture), TASK_WORKER_PAT="pat-first",
                       GH_TOKEN="independent-runner-secret")
    ref_args = ["--github-pat-env", "TASK_WORKER_PAT"] if reference == "env" else ["--github-pat-file", str(token_file)]
    if reference == "app":
        ref_args = ["--github-app-id", "123", "--github-installation-id", "456",
                    "--github-private-key-path", str(token_file), "--github-app-read-only"]
    assert settings("configure", "--worker-github-source", "host")[0] == 0
    defaults = UserDefaultsStore().path.read_bytes()

    def call(*arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([sys.executable, str(driver), *arguments,
                                 "--github-fixture", str(fixture), "--json"],
                                cwd=git_repo, env=environment, capture_output=True,
                                text=True, timeout=30)
        assert result.returncode == (0 if arguments[0] == "status" else 2), result.stdout + result.stderr
        assert "independent-runner-secret" not in result.stdout + result.stderr
        assert "pat-first" not in result.stdout + result.stderr
        assert "pat-repaired" not in result.stdout + result.stderr
        return result

    if reference == "app":
        token_file.unlink()
        failed_start = call("run", "1", "--worker-github-source", "app", *ref_args)
        assert not run_refs(managed_state(git_repo))
        assert "app" in failed_start.stdout
        token_file.write_text("pat-first\n")
    started = call("run", "1", "--worker-github-source", "app" if reference == "app" else "pat", *ref_args)
    assert run_refs(managed_state(git_repo)), started.stdout + started.stderr
    first = load_only_run_state(git_repo)
    assert first["status"] == "ready_for_human"
    expected = {"source": "pat", "pat": {reference: "TASK_WORKER_PAT" if reference == "env" else str(token_file)}}
    if reference == "app":
        expected = {"source": "app", "app_profile": {"app_id": "123", "installation_id": "456",
                    "private_key_path": str(token_file)}, "app_read_only": True,
                    "app_current_repository_only": False}
    assert first["github_identity"] == expected
    assert first["creation_configuration"]["github_identity"] == expected
    assert UserDefaultsStore().path.read_bytes() == defaults
    assert settings("show", "--run", first["run_id"])[1]["github"]["worker"] == expected
    assert json.loads(capture.read_text().splitlines()[0])["token"] == "pat-first"
    # Change the default and invalidate the selected reference. No host fallback.
    assert settings("configure", "--worker-github-source", "host")[0] == 0
    if reference == "env":
        environment["TASK_WORKER_PAT"] = "invalid-pat"
    else:
        token_file.unlink()
    call("resume", first["run_id"], "--message", "继续当前任务")
    failed = load_only_run_state(git_repo)
    assert failed["status"] == "execution_failed", failed
    assert failed["github_identity"] == expected
    assert len(capture.read_text().splitlines()) == 1
    assert failed["parent_job"]["review_budget"] == first["parent_job"]["review_budget"]
    status = call("status", first["run_id"])
    assert "execution_failed" in status.stdout
    if reference == "env":
        environment["TASK_WORKER_PAT"] = "pat-repaired"
    else:
        token_file.write_text("pat-repaired\n")
    call("resume", first["run_id"])
    repaired = load_only_run_state(git_repo)
    assert repaired["run_id"] == first["run_id"]
    assert repaired["status"] == "ready_for_human"
    assert repaired["github_identity"] == expected
    assert repaired["parent_job"]["development_thread_id"] == "identity-thread"
    assert repaired["parent_job"]["review_budget"] == first["parent_job"]["review_budget"]
    assert [json.loads(line)["token"] for line in capture.read_text().splitlines()] == ["pat-first", "pat-repaired"]
    texts = [path.read_text() for path in managed_state(git_repo).rglob("*.json")]
    texts.extend(run_state_text(reference) for reference in run_refs(managed_state(git_repo)))
    assert all(secret not in text for text in texts
               for secret in ("pat-first", "pat-repaired", "independent-runner-secret"))
    if reference == "app":
        state_ref = only_run_ref(managed_state(git_repo))
        compatible_state = read_run(state_ref)
        control_root = managed_state(git_repo) / "task-control"
        for missing in ("app_read_only", "app_current_repository_only", "github_identity"):
            legacy_state = deepcopy(compatible_state)
            if missing == "github_identity":
                legacy_state.pop(missing)
            else:
                legacy_state["github_identity"].pop(missing)
            write_run_fixture(state_ref, legacy_state)
            before = snapshot_run(state_ref)
            controls_before = {path.name: path.read_bytes() for path in control_root.glob("*.json")}
            capture_before, fixture_before = capture.read_bytes(), fixture.read_bytes()
            incompatible = call("resume", first["run_id"], "--message", "旧授权不能猜测")
            assert json.loads(incompatible.stdout)["diagnostics"][0]["code"] == "incompatible_run_state"
            assert snapshot_run(state_ref) == before
            assert {path.name: path.read_bytes() for path in control_root.glob("*.json")} == controls_before
            assert capture.read_bytes() == capture_before
            assert fixture.read_bytes() == fixture_before


def test_new_run_rejects_missing_pat_before_creating_state(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.cli import run_cli

    monkeypatch.chdir(git_repo)
    fixture = write_fixture(git_repo / "github.json", issues={})
    result = run_cli(git_repo, fixture, "run", "1", "--worker-github-source", "pat",
                     "--github-pat-file", str(tmp_path / "missing"))
    assert result.returncode == 2
    assert not run_refs(managed_state(git_repo))
    assert "PAT" in result.stdout
