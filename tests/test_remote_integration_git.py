"""Exact remote snapshots and mechanical integration with a local bare remote."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitError, GitIntegrationResult, GitRepository
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore


def _assert_durable_conflict_evidence(
    result: GitIntegrationResult, checkout: Path, incoming: str, tmp_path: Path,
) -> None:
    evidence = result.failure_evidence
    assert evidence is not None
    assert evidence["operation"] == "integrate_remote_heads"
    assert evidence["checkout"] == str(checkout)
    assert evidence["command"] == ["git", "merge", "--no-ff", "--no-edit", incoming]
    assert evidence["returncode"] == 1
    assert "stdout" not in evidence and "stderr" not in evidence
    assert "CONFLICT" in json.loads(evidence["stdout_json"])
    assert json.loads(evidence["stderr_json"]) == ""
    store = StateStore(tmp_path / "conflict-state")
    state = {"failure": evidence}
    store.save_run("conflict", state)
    assert store.load_run("conflict") == state


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, name: str, contents: str) -> str:
    (root / name).write_text(contents, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def _collaboration(git_repo: Path, tmp_path: Path) -> tuple[Path, GitRepository, GhGitHubPublisher, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "fetch", "origin", "main")
    _git(git_repo, "push", "origin", f"{base}:refs/heads/ticket")
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")
    git = GitRepository(git_repo)
    return collaborator, git, GhGitHubPublisher("example/project", git), base


def test_live_heads_fetch_exact_objects_and_integrate_both_remote_additions(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "human.txt", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "target.txt", "target\n")
    _git(collaborator, "push", "origin", "main")

    assert _git(git_repo, "rev-parse", "refs/remotes/origin/main") == base
    assert publisher.live_branch_heads("ticket", "main") == (source, target)
    assert git.fetch_remote_commit("ticket", source) == source
    assert git.fetch_remote_commit("main", target) == target
    result = git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    )
    assert result.status == "clean"
    assert result.head_sha == _git(git_repo, "rev-parse", "HEAD")
    assert all(git.is_ancestor(sha, result.head_sha) for sha in (candidate, source, target))
    assert (git_repo / "agent.txt").read_text() == "agent\n"
    assert (git_repo / "human.txt").read_text() == "human\n"
    assert (git_repo / "target.txt").read_text() == "target\n"
    assert _git(git_repo, "rev-parse", "refs/remotes/origin/main") == base


def test_real_conflict_keeps_resolution_scene_and_commits_both_parents(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "README.md", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "README.md", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    observed_source, target = publisher.live_branch_heads("ticket", "main")
    assert observed_source == source
    git.fetch_remote_commit("ticket", source)
    result = git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    )
    assert result.status == "conflict"
    assert result.conflict_stage == "source"
    assert result.conflict_paths == ("README.md",)
    assert result.head_sha == candidate
    assert result.merge_head_sha == source
    _assert_durable_conflict_evidence(result, git_repo, source, tmp_path)
    assert _git(git_repo, "rev-parse", "MERGE_HEAD") == source
    assert git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    ) == result
    with pytest.raises(GitError, match="leftover conflict marker"):
        git.commit_integration_resolution(
            git_repo, expected_head_sha=candidate, expected_merge_sha=source,
            message="resolve collaboration",
        )
    (git_repo / "README.md").write_text("agent and human\n", encoding="utf-8")
    resolved = git.commit_integration_resolution(
        git_repo, expected_head_sha=candidate, expected_merge_sha=source,
        message="resolve collaboration",
    )
    assert git.commit_parents(resolved) == [candidate, source]
    assert (git_repo / "README.md").read_text() == "agent and human\n"


def test_target_conflict_retains_clean_source_merge_as_first_parent(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "README.md", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "human.txt", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "README.md", "target\n")
    _git(collaborator, "push", "origin", "main")
    observed_source, observed_target = publisher.live_branch_heads("ticket", "main")
    assert (observed_source, observed_target) == (source, target)
    git.fetch_remote_commit("ticket", source)
    git.fetch_remote_commit("main", target)

    result = git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    )
    assert result.status == "conflict"
    assert result.conflict_stage == "target"
    assert result.conflict_paths == ("README.md",)
    assert result.merge_head_sha == target
    _assert_durable_conflict_evidence(result, git_repo, target, tmp_path)
    assert git.is_ancestor(candidate, result.head_sha)
    assert git.is_ancestor(source, result.head_sha)
    (git_repo / "README.md").write_text("agent and target\n", encoding="utf-8")
    resolved = git.commit_integration_resolution(
        git_repo, expected_head_sha=result.head_sha,
        expected_merge_sha=target, message="resolve target",
    )
    assert git.commit_parents(resolved) == [result.head_sha, target]
    assert all(git.is_ancestor(sha, resolved) for sha in (candidate, source, target))


def test_fetch_keeps_observed_commit_when_source_advances_after_observation(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(collaborator, "checkout", "ticket")
    observed = _commit(collaborator, "first.txt", "first\n")
    _git(collaborator, "push", "origin", "ticket")
    assert publisher.live_branch_heads("ticket", "main")[0] == observed
    later = _commit(collaborator, "later.txt", "later\n")
    _git(collaborator, "push", "origin", "ticket")

    assert git.fetch_remote_commit("ticket", observed) == observed
    assert git.resolve(observed) == observed
    assert publisher.live_branch_heads("ticket", "main")[0] == later


def test_interrupted_clean_merge_reconciles_exact_mechanical_history(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "human.txt", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "target.txt", "target\n")
    _git(collaborator, "push", "origin", "main")
    assert publisher.live_branch_heads("ticket", "main") == (source, target)
    git.fetch_remote_commit("ticket", source)
    git.fetch_remote_commit("main", target)

    _git(git_repo, "merge", "--no-ff", "--no-edit", source)
    source_merge = git.checkout_head(git_repo)
    result = git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    )
    assert result.status == "clean"
    assert git.commit_parents(result.head_sha) == [source_merge, target]
    assert git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target
    ).head_sha == result.head_sha


def test_rewritten_remote_source_is_observed_without_overwriting_local_history(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, publisher, base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "ticket")
    former = _commit(collaborator, "former.txt", "former\n")
    _git(collaborator, "push", "origin", "ticket")
    assert publisher.live_branch_heads("ticket", "main")[0] == former
    git.fetch_remote_commit("ticket", former)
    _git(collaborator, "reset", "--hard", base)
    rewritten = _commit(collaborator, "rewrite.txt", "rewrite\n")
    _git(collaborator, "push", "--force", "origin", "ticket")
    assert publisher.live_branch_heads("ticket", "main")[0] == rewritten
    assert not git.is_ancestor(former, rewritten)
    assert git.checkout_head(git_repo) == candidate


def test_resolution_reports_real_csv_diff_check_and_retries_after_file_repair(
    git_repo: Path, tmp_path: Path,
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "README.md", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "README.md", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    _, target = publisher.live_branch_heads("ticket", "main")
    git.fetch_remote_commit("ticket", source)
    assert git.integrate_remote_heads(
        git_repo, candidate_sha=candidate, source_sha=source, target_sha=target,
    ).status == "conflict"
    (git_repo / "README.md").write_text("agent and human\n")
    csv = git_repo / "fleet.csv"
    csv.write_bytes(b"\xef\xbb\xbfname,value\r\nship,1\r\n")
    with pytest.raises(GitError, match="trailing whitespace") as error:
        git.commit_integration_work(
            git_repo, expected_head_sha=candidate, expected_merge_sha=source,
            message="resolve csv integration",
        )
    evidence = error.value.evidence
    assert evidence["command"] == ["git", "diff", "--cached", "--check"]
    assert evidence["operation"] == "commit_integration_resolution"
    assert evidence["returncode"] != 0
    assert "fleet.csv:1: trailing whitespace" in evidence["stdout"]
    assert evidence["stderr"] == ""
    assert evidence["checkout"] == str(git_repo)
    assert evidence["head_sha"] == candidate
    assert evidence["merge_head_sha"] == source
    assert _git(git_repo, "ls-files", "-u") == ""
    assert csv.read_bytes().endswith(b"\r\n")
    csv.write_bytes(csv.read_bytes().replace(b"\r\n", b"\n"))
    resolved = git.commit_integration_work(
        git_repo, expected_head_sha=candidate, expected_merge_sha=source,
        message="resolve csv integration",
    )
    assert git.commit_parents(resolved) == [candidate, source]
    assert csv.read_bytes() == b"\xef\xbb\xbfname,value\nship,1\n"
    assert git.commit_integration_work(
        git_repo, expected_head_sha=candidate, expected_merge_sha=source,
        message="resolve csv integration",
    ) == resolved


def test_nonmerge_integration_work_checks_and_exact_recovery(git_repo: Path) -> None:
    git = GitRepository(git_repo)
    original = git.checkout_head(git_repo)
    (git_repo / "data.csv").write_bytes(b"item,value \n")
    with pytest.raises(GitError) as error:
        git.commit_integration_work(
            git_repo, expected_head_sha=original, expected_merge_sha=None,
            message="repair integration",
        )
    assert error.value.evidence["command"] == ["git", "diff", "--cached", "--check"]
    assert error.value.evidence["head_sha"] == original
    assert error.value.evidence["merge_head_sha"] is None
    (git_repo / "data.csv").write_text("item,value\n")
    committed = git.commit_integration_work(
        git_repo, expected_head_sha=original, expected_merge_sha=None,
        message="repair integration",
    )
    assert git.commit_parents(committed) == [original]
    assert git.commit_integration_work(
        git_repo, expected_head_sha=original, expected_merge_sha=None,
        message="repair integration",
    ) == committed
    with pytest.raises(GitError, match="HEAD changed"):
        git.commit_integration_work(
            git_repo, expected_head_sha=original, expected_merge_sha=None,
            message="other integration action",
        )
    assert git.commit_integration_work(
        git_repo, expected_head_sha=committed, expected_merge_sha=None,
        message="clean retry",
    ) == committed


def test_non_conflict_merge_failure_reports_original_hook_output(
    git_repo: Path, tmp_path: Path,
) -> None:
    collaborator, git, publisher, _base = _collaboration(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "ticket")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "ticket")
    source = _commit(collaborator, "human.txt", "human\n")
    _git(collaborator, "push", "origin", "ticket")
    _, target = publisher.live_branch_heads("ticket", "main")
    git.fetch_remote_commit("ticket", source)
    hook = git_repo / ".git/hooks/pre-merge-commit"
    hook.write_text("#!/bin/sh\nprintf 'original stdout\n'\nprintf 'hook failed\n' >&2\nexit 1\n")
    hook.chmod(0o700)
    with pytest.raises(GitError) as error:
        git.integrate_remote_heads(
            git_repo, candidate_sha=candidate, source_sha=source, target_sha=target,
        )
    assert error.value.evidence["command"] == ["git", "merge", "--no-ff", "--no-edit", source]
    # Git routes hook stdout to stderr; preserve the actual Git pipe outputs.
    assert error.value.evidence["stdout"] == ""
    assert "original stdout\n" in error.value.evidence["stderr"]
    assert "hook failed\n" in error.value.evidence["stderr"]
    assert error.value.evidence["head_sha"] == candidate
    assert error.value.evidence["merge_head_sha"] is None
    assert _git(git_repo, "status", "--porcelain") == ""
