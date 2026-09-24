from __future__ import annotations

from pathlib import Path

from agent_run import git as git_module
from agent_run.git import GitRepository


def test_ancestry_reuses_only_proven_commit_ids(git_repo: Path, monkeypatch) -> None:
    repository = GitRepository(git_repo)
    first = repository.resolve("HEAD")
    (git_repo / "README.md").write_text("next\n", encoding="utf-8")
    repository._run("commit", "-am", "next")
    second = repository.resolve("HEAD")

    merge_base_calls = 0
    tree_resolutions = 0
    original = git_module.run_git

    def counted(arguments, **kwargs):
        nonlocal merge_base_calls, tree_resolutions
        if arguments[1:3] == ["merge-base", "--is-ancestor"]:
            merge_base_calls += 1
        if (
            len(arguments) > 3
            and arguments[1:3] == ["rev-parse", "--verify"]
            and arguments[3] == f"{first}^{{tree}}"
        ):
            tree_resolutions += 1
        return original(arguments, **kwargs)

    monkeypatch.setattr(git_module, "run_git", counted)
    assert repository.is_ancestor(first, second)
    assert repository.is_ancestor(first, second)
    assert merge_base_calls == 1
    first_tree = repository.resolve(f"{first}^{{tree}}")
    assert repository.resolve(f"{first}^{{tree}}") == first_tree
    assert tree_resolutions == 1
    assert repository.is_ancestor(second, "refs/heads/main")
    repository._run("reset", "--hard", first)
    assert not repository.is_ancestor(second, "refs/heads/main")
    assert merge_base_calls == 3
