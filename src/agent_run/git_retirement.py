from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import re

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

    def rotate_run_repair_checkout(
        self,
        *,
        checkout: Path,
        current_branch: str,
        next_branch: str,
        candidate_sha: str,
        current_publication_sha: str,
    ) -> None:
        """Transfer a successor Candidate, then retire the completed source head."""
        for branch in (current_branch, next_branch):
            if (
                not is_managed_delivery_branch(branch)
                or self.git._run("check-ref-format", f"refs/heads/{branch}").returncode != 0
            ):
                raise GitError(f"refusing to rotate unmanaged branch {branch!r}")
        if not all(
            re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
            for sha in (candidate_sha, current_publication_sha)
        ):
            raise GitError("Run Repair rotation requires exact commit identities")
        self.git.require_clean_managed_checkout(checkout)
        if self.git.checkout_head(checkout) != candidate_sha:
            raise GitError("Run Repair rotation has a foreign Candidate")
        if not self.git.ticket_checkout_matches(checkout, next_branch):
            if not self.git.ticket_checkout_matches(checkout, current_branch):
                raise GitError("existing Run Repair checkout does not match its Job branch")
            if self.git.managed_branch_head(next_branch) is not None:
                raise GitError(f"Run Repair Job branch {next_branch!r} already exists")
            switched = self.git._run_in(checkout, "switch", "-c", next_branch, candidate_sha)
            if switched.returncode != 0:
                raise GitError(switched.stderr.strip() or "could not rotate Run Repair Job branch")
        previous = self.git.managed_branch_head(current_branch)
        if previous == current_publication_sha:
            return
        if previous != candidate_sha:
            raise GitError("completed Run Repair source branch has a foreign head")
        # Verify that the successor still holds the saved Candidate in the
        # same ref transaction that restores the completed publication ref.
        restored = self.git._run_in(
            self.git.root, "update-ref", "--no-deref", "--stdin",
            input=(
                f"verify refs/heads/{next_branch} {candidate_sha}\n"
                f"update refs/heads/{current_branch} {current_publication_sha} {candidate_sha}\n"
            ),
        )
        if restored.returncode != 0:
            raise GitError(restored.stderr.strip() or "could not retire completed Run Repair source")

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
