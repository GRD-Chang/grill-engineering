from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.github import GhGitHubReader
from agent_run.github_retry import MAX_COMMAND_OUTPUT_BYTES, run_read_command
from agent_run.models import Repository


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

    monkeypatch.setattr("agent_run.github_retry._run_bounded_command", fake_run)
    monkeypatch.setattr("agent_run.github_retry.time.sleep", lambda _seconds: None)

    reader = GhGitHubReader("example/project")
    assert reader.repository().name_with_owner == "example/project"
    assert attempts == 3


def test_github_command_capture_has_a_direct_byte_bound() -> None:
    result = run_read_command(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.stdout.write('x' * ({MAX_COMMAND_OUTPUT_BYTES} + 4096) + 'out-tail'); "
                f"sys.stderr.write('y' * ({MAX_COMMAND_OUTPUT_BYTES} + 4096) + 'err-tail')"
            ),
        ]
    )

    assert result.returncode == 0
    assert result.stdout == "x" * (MAX_COMMAND_OUTPUT_BYTES - 8) + "out-tail"
    assert result.stderr == "y" * (MAX_COMMAND_OUTPUT_BYTES - 8) + "err-tail"


@pytest.mark.parametrize("stderr", ["permission denied", "unexpected response"])
def test_github_read_retries_do_not_classify_stderr(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(arguments, 1, "", stderr)

    monkeypatch.setattr("agent_run.github_retry._run_bounded_command", fake_run)
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
        if arguments[:1] == ("api",):
            sha = arguments[1].rsplit("/", 1)[-1]
            return {
                "tree": {"sha": f"{sha}-tree"},
                "parents": [{"sha": "parent-a"}, {"sha": "parent-b"}],
            }
        return {
            "state": "MERGED",
            "headRefName": "agent-run-repair/run-1",
            "headRefOid": "head-sha",
            "headRepository": {"nameWithOwner": "example/project"},
            "baseRefName": "agent-run/run-1",
            "baseRefOid": "integrated-sha",
            "mergeCommit": {"oid": "integrated-sha"},
        }

    monkeypatch.setattr(reader, "_gh_json", fake_json)

    live = reader.live_pull_request(12)

    assert live["integrated_sha"] == "integrated-sha"
    assert live["head_branch"] == "agent-run-repair/run-1"
    assert live["head_repository"] == live["base_repository"] == "example/project"
    assert live["head_tree"] == "head-sha-tree"
    assert live["integrated_tree"] == "integrated-sha-tree"
    assert live["integrated_parents"] == ["parent-a", "parent-b"]
    assert "mergeCommit" in calls[0][-1]


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

    monkeypatch.setattr("agent_run.github_retry._run_bounded_command", fake_run)
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
