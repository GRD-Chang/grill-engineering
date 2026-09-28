"""Native identity delivery across public CLI restarts with external-tool doubles."""
from __future__ import annotations

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


@pytest.mark.parametrize("reference", ["env", "file"])
def test_public_native_pat_failure_and_same_reference_resume(
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
from agent_run import cli, codex

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

    started = call("run", "1", "--worker-github-source", "pat", *ref_args)
    assert list((managed_state(git_repo) / "runs").glob("*.json")), started.stdout + started.stderr
    first = load_only_run_state(git_repo)
    assert first["status"] == "ready_for_human"
    expected = {"source": "pat", "pat": {reference: "TASK_WORKER_PAT" if reference == "env" else str(token_file)}}
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
    for path in managed_state(git_repo).rglob("*.json"):
        assert all(secret not in path.read_text() for secret in ("pat-first", "pat-repaired", "independent-runner-secret"))


def test_new_run_rejects_missing_pat_before_creating_state(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.cli import run_cli

    monkeypatch.chdir(git_repo)
    fixture = write_fixture(git_repo / "github.json", issues={})
    result = run_cli(git_repo, fixture, "run", "1", "--worker-github-source", "pat",
                     "--github-pat-file", str(tmp_path / "missing"))
    assert result.returncode == 2
    assert not list((managed_state(git_repo) / "runs").glob("*.json"))
    assert "PAT" in result.stdout
