from __future__ import annotations

from agent_run.contracts.write_authority import WriteAuthority

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.adapters.github import GhGitHubReader
from agent_run.adapters.github_commands import MAX_COMMAND_OUTPUT_BYTES, run_read_command


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

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

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

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

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
        'agent_run.adapters.github.subprocess.run',
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, f"{remote}\n", ""
        ),
    )

    assert GhGitHubReader(working_directory=git_repo).repository_hint() == expected


def test_remote_merge_readback_returns_integrated_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from agent_run.adapters.git_objects import LocalGit
    from test_kernel_remote_effects import _fixture, _publish_pr

    port, git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    source_ref = "refs/heads/agent-run-repair/run-1"
    target_ref = "refs/heads/agent-run/run-1"
    git.run("push", "origin", push.base.sha + ":" + source_ref, push.base.sha + ":" + target_ref)
    push = replace(push, source_ref=source_ref, target_ref=target_ref)
    _original, pr = _publish_pr(port, push)
    parents = [push.base.sha, push.after_sha]
    merge = replace(push, effect_id="merge-readback", operation="merge", pr_number=pr.number,
                    payload={"method": "merge", "parents": parents, "commit_message": "Reviewed merge"})
    assert port.execute(merge, WriteAuthority(lambda: lambda: None)).disposition == "target"
    remote = LocalGit(tmp_path / "origin.git")
    integrated = remote.commit(target_ref)
    before = len(calls.read_text().splitlines())

    readback = port.reconcile(merge)
    live = readback.pr

    assert readback.disposition == "target" and readback.commit.sha == integrated.sha
    assert live.number == pr.number and live.state == "MERGED"
    assert live.source_ref.removeprefix("refs/heads/") == "agent-run-repair/run-1"
    assert live.head_repository == live.repository == "example/project"
    assert live.head.tree == push.candidate.tree
    assert live.merged.tree == integrated.tree
    assert readback.raw["parents"] == parents
    assert remote.run("rev-list", "--parents", "-n", "1", integrated.sha).stdout.split()[1:] == parents
    assert live.base.sha == parents[0]
    observed = [json.loads(line) for line in calls.read_text().splitlines()][before:]
    assert any(command[:2] == ["api", f"repos/example/project/git/commits/{integrated.sha}"] for command in observed)
    assert not any("--method" in command or "--input" in command for command in observed)


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

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

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
