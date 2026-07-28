from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def discover(cls, start: Path) -> GitRepository:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "not inside a Git repository")
        return cls(Path(result.stdout.strip()).resolve())

    def resolve_base(self, default_branch: str, expected_sha: str | None) -> str:
        if expected_sha is not None:
            if not self._commit_exists(expected_sha):
                self._fetch_default_branch(default_branch)
            if not self._commit_exists(expected_sha):
                raise GitError(
                    f"GitHub default head {expected_sha} is unavailable in the local repository"
                )
            return expected_sha

        for reference in (
            f"refs/remotes/origin/{default_branch}",
            f"refs/heads/{default_branch}",
        ):
            resolved = self._resolve(reference)
            if resolved is not None:
                return resolved
        raise GitError(f"default branch {default_branch!r} is unavailable")

    def ensure_run_branch(self, branch: str, base_sha: str) -> None:
        existing = self._resolve(f"refs/heads/{branch}")
        if existing is not None:
            ancestry = self._run(
                "merge-base", "--is-ancestor", base_sha, existing
            )
            if ancestry.returncode != 0:
                raise GitError(
                    f"run branch {branch!r} no longer descends from base {base_sha}"
                )
            return
        result = self._run("branch", branch, base_sha)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or f"could not create branch {branch}")

    def _fetch_default_branch(self, default_branch: str) -> None:
        result = self._run("fetch", "--no-tags", "origin", default_branch)
        if result.returncode != 0:
            raise GitError(
                result.stderr.strip() or f"could not fetch origin/{default_branch}"
            )

    def _commit_exists(self, sha: str) -> bool:
        return (
            self._run("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
        )

    def _resolve(self, reference: str) -> str | None:
        result = self._run("rev-parse", "--verify", reference)
        return result.stdout.strip() if result.returncode == 0 else None

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )


class Publisher:
    """Issue #2 中唯一允许修改 Git 引用的边界。"""

    def __init__(self, git: GitRepository) -> None:
        self.git = git

    def resolve_base(
        self, default_branch: str, expected_sha: str | None
    ) -> str:
        return self.git.resolve_base(default_branch, expected_sha)

    def ensure_run_branch(self, branch: str, base_sha: str) -> None:
        self.git.ensure_run_branch(branch, base_sha)
