from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_run.git_cleanup_lock import hold_cleanup_head
from agent_run.git_errors import GitError

if TYPE_CHECKING:
    from agent_run.git import GitRepository


MANAGED_DELIVERY_BRANCH_PREFIXES = ("agent-run/", "agent-run-repair/")


def is_managed_delivery_branch(branch: str) -> bool:
    return branch.startswith(MANAGED_DELIVERY_BRANCH_PREFIXES)


class ManagedBranchRetirementGit:
    """Keep source-ref retirement and completed checkout protection together."""

    def __init__(self, git: GitRepository) -> None:
        self.git = git

    def managed_branch_head(self, branch: str) -> str | None:
        """Distinguish an absent ref from an unreadable local ref database."""
        ref = f"refs/heads/{branch}"
        result = self.git._run("for-each-ref", "--format=%(refname)%09%(objectname)", ref)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not inspect local branch")
        for line in result.stdout.splitlines():
            name, _, head = line.partition("\t")
            if name == ref:
                if not head:
                    raise GitError("local branch has no readable head")
                return head
        return None

    def delete_managed_delivery_branch(self, branch: str, *, expected_head_sha: str) -> None:
        """Atomically delete the authorized head, retaining newer committed work."""
        if not is_managed_delivery_branch(branch):
            raise GitError(f"refusing to delete unmanaged branch {branch!r}")
        if not expected_head_sha:
            raise GitError("managed branch cleanup is missing its expected source head")
        observed = self.git.managed_branch_head(branch)
        if observed is None:
            return
        if observed != expected_head_sha:
            raise GitError(f"preserved local branch {branch!r}: source head changed")
        listed = self.git._run("worktree", "list", "--porcelain")
        if listed.returncode != 0:
            raise GitError(listed.stderr.strip() or "could not inspect worktrees")
        if f"branch refs/heads/{branch}" in listed.stdout.splitlines():
            raise GitError(f"preserved local branch {branch!r}: branch is checked out")
        deleted = self.git._run("update-ref", "-d", "--no-deref", f"refs/heads/{branch}", expected_head_sha)
        if deleted.returncode != 0:
            raise GitError(deleted.stderr.strip() or f"could not delete branch {branch}")

    def remove_completed_worktree(
        self, checkout: Path, *, branch: str, expected_head_sha: str
    ) -> None:
        """Retire a completed source checkout without discarding later work."""
        head = self.git.managed_branch_head(branch)
        if head is not None and head != expected_head_sha:
            raise GitError(f"preserved local branch {branch!r}: source head changed")
        if not checkout.exists():
            return
        self.git.require_clean_managed_checkout(checkout)
        if self.git.checkout_head(checkout) != expected_head_sha:
            raise GitError(f"preserved completed checkout {checkout}: source head changed")
        # Git rechecks cleanliness itself. --force would erase files written
        # after our preflight, so completed delivery cleanup never uses it.
        self.git._check_write_guard()
        with hold_cleanup_head(
            self.git.root, checkout=checkout, branch=branch, expected_head_sha=expected_head_sha
        ):
            # HEAD itself is locked as well as the source ref: a same-SHA
            # switch/detach cannot redirect a later commit around the ref lock.
            symbolic = self.git._run_in(checkout, "symbolic-ref", "--quiet", "HEAD")
            if symbolic.returncode == 0:
                if symbolic.stdout.strip() != f"refs/heads/{branch}":
                    raise GitError(f"preserved completed checkout {checkout}: branch identity changed")
            elif symbolic.returncode != 1 or self.git.is_managed_development_checkout(checkout):
                raise GitError(f"preserved completed checkout {checkout}: HEAD is not the managed branch")
            removed = self.git._run("worktree", "remove", str(checkout))
            if removed.returncode != 0:
                raise GitError(removed.stderr.strip() or "could not remove completed checkout")
