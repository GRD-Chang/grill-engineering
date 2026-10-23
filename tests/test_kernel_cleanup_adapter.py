"""Cleanup mechanics preserve the exact resources authorized by an intent."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.model import Effect, Execution
from agent_run.errors import KernelError
from agent_run.adapters.cleanup import cleanup_local_ref, cleanup_remote_ref, cleanup_worktree, inspect_local_ref, inspect_remote_ref, inspect_worktree
from agent_run.adapters.git_objects import LocalGit


REF = "refs/heads/task"


def _intent(git: LocalGit, operation: str, **payload: object) -> Effect:
    base = git.commit("HEAD")
    return Effect(
        "cleanup-original", "ticket:3", operation, "example/repository",
        REF, "refs/heads/run", base.sha, None, None, base, 1,
        "offline-publisher", {"branch_ref": REF, "expected_head": base.sha, **payload},
        Execution("run", "executor", 1),
    )


def test_local_ref_cleanup_uses_original_source_not_a_different_merge(git_repo: Path) -> None:
    git = LocalGit(git_repo)
    base = git.commit("HEAD")
    source = git.make_commit(base.tree, (base.sha,), "published source")
    merged = git.make_commit(base.tree, (base.sha,), "independent squash merge")
    assert source.sha != merged.sha
    git.run("update-ref", REF, source.sha)
    effect = _intent(git, "cleanup_local_ref", expected_head=source.sha)
    wrong = replace(effect, payload={**effect.payload, "expected_head": merged.sha})
    assert inspect_local_ref(git, wrong).disposition == "third"
    assert cleanup_local_ref(git, wrong).disposition == "third"
    assert git.commit(REF) == source
    assert inspect_local_ref(git, effect).disposition == "prior"
    authorized: list[str] = []
    git.authorize = lambda: authorized.append("current")
    assert cleanup_local_ref(git, effect).disposition == "target"
    assert authorized
    assert git.run("for-each-ref", "--format=%(refname)", REF).stdout == ""
    assert inspect_local_ref(git, effect).disposition == "target"
    assert cleanup_local_ref(git, effect).disposition == "target"


def test_local_cleanup_rejects_another_effect_and_authority_revocation(git_repo: Path) -> None:
    git = LocalGit(git_repo)
    effect = _intent(git, "cleanup_local_ref")
    git.run("update-ref", REF, effect.before_sha)
    assert cleanup_local_ref(git, replace(effect, operation="push")).disposition == "third"
    with pytest.raises(KernelError, match="cleanup_authority_missing"):
        cleanup_local_ref(git, effect)

    def revoked() -> None:
        raise KernelError("executor_fenced")

    git.authorize = revoked
    with pytest.raises(KernelError, match="executor_fenced"):
        cleanup_local_ref(git, effect)
    assert inspect_local_ref(git, effect).disposition == "prior"
    assert git.commit(REF).sha == effect.before_sha


def test_local_cleanup_preserves_a_commit_added_at_atomic_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = LocalGit(git_repo)
    effect = _intent(git, "cleanup_local_ref")
    git.run("update-ref", REF, effect.before_sha)
    later = git.make_commit(git.commit(REF).tree, (effect.before_sha,), "later work")
    actual_run = LocalGit.run
    writes: list[str] = []

    def competing_write(self: LocalGit, *arguments: str, **options):
        if arguments[:3] == ("update-ref", "--no-deref", "-d"):
            writes.append(arguments[-1])
            actual_run(self, "update-ref", REF, later.sha, effect.before_sha)
        return actual_run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", competing_write)
    git.authorize = lambda: None
    assert cleanup_local_ref(git, effect).disposition == "third"
    assert writes == [effect.before_sha]
    assert git.commit(REF) == later


def test_ref_read_failure_is_unknown_without_dispatch(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = LocalGit(git_repo)
    effect = _intent(git, "cleanup_local_ref")
    git.run("update-ref", REF, effect.before_sha)
    actual_run = LocalGit.run
    authorized: list[str] = []

    def unavailable(self: LocalGit, *arguments: str, **options):
        if arguments[0] == "for-each-ref":
            raise KernelError("git_operation_failed", "ref database unreadable")
        return actual_run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", unavailable)
    git.authorize = lambda: authorized.append("write")
    assert inspect_local_ref(git, effect).disposition == "unknown"
    assert cleanup_local_ref(git, effect).disposition == "unknown"
    assert authorized == []
    assert git.commit(REF).sha == effect.before_sha


def _remote(git: LocalGit, tmp_path: Path) -> str:
    remote = str(tmp_path / "origin.git")
    git.run("init", "--bare", remote)
    git.run("push", remote, f"HEAD:{REF}")
    return remote


@pytest.mark.parametrize("outcome", ["missing", "deleted_response_lost", "unchanged_response_lost", "changed_response_lost", "read_error"])
def test_remote_cleanup_reconciles_original_head_after_unknown_write(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    git = LocalGit(git_repo)
    remote = _remote(git, tmp_path)
    effect = _intent(git, "cleanup_remote_ref", remote=remote)
    later = git.make_commit(git.commit("HEAD").tree, (effect.before_sha,), "another publisher")
    if outcome == "missing":
        git.run("push", remote, f":{REF}")
    actual_run = LocalGit.run
    dispatched: list[tuple[str, ...]] = []

    def lose_response(self: LocalGit, *arguments: str, **options):
        if arguments[0] == "ls-remote" and outcome == "read_error":
            raise OSError("remote unreadable")
        if arguments[0] == "push":
            dispatched.append(arguments)
            if outcome == "deleted_response_lost":
                actual_run(self, *arguments, **options)
            elif outcome == "changed_response_lost":
                actual_run(self, "push", remote, f"{later.sha}:{REF}")
            raise OSError("lost response")
        return actual_run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", lose_response)
    git.authorize = lambda: None
    result = cleanup_remote_ref(git, effect)
    expected = {"missing": "target", "deleted_response_lost": "target",
                "unchanged_response_lost": "prior", "changed_response_lost": "third", "read_error": "unknown"}
    assert result.disposition == expected[outcome]
    assert len(dispatched) == (0 if outcome in {"missing", "read_error"} else 1)
    assert all(f"--force-with-lease={REF}:{effect.before_sha}" in command for command in dispatched)
    if outcome == "unchanged_response_lost":
        actual_run(git, "push", remote, f"{later.sha}:{REF}")
        assert cleanup_remote_ref(git, effect).disposition == "third"
        assert len(dispatched) == 1
    if outcome in {"unchanged_response_lost", "changed_response_lost"}:
        assert later.sha in actual_run(git, "ls-remote", remote, REF).stdout


def test_remote_cleanup_preserves_commit_added_at_delete_dispatch(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = LocalGit(git_repo)
    remote = _remote(git, tmp_path)
    effect = _intent(git, "cleanup_remote_ref", remote=remote)
    later = git.make_commit(git.commit("HEAD").tree, (effect.before_sha,), "new remote work")
    actual_run = LocalGit.run
    dispatched: list[tuple[str, ...]] = []

    def competing_push(self: LocalGit, *arguments: str, **options):
        if arguments[0] == "push":
            dispatched.append(arguments)
            actual_run(self, "push", remote, f"{later.sha}:{REF}")
        return actual_run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", competing_push)
    git.authorize = lambda: None
    assert inspect_remote_ref(git, effect).disposition == "prior"
    assert cleanup_remote_ref(git, effect).disposition == "third"
    assert len(dispatched) == 1
    assert f"--force-with-lease={REF}:{effect.before_sha}" in dispatched[0]
    assert later.sha in actual_run(git, "ls-remote", remote, REF).stdout


def _checkout(git: LocalGit, tmp_path: Path, *, attached: bool = False) -> tuple[Path, Effect]:
    owned = tmp_path / "owned"
    owned.mkdir()
    checkout = owned / "development"
    git.run("update-ref", REF, git.commit("HEAD").sha)
    git.run("worktree", "add", *( () if attached else ("--detach",)), str(checkout), REF)
    common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return checkout, _intent(git, "cleanup_worktree", checkout=str(checkout), owned_root=str(owned), common_git_dir=common)


@pytest.mark.parametrize("attached", [False, True])
def test_worktree_cleanup_is_exact_and_idempotent(git_repo: Path, tmp_path: Path, attached: bool) -> None:
    git = LocalGit(git_repo)
    checkout, effect = _checkout(git, tmp_path, attached=attached)
    assert inspect_worktree(git, effect).disposition == "prior"
    git.authorize = lambda: None
    assert cleanup_worktree(git, effect).disposition == "target"
    assert not checkout.exists()
    assert git.commit(REF).sha == effect.before_sha
    assert cleanup_worktree(git, effect).disposition == "target"


@pytest.mark.parametrize("foreign", ["owned_root", "common_git_dir", "head", "dirty", "symbolic_branch"])
def test_worktree_cleanup_preserves_foreign_or_changed_resources(
    git_repo: Path, tmp_path: Path, foreign: str,
) -> None:
    git = LocalGit(git_repo)
    checkout, effect = _checkout(git, tmp_path)
    if foreign in {"owned_root", "common_git_dir"}:
        effect = replace(effect, payload={**effect.payload, foreign: str(tmp_path / "other")})
    elif foreign == "head":
        later = git.make_commit(git.commit("HEAD").tree, (effect.before_sha,), "new checkout work")
        git.run("reset", "--hard", later.sha, cwd=checkout)
    elif foreign == "dirty":
        (checkout / "untracked.txt").write_text("keep this work")
    else:
        git.run("switch", "-c", "foreign", cwd=checkout)
    git.authorize = lambda: None
    assert cleanup_worktree(git, effect).disposition == "third"
    assert checkout.is_dir()
    if foreign == "dirty":
        assert (checkout / "untracked.txt").read_text() == "keep this work"


@pytest.mark.parametrize("change", ["dirty", "commit"])
def test_worktree_cleanup_rechecks_changes_at_remove(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    git = LocalGit(git_repo)
    checkout, effect = _checkout(git, tmp_path)
    later = git.make_commit(git.commit("HEAD").tree, (effect.before_sha,), "new checkout commit")
    actual_run = LocalGit.run
    attempts: list[tuple[str, ...]] = []

    def concurrent_work(self: LocalGit, *arguments: str, **options):
        if arguments[:2] == ("worktree", "remove"):
            attempts.append(arguments)
            if change == "dirty":
                (checkout / "unsaved.txt").write_text("uncommitted work")
            else:
                changed = actual_run(self, "update-ref", "--no-deref", "HEAD", later.sha,
                                     effect.before_sha, cwd=checkout, check=False)
                assert changed.returncode != 0  # Prepared HEAD lock rejects the competing writer.
                (checkout / "unsaved.txt").write_text("blocked writer's unsaved work")
        return actual_run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", concurrent_work)
    git.authorize = lambda: None
    assert cleanup_worktree(git, effect).disposition == "third"
    assert len(attempts) == 1
    assert "--force" not in attempts[0]
    assert checkout.is_dir() and (checkout / "unsaved.txt").exists()
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == effect.before_sha
