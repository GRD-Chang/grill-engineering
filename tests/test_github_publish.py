from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitError, GitRepository
from agent_run.github_publish import GhGitHubPublisher


def test_publish_branch_refuses_remote_drift(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{base}:refs/heads/ticket"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    candidate = _rev_parse(git_repo, "HEAD")
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Updater"], cwd=updater, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "ticket"], cwd=updater, check=True, capture_output=True
    )
    (updater / "external.txt").write_text("external\n", encoding="utf-8")
    subprocess.run(["git", "add", "external.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "external drift"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "ticket"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    drift = _rev_parse(updater, "HEAD")

    with pytest.raises(GitError):
        GhGitHubPublisher(
            "example/project", GitRepository(git_repo)
        ).publish_branch(
            "ticket",
            candidate,
            expected_remote_sha=base,
        )

    assert _remote_head(git_repo, "ticket") == drift


def test_publish_branch_accepts_retry_after_successful_push(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{base}:refs/heads/ticket"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    candidate = _rev_parse(git_repo, "HEAD")
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    publisher.publish_branch("ticket", candidate, expected_remote_sha=base)
    publisher.publish_branch("ticket", candidate, expected_remote_sha=base)

    assert _remote_head(git_repo, "ticket") == candidate


def test_sync_run_branch_recovers_remote_integration_locally(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "branch", "run", base], cwd=git_repo, check=True
    )
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat(test): remote integration"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    integrated = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{integrated}:refs/heads/run"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    assert _rev_parse(git_repo, "run") == base

    GhGitHubPublisher(
        "example/project", GitRepository(git_repo)
    ).sync_run_branch(run_branch="run", integrated_sha=integrated)

    assert _rev_parse(git_repo, "run") == integrated


def test_ticket_pr_recovery_only_reuses_open_prs(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher(
        "example/project", GitRepository(git_repo)
    )
    reads: list[tuple[str, ...]] = []
    writes: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        reads.append(arguments)
        return [] if arguments[:2] == ("pr", "list") else {"number": 12}

    def fake_require(*arguments: str) -> None:
        writes.append(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    number = publisher.ensure_ticket_pr(
        branch="ticket-3",
        base_branch="agent-run/run-1",
        title="fix(delivery): handle a later revision",
        body="Primary Ticket: #3",
        primary_ticket=3,
    )

    assert "--state" in reads[0]
    assert reads[0][reads[0].index("--state") + 1] == "open"
    assert writes[0][:2] == ("pr", "create")
    assert number == 12


def test_agent_run_status_comment_is_updated_in_place(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    comments: list[dict[str, object]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:1] != ("api",):
            raise AssertionError(arguments)
        if "--paginate" in arguments:
            return comments
        body = next(argument[5:] for argument in arguments if argument.startswith("body="))
        if "PATCH" in arguments:
            comments[0]["body"] = body
        else:
            comments.append({"id": 9, "body": body})
        return {"id": 9}

    monkeypatch.setattr(publisher, "_json", fake_json)
    pending = {
        "scope": "ticket-3",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "validation_verdict": "pass",
        "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
        "required_checks": "pending",
        "next_action": "wait for Required Checks",
    }
    publisher.record_agent_run_status(12, pending)
    publisher.record_agent_run_status(
        12, {**pending, "required_checks": "pass", "next_action": "merge"}
    )

    assert len(comments) == 1
    body = str(comments[0]["body"])
    assert "<!-- agent-run:agent-run-status -->" in body
    assert "Required Checks: `pass`" in body
    assert "```json" not in body


def test_squash_merge_uses_supported_pr_merge_and_live_result(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher(
        "example/project", GitRepository(git_repo)
    )
    captured: list[str] = []

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        captured.extend(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {
            "state": "MERGED",
            "integrated_sha": "a" * 40,
        },
    )

    integrated = publisher.squash_merge(
        pr_number=11,
        expected_head_sha="b" * 40,
        run_branch="agent-run/run",
        commit_message="feat(test): supported merge",
    )

    assert integrated == "a" * 40
    assert captured == [
        "pr",
        "merge",
        "11",
        "--repo",
        "example/project",
        "--squash",
        "--match-head-commit",
        "b" * 40,
        "--subject",
        "feat(test): supported merge",
    ]


def test_squash_merge_recovers_when_command_response_is_lost(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher(
        "example/project", GitRepository(git_repo)
    )
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *arguments: subprocess.CompletedProcess(
            arguments, 1, "", "connection reset"
        ),
    )
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {
            "state": "MERGED",
            "integrated_sha": "c" * 40,
        },
    )

    integrated = publisher.squash_merge(
        pr_number=11,
        expected_head_sha="b" * 40,
        run_branch="agent-run/run",
        commit_message="feat(test): response recovery",
    )

    assert integrated == "c" * 40


def _rev_parse(repository: Path, reference: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", reference],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _remote_head(repository: Path, branch: str) -> str:
    output = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    return output.split()[0]
