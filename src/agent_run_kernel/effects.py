"""Mechanical Git verbs shared by managed delivery and isolated fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Callable, Iterable

from agent_run.git_errors import GitError
from agent_run.git_output import run_git

from .model import Commit, KernelError


class LocalGit:
    def __init__(self, root: Path, authorize: Callable[[], None] | None = None) -> None:
        self.root = root
        self.authorize = authorize
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"})

    def run(self, *arguments: str, cwd: Path | None = None, input_text: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ("git", "-C", str(cwd or self.root), *arguments)
        if arguments and (arguments[0] in {"init", "config", "add", "commit", "commit-tree", "reset", "update-ref", "worktree", "switch", "write-tree", "merge-tree", "merge"}
                          or arguments[0] == "hash-object" and "-w" in arguments):
            if self.authorize is not None:
                self.authorize()
        try:
            result = run_git(list(command), cwd=cwd or self.root, input=input_text,
                             timeout=30, environment=self.env)
        except GitError as error:
            raise KernelError("git_operation_failed", str(error)) from error
        if check and result.returncode:
            raise KernelError("git_operation_failed", repr({"command": command, "returncode": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr}))
        return result

    def _run_in(self, directory: Path, *arguments: str, input: str | None = None,
                timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """The existing exact-object helper's narrow Git command protocol."""
        result = self.run(*arguments, cwd=directory, input_text=input, check=False)
        if result.returncode and not (arguments[:2] == ("config", "--get") and result.returncode == 1):
            raise KernelError("git_operation_failed", result.stderr)
        return result

    def conflict_checkout(self, checkout: Path, target: str) -> dict[str, object]:
        result = self.run("merge", "--no-commit", "--no-ff", target, cwd=checkout, check=False)
        return {"command": ["git", "merge", "--no-commit", "--no-ff", target], "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "head": self.run("rev-parse", "HEAD", cwd=checkout).stdout.strip(),
                "merge_head": self.run("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=checkout, check=False).stdout.strip()}

    def commit(self, sha: str) -> Commit:
        resolved = self.run("rev-parse", "--verify", f"{sha}^{{commit}}").stdout.strip()
        tree = self.run("rev-parse", f"{resolved}^{{tree}}").stdout.strip()
        return Commit(resolved, tree)

    def parents(self, sha: str) -> tuple[str, ...]:
        return tuple(self.run("show", "-s", "--format=%P", sha).stdout.strip().split())

    def is_ancestor(self, before: str, after: str) -> bool:
        result = self.run("merge-base", "--is-ancestor", before, after, check=False)
        if result.returncode not in {0, 1}:
            raise KernelError("git_ancestry_unknown")
        return result.returncode == 0

    def make_commit(self, tree: str, parents: Iterable[str], message: str) -> Commit:
        arguments: list[str] = ["commit-tree", tree]
        parents = tuple(parents)
        for parent in parents:
            arguments.extend(("-p", parent))
        sha = self.run(*arguments, input_text=message + "\n").stdout.strip()
        if self.parents(sha) != parents:
            raise KernelError("commit_parents_changed")
        return self.commit(sha)

    def candidate(self, checkout: Path, before: str, token: str,
                  parents: tuple[str, ...]) -> tuple[Commit, bool]:
        if not parents or parents[0] != before or len(parents) not in {1, 2} or len(set(parents)) != len(parents):
            raise KernelError("candidate_parent_identity")
        actual = self.run("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        merge = self.run("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=checkout, check=False)
        if merge.returncode not in {0, 1}:
            raise KernelError("candidate_merge_unknown")
        merge_head = merge.stdout.strip() if merge.returncode == 0 else None
        if actual != before:
            message = self.run("show", "-s", "--format=%B", actual).stdout.rstrip("\n")
            if (message != f"candidate {token}" or self.parents(actual) != parents or merge_head is not None
                    or self.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout):
                raise KernelError("candidate_checkout_changed")
            self.run("diff", "--check", before, actual, cwd=checkout)
            return self.commit(actual), False
        if merge_head != (parents[1] if len(parents) == 2 else None):
            raise KernelError("candidate_merge_changed")
        self.run("add", "--all", cwd=checkout)
        self.run("diff", "--cached", "--check", cwd=checkout)
        tree = self.run("write-tree", cwd=checkout).stdout.strip()
        if tree == self.commit(before).tree and len(parents) == 1:
            return self.commit(before), True
        result = self.make_commit(tree, parents, f"candidate {token}")
        self.run("reset", "--hard", result.sha, cwd=checkout)
        return result, False

    def combine(self, candidate: str, target: str, token: str) -> Commit:
        if self.is_ancestor(target, candidate):
            return self.commit(candidate)
        result = self.run("merge-tree", "--write-tree", candidate, target, check=False)
        if result.returncode:
            raise KernelError("integration_failed", repr({"command": ["git", "merge-tree", "--write-tree", candidate, target],
                              "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}))
        tree = result.stdout.splitlines()[0]
        return self.make_commit(tree, (candidate, target), f"integration {token}")

    def install_checkout(self, checkout: Path, sha: str) -> None:
        if self.run("status", "--porcelain", cwd=checkout).stdout:
            raise KernelError("dirty_managed_checkout", str(checkout))
        self.run("reset", "--hard", sha, cwd=checkout)

    def compare_and_swap(self, ref: str, before: str, after: str) -> None:
        if not self.is_ancestor(before, after):
            raise KernelError("non_append_write")
        self.run("update-ref", ref, after, before)
