from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import write_fixture
from agent_run.controller import Controller
from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.github_retry import run_read_command
from agent_run.models import Repository
from agent_run.state import MAX_TIMELINE_EVENTS, StateStore


def test_github_reader_retries_transient_timeout(
    git_repo: Path, monkeypatch
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        if "repo" in arguments:
            attempts += 1
            if attempts < 3:
                return subprocess.CompletedProcess(
                    arguments, 1, "", "dial tcp: i/o timeout"
                )
        return _repository_response()

    monkeypatch.setattr("agent_run.github_retry.subprocess.run", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)

    reader = GhGitHubReader("example/project")
    assert reader.repository().name_with_owner == "example/project"
    assert attempts == 3


@pytest.mark.parametrize("stderr", ["permission denied", "unexpected response"])
def test_github_read_retries_do_not_classify_stderr(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(arguments, 1, "", stderr)

    monkeypatch.setattr("agent_run.github_retry.subprocess.run", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)

    result = run_read_command(["gh", "api", "repos/example/project"])

    assert result.stderr == stderr
    assert attempts == 3


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:example/project.git", "example/project"),
        ("https://github.com/example/project.git", "example/project"),
        ("https://gitlab.com/example/project.git", None),
    ],
)
def test_github_reader_derives_repository_hint_from_origin(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        "agent_run.github.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, f"{remote}\n", ""
        ),
    )

    assert GhGitHubReader(working_directory=git_repo).repository_hint() == expected


def test_github_reader_live_pull_request_returns_integrated_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = GhGitHubReader("example/project")
    monkeypatch.setattr(
        reader,
        "repository",
        lambda: Repository("example/project", "main", "default-sha"),
    )
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> dict[str, object]:
        calls.append(arguments)
        return {
            "state": "MERGED",
            "headRefOid": "head-sha",
            "baseRefName": "agent-run/run-1",
            "baseRefOid": "integrated-sha",
            "mergeCommit": {"oid": "integrated-sha"},
        }

    monkeypatch.setattr(reader, "_gh_json", fake_json)

    live = reader.live_pull_request(12)

    assert live["integrated_sha"] == "integrated-sha"
    assert "mergeCommit" in calls[0][-1]


def test_git_fetch_retries_transient_timeout(
    git_repo: Path, monkeypatch
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return subprocess.CompletedProcess(
                arguments, 1, "", "dial tcp: i/o timeout"
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("agent_run.github_retry.subprocess.run", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)

    GitRepository(git_repo)._fetch_default_branch("main")

    assert attempts == 3


def test_publisher_does_not_blindly_retry_a_write(
    git_repo: Path, monkeypatch
) -> None:
    writes = 0
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher, "_ensure_remote_run_branch", lambda _branch, _sha: None
    )
    monkeypatch.setattr(publisher, "_remote_branch_sha", lambda _branch: None)

    def fail_write(_arguments, **_kwargs):
        nonlocal writes
        writes += 1
        return subprocess.CompletedProcess([], 1, "", "dial tcp: i/o timeout")

    monkeypatch.setattr("agent_run.github_publish.run_write_command", fail_write)

    with pytest.raises(GitError, match="dial tcp: i/o timeout"):
        publisher.ensure_change_branch(
            branch="agent-run/run-1/example",
            base_branch="main",
            expected_base_sha="a" * 40,
            expected_remote_sha="a" * 40,
            recovery_remote_sha="a" * 40,
        )
    assert writes == 1


def test_publisher_does_not_write_after_branch_read_exhaustion(
    git_repo: Path, monkeypatch
) -> None:
    reads = 0
    writes: list[list[str]] = []

    def fake_run(arguments, **_kwargs):
        nonlocal reads
        reads += 1
        return subprocess.CompletedProcess(
            arguments, 1, "", "HTTP 503: upstream unavailable"
        )

    monkeypatch.setattr("agent_run.github_retry.subprocess.run", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher, "_ensure_remote_run_branch", lambda _branch, _sha: None
    )

    with pytest.raises(GitError, match="HTTP 503"):
        publisher.ensure_change_branch(
            branch="agent-run/run-1/example",
            base_branch="main",
            expected_base_sha="a" * 40,
            expected_remote_sha="a" * 40,
            recovery_remote_sha="a" * 40,
        )

    assert reads == 3
    assert writes == []


def test_foreground_run_selection_reuses_one_unfinished_run(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket()})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )

    created, created_resumed = controller.start_or_resume_unfinished(1)
    reused, reused_resumed = controller.start_or_resume_unfinished(1)

    assert not created_resumed
    assert reused_resumed
    assert reused["run_id"] == created["run_id"]
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_foreground_run_reports_all_conflicting_run_ids(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket()})
    states = StateStore(git_repo / ".agent-run")
    for run_id in ("run-first", "run-second"):
        states.save_run(
            run_id,
            {
                "run_id": run_id,
                "repository": "example/project",
                "parent": {"number": 1},
                "status": "active",
                "created_at": run_id,
            },
        )
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )

    with pytest.raises(ValueError, match="run-first, run-second"):
        controller.start_or_resume_unfinished(1)


def test_timeline_stops_appending_at_the_bounded_capacity(git_repo: Path) -> None:
    states = StateStore(git_repo / ".agent-run")
    state: dict[str, object] = {"run_id": "bounded", "status": "starting"}

    for number in range(MAX_TIMELINE_EVENTS + 10):
        state["status"] = f"state-{number}"
        states.save_run("bounded", state)

    stored = states.load_run("bounded")
    assert stored is not None
    assert len(stored["timeline"]) == MAX_TIMELINE_EVENTS
    assert stored["timeline"][-1]["kind"] == "timeline_capacity"
    assert stored["timeline_at_capacity"] is True


def test_github_reader_retries_transient_http_status(
    git_repo: Path, monkeypatch
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        if "repo" in arguments:
            attempts += 1
            if attempts < 3:
                return subprocess.CompletedProcess(
                    arguments, 1, "", "HTTP 503: upstream unavailable"
                )
        return _repository_response()

    monkeypatch.setattr("agent_run.github_retry.subprocess.run", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)

    reader = GhGitHubReader("example/project")
    assert reader.repository().name_with_owner == "example/project"
    assert attempts == 3


def _repository_response() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"],
        0,
        json.dumps(
            {
                "nameWithOwner": "example/project",
                "defaultBranchRef": {"name": "main"},
                "sha": "abc123",
            }
        ),
        "",
    )


def _ticket() -> dict[str, object]:
    return {
        "number": 2,
        "title": "Ticket",
        "body": "Deliver the requested change.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }
