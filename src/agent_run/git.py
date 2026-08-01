from __future__ import annotations

import shutil
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

    def resolve(self, reference: str) -> str:
        resolved = self._resolve(reference)
        if resolved is None:
            raise GitError(f"Git reference {reference!r} does not exist")
        return resolved

    def checkout_head(self, checkout: Path) -> str:
        return self._resolve_in(checkout, "HEAD")

    def prepare_ticket_checkout(
        self,
        *,
        branch: str,
        base_sha: str,
        checkout: Path,
    ) -> None:
        if checkout.exists():
            if self.ticket_checkout_matches(checkout, branch):
                return
            raise GitError("existing ticket checkout does not match its branch")
        self.remove_worktree(checkout)
        existing = self._resolve(f"refs/heads/{branch}")
        if existing is None:
            created = self._run("branch", branch, base_sha)
            if created.returncode != 0:
                raise GitError(created.stderr.strip() or "could not create ticket branch")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        added = self._run("worktree", "add", "--force", str(checkout), branch)
        if added.returncode != 0:
            raise GitError(added.stderr.strip() or "could not create ticket checkout")

    def ticket_checkout_matches(self, checkout: Path, branch: str) -> bool:
        if not checkout.exists():
            return False
        branch_result = self._run_in(
            checkout, "rev-parse", "--abbrev-ref", "HEAD"
        )
        branch_head = self._resolve(f"refs/heads/{branch}")
        checkout_result = self._run_in(
            checkout, "rev-parse", "--verify", "HEAD"
        )
        checkout_head = (
            checkout_result.stdout.strip()
            if checkout_result.returncode == 0
            else None
        )
        return (
            branch_result.returncode == 0
            and branch_result.stdout.strip() == branch
            and branch_head is not None
            and branch_head == checkout_head
        )

    def delete_branch(self, branch: str) -> None:
        if self._resolve(f"refs/heads/{branch}") is None:
            return
        deleted = self._run("branch", "-D", branch)
        if deleted.returncode != 0:
            raise GitError(
                deleted.stderr.strip()
                or f"could not delete branch {branch}"
            )

    def prepare_validation_checkout(
        self, *, head_sha: str, checkout: Path
    ) -> None:
        self.remove_worktree(checkout)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        added = self._run(
            "worktree",
            "add",
            "--detach",
            "--force",
            str(checkout),
            head_sha,
        )
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not create validation checkout"
            )

    def commit_candidate(
        self, checkout: Path, *, ticket_number: int, attempt: int
    ) -> str | None:
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect ticket checkout")
        if not status.stdout.strip():
            expected_message = f"chore(ticket-{ticket_number}): candidate {attempt}"
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            if (
                subject.returncode == 0
                and subject.stdout.strip() == expected_message
            ):
                return self._resolve_in(checkout, "HEAD")
            return None
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(added.stderr.strip() or "could not stage candidate")
        committed = self._run_in(
            checkout,
            "commit",
            "-m",
            f"chore(ticket-{ticket_number}): candidate {attempt}",
        )
        if committed.returncode != 0:
            raise GitError(committed.stderr.strip() or "could not commit candidate")
        return self._resolve_in(checkout, "HEAD")

    def create_publication_commit(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        base_sha: str,
        message: str,
    ) -> str:
        tree = self._resolve_in(checkout, f"{candidate_sha}^{{tree}}")
        created = self._run_in(
            checkout,
            "commit-tree",
            tree,
            "-p",
            base_sha,
            "-m",
            message,
        )
        if created.returncode != 0:
            raise GitError(created.stderr.strip() or "could not create publication commit")
        publication_sha = created.stdout.strip()
        reset = self._run_in(checkout, "reset", "--hard", publication_sha)
        if reset.returncode != 0:
            raise GitError(reset.stderr.strip() or "could not install publication commit")
        publication_tree = self._resolve_in(
            checkout, f"{publication_sha}^{{tree}}"
        )
        if publication_tree != tree:
            raise GitError("publication commit changed the accepted candidate tree")
        return publication_sha

    def diff_between(self, base_sha: str, head_sha: str) -> str:
        result = self._run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            head_sha,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not read ticket diff")
        return result.stdout

    def commit_subject(self, sha: str) -> str:
        result = self._run("log", "-1", "--format=%s", sha)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not read commit subject")
        return result.stdout.strip()

    def commit_parents(self, sha: str) -> list[str]:
        result = self._run("show", "-s", "--format=%P", sha)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not read commit parents")
        return result.stdout.strip().split()

    def remove_worktree(self, checkout: Path) -> None:
        if checkout.exists():
            removed = self._run("worktree", "remove", "--force", str(checkout))
            if removed.returncode != 0:
                listed = self._run("worktree", "list", "--porcelain")
                marker = f"worktree {checkout.resolve()}"
                if listed.returncode != 0 or marker in listed.stdout.splitlines():
                    raise GitError(
                        removed.stderr.strip() or "could not remove ticket checkout"
                    )
                shutil.rmtree(checkout)
        self._run("worktree", "prune")

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

    def _resolve_in(self, directory: Path, reference: str) -> str:
        result = self._run_in(directory, "rev-parse", "--verify", reference)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or f"could not resolve {reference}")
        return result.stdout.strip()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _run_in(
        directory: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=directory,
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

    def retire_ticket_branch(self, branch: str, checkout: Path) -> None:
        self.git.remove_worktree(checkout)
        self.git.delete_branch(branch)
