"""A later publication keeps the source history used by the existing PR."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_run import github_publish
from agent_run.branch_publication import RemoteBranchChanged
from agent_run.git import GitError, GitRepository
from agent_run.github_publish import GhGitHubPublisher


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _candidate(root: Path, name: str, contents: str) -> str:
    (root / name).write_text(contents, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def test_ticket_ref_recovery_accepts_remote_source_and_target_appends(
    git_repo: Path, tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    source_branch = "agent-run/test/ticket"
    target_branch = "agent-run/test/run"
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    authority = {
        "ticket_number": 1,
        "branch": source_branch,
        "base_branch": target_branch,
        "expected_base_sha": base,
        "expected_remote_sha": base,
        "recovery_remote_sha": base,
    }
    publisher.ensure_ticket_branch(**authority)
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")
    _git(collaborator, "checkout", "--detach", base)
    source = _candidate(collaborator, "source.txt", "source contribution\n")
    _git(collaborator, "push", "origin", f"{source}:refs/heads/{source_branch}")
    _git(collaborator, "reset", "--hard", base)
    target = _candidate(collaborator, "target.txt", "target contribution\n")
    _git(collaborator, "push", "origin", f"{target}:refs/heads/{target_branch}")

    publisher.ensure_ticket_branch(**authority)

    assert publisher.live_branch_heads(source_branch, target_branch) == (source, target)


def test_later_publication_is_fast_forward_and_failed_rewrite_preserves_remote(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "push", "origin", f"{base}:refs/heads/ticket")
    git = GitRepository(git_repo)
    publisher = GhGitHubPublisher("example/project", git)

    first_candidate = _candidate(git_repo, "first.txt", "first\n")
    first = git.create_publication_commit(
        git_repo, candidate_sha=first_candidate, base_sha=base, message="first"
    )
    assert publisher.publish_branch(
        "ticket", first, expected_remote_sha=base
    ).disposition == "published"

    second_candidate = _candidate(git_repo, "second.txt", "second\n")
    second = git.create_publication_commit(
        git_repo, candidate_sha=second_candidate, base_sha=base,
        published_sha=first, message="second",
    )
    assert git.commit_parents(second) == [first]
    result = publisher.publish_branch("ticket", second, expected_remote_sha=first)
    assert result.observation.observed_sha == first
    assert result.disposition == "published"
    assert publisher.publish_branch(
        "ticket", second, expected_remote_sha=first
    ).disposition == "already_published"

    _git(git_repo, "reset", "--hard", base)
    rewritten = _candidate(git_repo, "rewrite.txt", "rewrite\n")
    with pytest.raises(GitError, match="does not descend"):
        publisher.publish_branch("ticket", rewritten, expected_remote_sha=second)
    assert _git(git_repo, "ls-remote", "origin", "refs/heads/ticket").split()[0] == second


def test_unexpected_remote_rewrite_returns_observed_head_without_overwrite(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "push", "origin", f"{base}:refs/heads/ticket")
    git = GitRepository(git_repo)
    publisher = GhGitHubPublisher("example/project", git)
    first_candidate = _candidate(git_repo, "first.txt", "first\n")
    first = git.create_publication_commit(
        git_repo, candidate_sha=first_candidate, base_sha=base, message="first"
    )
    publisher.publish_branch("ticket", first, expected_remote_sha=base)
    second_candidate = _candidate(git_repo, "second.txt", "second\n")
    second = git.create_publication_commit(
        git_repo, candidate_sha=second_candidate, base_sha=base,
        published_sha=first, message="second",
    )

    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    _git(collaborator, "push", "--force", "origin", f"{base}:refs/heads/ticket")
    with pytest.raises(RemoteBranchChanged) as raised:
        publisher.publish_branch("ticket", second, expected_remote_sha=first)
    assert raised.value.observation.observed_sha == base
    assert _git(git_repo, "ls-remote", "origin", "refs/heads/ticket").split()[0] == base


@pytest.mark.parametrize("remote_change", ["rewind", "delete", "advance"])
def test_remote_change_between_read_and_push_preserves_collaborator_ref(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_change: str
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "push", "origin", f"{base}:refs/heads/ticket")
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    first = _candidate(git_repo, "first.txt", "first\n")
    publisher.publish_branch("ticket", first, expected_remote_sha=base)
    next_head = _candidate(git_repo, "next.txt", "next\n")

    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    if remote_change == "advance":
        _git(collaborator, "config", "user.name", "Collaborator")
        _git(collaborator, "config", "user.email", "collaborator@example.invalid")
        _git(collaborator, "checkout", "--detach", first)
        collaborator_head = _candidate(collaborator, "collaborator.txt", "kept\n")
    else:
        collaborator_head = base if remote_change == "rewind" else None

    actual_write = github_publish.run_write_command
    pushes = 0

    def change_before_push(arguments: list[str], **kwargs: object):
        nonlocal pushes
        pushes += 1
        assert pushes == 1
        if remote_change == "delete":
            _git(collaborator, "push", "origin", ":refs/heads/ticket")
        else:
            force = ("--force",) if remote_change == "rewind" else ()
            _git(collaborator, "push", *force, "origin", f"{collaborator_head}:refs/heads/ticket")
        return actual_write(arguments, **kwargs)

    monkeypatch.setattr(github_publish, "run_write_command", change_before_push)
    with pytest.raises(RemoteBranchChanged) as raised:
        publisher.publish_branch("ticket", next_head, expected_remote_sha=first)
    assert pushes == 1
    assert raised.value.observation.observed_sha == collaborator_head
    remote_ref = _git(git_repo, "ls-remote", "origin", "refs/heads/ticket")
    assert (remote_ref.split()[0] if remote_ref else None) == collaborator_head


def test_parent_merge_resolution_candidate_keeps_run_and_default_ancestors(
    git_repo: Path,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    default = _candidate(git_repo, "default.txt", "default\n")
    original = _git(git_repo, "commit-tree", git.resolve(f"{default}^{{tree}}"),
                    "-p", base, "-p", default, "-m", "resolve")
    _git(git_repo, "reset", "--hard", original)
    (git_repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    candidate = git.commit_merge_resolution_candidate(
        git_repo, run_head_sha=base, default_head_sha=default, attempt=2,
    )
    assert isinstance(candidate, str)
    assert git.commit_parents(candidate) == [base, default]
    assert git.is_ancestor(base, candidate)
    assert git.is_ancestor(default, candidate)
    assert (git_repo / "default.txt").read_text() == "default\n"
    assert (git_repo / "repair.txt").read_text() == "repair\n"


def test_parent_repair_integration_record_requires_exact_result_and_ancestors(
    git_repo: Path,
) -> None:
    from agent_run.parent_integration import parent_integration_record_is_valid

    git = GitRepository(git_repo)
    run_head = git.resolve("HEAD")
    source = _candidate(git_repo, "source.txt", "source\n")
    original = _candidate(git_repo, "repair.txt", "repair\n")
    integrated_tree = git.resolve(f"{original}^{{tree}}")
    integrated = _git(git_repo, "commit-tree", integrated_tree,
                      "-p", original, "-p", source, "-m", "integrate")
    record = {
        "candidate_sha": original,
        "candidate_tree": git.resolve(f"{original}^{{tree}}"),
        "source_sha": source, "target_sha": run_head,
        "head_sha": integrated, "head_tree": integrated_tree, "conflict_paths": [],
    }
    assert parent_integration_record_is_valid(
        git, {"candidate_sha": integrated, "base_sha": run_head, "parent_integration": record},
    )
    rewritten = _git(git_repo, "commit-tree", integrated_tree, "-m", "foreign history")
    for changed in ({"head_tree": git.resolve(f"{run_head}^{{tree}}")},
                    {"source_sha": rewritten}, {"target_sha": rewritten},
                    {"candidate_sha": rewritten}):
        assert not parent_integration_record_is_valid(
            git, {"candidate_sha": integrated, "base_sha": run_head, "parent_integration": {**record, **changed}},
        )
