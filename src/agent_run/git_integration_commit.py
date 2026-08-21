from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from agent_run.git_errors import GitError


class GitCommitOperations(Protocol):
    def checkout_head(self, checkout: Path) -> str: ...

    def commit_parents(self, sha: str) -> list[str]: ...

    def commit_subject(self, sha: str) -> str: ...

    def _resolve_in(self, directory: Path, reference: str) -> str: ...

    def _resolve_in_optional(self, directory: Path, reference: str) -> str | None: ...

    def _run_in(
        self, directory: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]: ...


class IntegrationRepairCommitGit:
    def __init__(self, git: GitCommitOperations) -> None:
        self._git = git

    def commit_merge_resolution_candidate(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        attempt: int,
        squash_candidate_sha: str | None = None,
        expected_conflict_paths: tuple[str, ...] = (),
    ) -> str | None:
        """Publisher-owned two-parent commit of a resolved integration tree."""

        expected_message = f"chore(run-repair): merge-resolution candidate {attempt}"
        checkout_head = self._git.checkout_head(checkout)
        status = self._git._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Integration-repair Worktree"
            )
        merge_head = self._git._resolve_in_optional(checkout, "MERGE_HEAD")
        if not status.stdout.strip() and merge_head is None:
            if (
                self._git.commit_parents(checkout_head) == [run_head_sha, default_head_sha]
                and self._git.commit_subject(checkout_head) == expected_message
            ):
                return checkout_head
            return None
        if merge_head is not None:
            allowed_head = checkout_head == run_head_sha or (
                isinstance(squash_candidate_sha, str)
                and checkout_head == squash_candidate_sha
                and self._git.commit_parents(squash_candidate_sha) == [run_head_sha]
            )
            if not allowed_head:
                raise GitError("Merge-resolution Candidate has a stale Run parent")
            if merge_head != default_head_sha:
                raise GitError("Merge-resolution Candidate has a stale default parent")
            self._require_repaired_unmerged_paths(
                checkout, expected_conflict_paths=expected_conflict_paths
            )
        elif self._git.commit_parents(checkout_head) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution follow-up has a stale parent boundary")
        added = self._git._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not stage resolved integration tree"
            )
        tree = self._git._run_in(checkout, "write-tree")
        if tree.returncode != 0:
            raise GitError(
                tree.stderr.strip() or "integration conflict is not fully resolved"
            )
        created = self._git._run_in(
            checkout,
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            run_head_sha,
            "-p",
            default_head_sha,
            "-m",
            expected_message,
        )
        if created.returncode != 0:
            raise GitError(
                created.stderr.strip() or "could not create Merge-resolution Candidate"
            )
        candidate = created.stdout.strip()
        reset = self._git._run_in(checkout, "reset", "--hard", candidate)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not install Merge-resolution Candidate"
            )
        if self._git.commit_parents(candidate) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution Candidate parents changed unexpectedly")
        return candidate

    def _require_repaired_unmerged_paths(
        self, checkout: Path, *, expected_conflict_paths: tuple[str, ...]
    ) -> None:
        """Reject paths whose worktree content is still Git's raw conflict file."""

        unmerged = self._git._run_in(
            checkout, "diff", "--name-only", "-z", "--diff-filter=U"
        )
        if unmerged.returncode != 0:
            raise GitError("could not inspect unresolved integration paths")
        paths = set(expected_conflict_paths)
        paths.update(value for value in unmerged.stdout.split("\0") if value)
        unchanged: list[str] = []
        for path in sorted(paths):
            compared = self._git._run_in(
                checkout, "diff", "--quiet", "AUTO_MERGE", "--", path
            )
            if compared.returncode == 0:
                unchanged.append(path)
            elif compared.returncode != 1:
                raise GitError(
                    "could not verify that an integration conflict was repaired"
                )
        if unchanged:
            raise GitError(
                "unresolved conflict paths were not repaired: "
                + ", ".join(unchanged)
            )
        marker_paths = [
            path
            for path in sorted(paths)
            if self._worktree_path_has_conflict_markers(checkout, path)
        ]
        if marker_paths:
            raise GitError(
                "resolved integration paths retain conflict markers: "
                + ", ".join(marker_paths)
            )

    @staticmethod
    def _worktree_path_has_conflict_markers(checkout: Path, path: str) -> bool:
        candidate = checkout / path
        if not candidate.is_file() or candidate.is_symlink():
            return False
        marker_lines = (b"<<<<<<< ", b"=======", b">>>>>>> ")
        return any(
            line == marker_lines[1]
            or line.startswith(marker_lines[0])
            or line.startswith(marker_lines[2])
            for line in candidate.read_bytes().splitlines()
        )

    def create_merge_resolution_publication_commit(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        run_head_sha: str,
        default_head_sha: str,
        message: str,
    ) -> str:
        """Rewrite only metadata while retaining the accepted tree and parents."""

        if self._git.commit_parents(candidate_sha) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution Candidate parents do not match its boundary")
        tree = self._git._resolve_in(checkout, f"{candidate_sha}^{{tree}}")
        created = self._git._run_in(
            checkout,
            "commit-tree",
            tree,
            "-p",
            run_head_sha,
            "-p",
            default_head_sha,
            "-m",
            message,
        )
        if created.returncode != 0:
            raise GitError(
                created.stderr.strip()
                or "could not create Merge-resolution publication commit"
            )
        publication = created.stdout.strip()
        reset = self._git._run_in(checkout, "reset", "--hard", publication)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip()
                or "could not install Merge-resolution publication commit"
            )
        if (
            self._git._resolve_in(checkout, f"{publication}^{{tree}}") != tree
            or self._git.commit_parents(publication) != [run_head_sha, default_head_sha]
        ):
            raise GitError("Merge-resolution publication changed the accepted boundary")
        return publication
