from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitRepository


def test_fetch_retry_rechecks_ownership(git_repo: Path, monkeypatch) -> None:
    git = GitRepository(git_repo)
    owned = True
    attempts = 0

    def guard() -> None:
        if not owned:
            raise RuntimeError("ownership revoked")

    def attempt(arguments, **kwargs):
        nonlocal owned, attempts
        attempts += 1
        owned = False
        return subprocess.CompletedProcess(arguments, 1, "", "transient failure")

    git._set_write_guard(guard)
    monkeypatch.setattr("agent_run.github_retry._run_bounded_command", attempt)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="ownership revoked"):
        git._fetch_default_branch("main")
    assert attempts == 1


@pytest.mark.parametrize("boundary", ["apply", "index"])
@pytest.mark.parametrize("revoke", [False, True])
def test_integration_replay_stdin_rechecks_ownership(
    git_repo: Path, monkeypatch, boundary: str, revoke: bool
) -> None:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=git_repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    tracked = git_repo / "replay.txt"
    tracked.write_text("base\n")
    run("add", ".")
    run("commit", "-m", "replay base")
    base = run("rev-parse", "HEAD")
    tracked.write_text("finding\n")
    run("commit", "-am", "finding snapshot")
    snapshot = run("rev-parse", "HEAD")
    run("checkout", "--detach", base)
    tracked.write_text("default\n")
    run("commit", "-am", "default update")
    default = run("rev-parse", "HEAD")
    git = GitRepository(git_repo)
    owned = True
    dispatched: list[tuple[str, ...]] = []
    from agent_run import git as git_module
    real_run = git_module.run_git

    def guard() -> None:
        if not owned:
            raise RuntimeError("ownership revoked")

    def dispatch(arguments, **kwargs):
        nonlocal owned
        dispatched.append(tuple(arguments[1:]))
        result = real_run(arguments, **kwargs)
        # These are the last read / preceding mutation before each stdin write.
        at_boundary = (
            boundary == "apply" and arguments[1] == "hash-object"
        ) or (
            boundary == "index" and arguments[1:3] == ["update-index", "--force-remove"]
        )
        if at_boundary and revoke:
            owned = False
        return result

    git._set_write_guard(guard)
    monkeypatch.setattr(git_module, "run_git", dispatch)
    if revoke:
        with pytest.raises(RuntimeError, match="ownership revoked"):
            git._replay_finding_delta(
                git_repo, run_head_sha=base, default_head_sha=default,
                candidate_sha=base, snapshot_sha=snapshot,
            )
    else:
        git._replay_finding_delta(
            git_repo, run_head_sha=base, default_head_sha=default,
            candidate_sha=base, snapshot_sha=snapshot,
        )
    target = ("apply", "--whitespace=nowarn") if boundary == "apply" else ("update-index", "-z")
    assert sum(args[:2] == target for args in dispatched) == (0 if revoke else 1)
