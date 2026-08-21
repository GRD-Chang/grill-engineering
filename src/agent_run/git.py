from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_run.github_retry import run_read_command


MANAGED_DELIVERY_BRANCH_PREFIXES = (
    "agent-run/",
    "agent-run-repair/",
)


def is_managed_delivery_branch(branch: str) -> bool:
    return branch.startswith(MANAGED_DELIVERY_BRANCH_PREFIXES)


class GitError(RuntimeError):
    pass


class MergeConflictError(GitError):
    """The exact default/Run merge needs semantic conflict resolution."""


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

    def prepare_run_repair_checkout(
        self, *, branch: str, checkout: Path
    ) -> None:
        """Check out the existing Run Branch for a bounded repair attempt."""
        if checkout.exists():
            if self.ticket_checkout_matches(checkout, branch):
                return
            raise GitError("existing Run Repair checkout does not match Run Branch")
        self.remove_worktree(checkout)
        if self._resolve(f"refs/heads/{branch}") is None:
            raise GitError(f"Run Branch {branch!r} does not exist")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        added = self._run("worktree", "add", "--force", str(checkout), branch)
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not create Run Repair checkout"
            )

    def rotate_run_repair_checkout(
        self,
        *,
        checkout: Path,
        current_branch: str,
        next_branch: str,
        candidate_sha: str,
    ) -> None:
        """Move one persistent repair checkout onto a fresh managed Job branch."""

        if self.ticket_checkout_matches(checkout, next_branch):
            if self.checkout_head(checkout) != candidate_sha:
                raise GitError("rotated Run Repair checkout has a foreign Candidate")
            return
        if not self.ticket_checkout_matches(checkout, current_branch):
            raise GitError("existing Run Repair checkout does not match its Job branch")
        if not is_managed_delivery_branch(next_branch):
            raise GitError(f"refusing to create unmanaged branch {next_branch!r}")
        if self._resolve(f"refs/heads/{next_branch}") is not None:
            raise GitError(f"Run Repair Job branch {next_branch!r} already exists")
        switched = self._run_in(
            checkout, "switch", "-c", next_branch, candidate_sha
        )
        if switched.returncode != 0:
            raise GitError(
                switched.stderr.strip() or "could not rotate Run Repair Job branch"
            )

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

    def delete_managed_delivery_branch(self, branch: str) -> None:
        """Delete only a branch shape owned by the Delivery Run controller."""
        if not is_managed_delivery_branch(branch):
            raise GitError(f"refusing to delete unmanaged branch {branch!r}")
        self.delete_branch(branch)

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

    def prepare_expected_merge_checkout(
        self,
        *,
        default_head_sha: str,
        run_head_sha: str,
        checkout: Path,
    ) -> None:
        """Create a disposable checkout containing the actual merge preview."""
        self.prepare_validation_checkout(head_sha=default_head_sha, checkout=checkout)
        merged = self._run_in(
            checkout, "merge", "--no-commit", "--no-ff", run_head_sha
        )
        if merged.returncode == 0:
            return
        unmerged = self._run_in(
            checkout, "diff", "--name-only", "--diff-filter=U"
        )
        self._run_in(checkout, "merge", "--abort")
        evidence = "\n".join(
            detail
            for detail in (merged.stdout.strip(), merged.stderr.strip())
            if detail
        )
        if unmerged.returncode != 0 or not unmerged.stdout.strip():
            raise GitError(
                evidence or "Run Branch merge preview failed before conflict detection"
            )
        raise MergeConflictError(
            evidence or "Run Branch cannot be merged into the current default branch"
        )

    def prepare_integration_repair_checkout(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        squash_candidate_sha: str | None = None,
        allow_clean_merge: bool = False,
        allow_staged_resolution: bool = False,
    ) -> str:
        """Prepare or verify the persistent, writable three-way conflict scene."""

        checkout_head = self.checkout_head(checkout)
        if checkout_head != run_head_sha:
            if self.commit_parents(checkout_head) == [run_head_sha, default_head_sha]:
                return ""
            if not (
                isinstance(squash_candidate_sha, str)
                and checkout_head == squash_candidate_sha
                and self.commit_parents(checkout_head) == [run_head_sha]
            ):
                raise GitError("Integration-repair Worktree is not at an allowed parent")
        merge_head = self._resolve_in_optional(checkout, "MERGE_HEAD")
        if merge_head is not None:
            if merge_head != default_head_sha:
                raise GitError(
                    "Integration-repair Worktree targets a different default head"
                )
            unmerged = self._run_in(
                checkout, "diff", "--name-only", "--diff-filter=U"
            )
            if unmerged.returncode != 0:
                raise GitError("could not inspect Integration-repair Worktree")
            if not unmerged.stdout.strip() and allow_clean_merge:
                return ""
            if not unmerged.stdout.strip() and allow_staged_resolution:
                self._require_staged_integration_resolution(checkout)
                return ""
            return self._integration_conflict_evidence(checkout)
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Integration-repair Worktree"
            )
        if status.stdout.strip():
            raise GitError("Integration-repair Worktree contains unrelated changes")
        merged = self._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if merged.returncode == 0:
            self._run_in(checkout, "merge", "--abort")
            raise GitError("Integration-repair Worktree no longer reproduces a conflict")
        if self._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
            raise GitError("Git did not preserve the expected integration conflict state")
        return self._integration_conflict_evidence(checkout, merge_result=merged)

    def convert_squash_candidate_to_integration_repair(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
    ) -> str:
        """Prepare the real conflict between one squash Candidate and latest default."""

        if self.commit_parents(candidate_sha) != [run_head_sha]:
            raise GitError("Squash Candidate does not have the exact Run parent")
        checkout_head = self.checkout_head(checkout)
        merge_head = self._resolve_in_optional(checkout, "MERGE_HEAD")
        if checkout_head == candidate_sha and merge_head == default_head_sha:
            return self._integration_conflict_evidence(checkout)
        candidate_tree = self._resolve(f"{candidate_sha}^{{tree}}")
        if checkout_head != candidate_sha and not (
            self.commit_parents(checkout_head) == [run_head_sha]
            and self._resolve(f"{checkout_head}^{{tree}}") == candidate_tree
        ):
            raise GitError("Run Repair checkout is not at its managed squash tree")
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect Run Repair checkout")
        if status.stdout.strip() or merge_head is not None:
            raise GitError("Run Repair checkout changed before conflict conversion")
        reset = self._run_in(checkout, "reset", "--hard", candidate_sha)
        if reset.returncode != 0:
            raise GitError(reset.stderr.strip() or "could not restore the squash Candidate")
        merged = self._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if self._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
            raise GitError("Git did not preserve the converted integration conflict")
        if merged.returncode == 0:
            self._run_in(checkout, "merge", "--abort")
            raise GitError("Squash Candidate no longer conflicts with the latest default")
        return self._integration_conflict_evidence(checkout, merge_result=merged)

    def _require_staged_integration_resolution(self, checkout: Path) -> None:
        status = self._run_in(
            checkout, "status", "--porcelain=v1", "--untracked-files=all"
        )
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect staged conflict resolution"
            )
        if any(
            len(line) < 2 or line[:2] == "??" or line[1] != " "
            for line in status.stdout.splitlines()
        ):
            raise GitError(
                "Interrupted Integration-repair resolution contains unstaged changes"
            )

    def reprepare_integration_repair_checkout(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        superseded_candidate_sha: str | None,
        superseded_default_head_sha: str,
        superseded_publication_sha: str | None = None,
        squash_candidate_sha: str | None = None,
        finding_snapshot_sha: str | None = None,
        discard_invocation_changes: bool = False,
    ) -> str | None:
        """Replace a stale repair scene with the exact new parent boundary."""

        checkout_head = self.checkout_head(checkout)
        merge_head = self._resolve_in_optional(checkout, "MERGE_HEAD")
        if checkout_head == run_head_sha and merge_head == default_head_sha:
            if finding_snapshot_sha is not None:
                if superseded_candidate_sha is None or self.commit_parents(
                    finding_snapshot_sha
                ) != [superseded_candidate_sha]:
                    raise GitError(
                        "Finding repair snapshot has a stale Candidate parent"
                    )
                self._replay_finding_delta(
                    checkout,
                    run_head_sha=run_head_sha,
                    default_head_sha=default_head_sha,
                    candidate_sha=superseded_candidate_sha,
                    snapshot_sha=finding_snapshot_sha,
                )
            unmerged = self._run_in(
                checkout, "diff", "--name-only", "--diff-filter=U"
            )
            if unmerged.returncode != 0:
                raise GitError("could not inspect the reprepared integration merge")
            return (
                self._integration_conflict_evidence(checkout)
                if unmerged.stdout.strip()
                else None
            )
        allowed_heads = {run_head_sha}
        reset_head = run_head_sha
        if squash_candidate_sha is not None:
            if self.commit_parents(squash_candidate_sha) != [run_head_sha]:
                raise GitError("Superseded squash Candidate has a stale Run parent")
            allowed_heads.add(squash_candidate_sha)
            reset_head = squash_candidate_sha
        if superseded_candidate_sha is not None:
            if self.commit_parents(superseded_candidate_sha) != [
                run_head_sha,
                superseded_default_head_sha,
            ]:
                raise GitError("Superseded Merge-resolution Candidate has stale parents")
            allowed_heads.add(superseded_candidate_sha)
        if finding_snapshot_sha is not None:
            if superseded_candidate_sha is None or self.commit_parents(
                finding_snapshot_sha
            ) != [superseded_candidate_sha]:
                raise GitError("Finding repair snapshot has a stale Candidate parent")
        if superseded_publication_sha is not None:
            if superseded_candidate_sha is None:
                raise GitError("Superseded publication requires its Candidate")
            if (
                self.commit_parents(superseded_publication_sha)
                != [run_head_sha, superseded_default_head_sha]
                or self._resolve(f"{superseded_publication_sha}^{{tree}}")
                != self._resolve(f"{superseded_candidate_sha}^{{tree}}")
            ):
                raise GitError(
                    "Superseded Merge-resolution publication changed its boundary"
                )
            allowed_heads.add(superseded_publication_sha)
        if checkout_head not in allowed_heads:
            raise GitError("Integration-repair Worktree has a foreign superseded head")
        if merge_head is not None and merge_head != superseded_default_head_sha:
            raise GitError("Integration-repair Worktree has a foreign superseded merge")
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Integration-repair Worktree"
            )
        if (
            merge_head is None
            and status.stdout.strip()
            and not discard_invocation_changes
        ):
            raise GitError("Integration-repair Worktree contains uncommitted repair changes")
        if merge_head is not None:
            aborted = self._run_in(checkout, "merge", "--abort")
            if aborted.returncode != 0:
                raise GitError(
                    aborted.stderr.strip() or "could not discard stale integration conflict"
                )
        reset = self._run_in(checkout, "reset", "--hard", reset_head)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not restore the exact Run parent"
            )
        if discard_invocation_changes:
            cleaned = self._run_in(checkout, "clean", "-fd")
            if cleaned.returncode != 0:
                raise GitError(
                    cleaned.stderr.strip()
                    or "could not discard stale managed repair changes"
                )
            clean_status = self._run_in(checkout, "status", "--porcelain=v1")
            if clean_status.returncode != 0 or clean_status.stdout.strip():
                raise GitError("could not restore a clean Integration-repair Worktree")
        merged = self._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if self._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
            raise GitError("Git did not preserve the reprepared integration merge")
        if finding_snapshot_sha is not None:
            if superseded_candidate_sha is None:
                raise GitError("Finding repair snapshot requires its Candidate")
            self._replay_finding_delta(
                checkout,
                run_head_sha=run_head_sha,
                default_head_sha=default_head_sha,
                candidate_sha=superseded_candidate_sha,
                snapshot_sha=finding_snapshot_sha,
            )
        remaining_conflicts = self.integration_conflict_paths(checkout)
        if merged.returncode == 0 and not remaining_conflicts:
            return None
        return self._integration_conflict_evidence(checkout, merge_result=merged)

    def _replay_finding_delta(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
        snapshot_sha: str,
    ) -> None:
        """Replay only C1-to-snapshot changes onto the exact R+D merge scene."""

        changed = self._run_in(
            checkout,
            "diff",
            "--name-only",
            "-z",
            candidate_sha,
            snapshot_sha,
            "--",
        )
        if changed.returncode != 0:
            raise GitError(changed.stderr.strip() or "could not inspect finding delta")
        paths = tuple(value for value in changed.stdout.split("\0") if value)
        if not paths:
            return
        scene_preview = self._run(
            "merge-tree", "--write-tree", run_head_sha, default_head_sha
        )
        if scene_preview.returncode == 1:
            scene_tree = self._resolve_in_optional(checkout, "AUTO_MERGE")
            if scene_tree is None:
                raise GitError("conflicting integration scene has no AUTO_MERGE tree")
        elif scene_preview.returncode == 0:
            scene_tree = scene_preview.stdout.splitlines()[0].strip()
        else:
            raise GitError(
                scene_preview.stderr.strip() or "could not inspect integration scene"
            )
        scene = self._run_in(
            checkout,
            "commit-tree",
            scene_tree,
            "-p",
            run_head_sha,
            "-p",
            default_head_sha,
            "-m",
            "chore(run-repair): integration replay scene",
        )
        if scene.returncode != 0:
            raise GitError(scene.stderr.strip() or "could not save integration scene")
        replayed = self._run_in(
            checkout,
            "merge-tree",
            "--write-tree",
            "--merge-base",
            candidate_sha,
            "-z",
            scene.stdout.strip(),
            snapshot_sha,
        )
        if replayed.returncode not in {0, 1}:
            raise GitError(
                replayed.stderr.strip() or "could not replay finding repair delta"
            )
        fields = replayed.stdout.split("\0")
        result_tree = fields[0].strip() if fields else ""
        if not result_tree:
            raise GitError("finding repair replay did not produce a tree")
        index_entries: list[str] = []
        for field in fields[1:]:
            if not field:
                break
            index_entries.append(field)
        overlay = self._run_in(
            checkout, "diff", "--binary", scene_tree, result_tree, "--", *paths
        )
        if overlay.returncode != 0:
            raise GitError(overlay.stderr.strip() or "could not read replayed finding tree")
        if not self._worktree_matches_tree_paths(checkout, result_tree, paths):
            applied = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=checkout,
                input=overlay.stdout,
                text=True,
                capture_output=True,
                check=False,
            )
            if applied.returncode != 0:
                raise GitError(
                    applied.stderr.strip() or "could not install replayed finding tree"
                )
        if index_entries:
            raw_conflict_paths: set[str] = set()
            for entry in index_entries:
                fields = entry.split("\t", 1)
                if len(fields) != 2 or not fields[1]:
                    raise GitError("finding conflict has an invalid index entry")
                raw_conflict_paths.add(fields[1])
            conflict_paths = tuple(sorted(raw_conflict_paths))
            removed = self._run_in(
                checkout, "update-index", "--force-remove", "--", *conflict_paths
            )
            if removed.returncode != 0:
                raise GitError(
                    removed.stderr.strip() or "could not replace finding conflicts"
                )
            indexed = subprocess.run(
                ["git", "update-index", "-z", "--index-info"],
                cwd=checkout,
                input="\0".join(index_entries) + "\0",
                text=True,
                capture_output=True,
                check=False,
            )
            if indexed.returncode != 0:
                raise GitError(
                    indexed.stderr.strip() or "could not install finding conflicts"
                )

    def _worktree_matches_tree_paths(
        self, checkout: Path, tree_sha: str, paths: tuple[str, ...]
    ) -> bool:
        for path in paths:
            expected = self._run_in(checkout, "ls-tree", tree_sha, "--", path)
            if expected.returncode != 0:
                raise GitError("could not inspect replayed finding path")
            candidate = checkout / path
            if not expected.stdout.strip():
                if candidate.exists() or candidate.is_symlink():
                    return False
                continue
            fields = expected.stdout.split(None, 3)
            if len(fields) != 4:
                raise GitError("replayed finding path has an invalid tree entry")
            expected_mode, _kind, expected_blob, _name = fields
            if not candidate.exists() and not candidate.is_symlink():
                return False
            current_mode = (
                "120000"
                if candidate.is_symlink()
                else "100755"
                if candidate.stat().st_mode & 0o111
                else "100644"
            )
            hashed = self._run_in(
                checkout, "hash-object", f"--path={path}", "--", path
            )
            if (
                hashed.returncode != 0
                or current_mode != expected_mode
                or hashed.stdout.strip() != expected_blob
            ):
                return False
        return True

    def _integration_conflict_evidence(
        self, checkout: Path, *, merge_result: subprocess.CompletedProcess[str] | None = None
    ) -> str:
        unmerged = self._run_in(
            checkout, "diff", "--name-only", "--diff-filter=U"
        )
        if unmerged.returncode != 0 or not unmerged.stdout.strip():
            raise GitError("Integration-repair Worktree has no unresolved paths")
        details: list[str] = []
        if merge_result is not None:
            details.extend((merge_result.stdout.strip(), merge_result.stderr.strip()))
        details.append(f"Unresolved paths:\n{unmerged.stdout.strip()}")
        return "\n".join(detail for detail in details if detail)


    def expected_merge_tree(
        self, *, default_head_sha: str, run_head_sha: str
    ) -> str:
        """Return the tree Git would create by normally merging a Run."""
        preview = self._run(
            "merge-tree", "--write-tree", default_head_sha, run_head_sha
        )
        if preview.returncode != 0:
            raise GitError(
                preview.stderr.strip()
                or "Run Branch cannot be merged into the current default branch"
            )
        tree = preview.stdout.splitlines()[0].strip()
        if not tree:
            raise GitError("Run merge preview did not produce a tree")
        return tree

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

    def commit_run_repair_candidate(
        self, checkout: Path, *, attempt: int
    ) -> str | None:
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Run Repair checkout"
            )
        expected_message = f"chore(run-repair): candidate {attempt}"
        if not status.stdout.strip():
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            if (
                subject.returncode == 0
                and subject.stdout.strip() == expected_message
            ):
                return self._resolve_in(checkout, "HEAD")
            return None
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not stage Run Repair candidate"
            )
        committed = self._run_in(
            checkout, "commit", "-m", expected_message
        )
        if committed.returncode != 0:
            raise GitError(
                committed.stderr.strip() or "could not commit Run Repair candidate"
            )
        return self._resolve_in(checkout, "HEAD")

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
        checkout_head = self.checkout_head(checkout)
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Integration-repair Worktree"
            )
        merge_head = self._resolve_in_optional(checkout, "MERGE_HEAD")
        if not status.stdout.strip() and merge_head is None:
            if (
                self.commit_parents(checkout_head) == [run_head_sha, default_head_sha]
                and self.commit_subject(checkout_head) == expected_message
            ):
                return checkout_head
            return None
        if merge_head is not None:
            allowed_head = checkout_head == run_head_sha or (
                isinstance(squash_candidate_sha, str)
                and checkout_head == squash_candidate_sha
                and self.commit_parents(squash_candidate_sha) == [run_head_sha]
            )
            if not allowed_head:
                raise GitError("Merge-resolution Candidate has a stale Run parent")
            if merge_head != default_head_sha:
                raise GitError("Merge-resolution Candidate has a stale default parent")
            self._require_repaired_unmerged_paths(
                checkout, expected_conflict_paths=expected_conflict_paths
            )
        elif self.commit_parents(checkout_head) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution follow-up has a stale parent boundary")
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not stage resolved integration tree"
            )
        tree = self._run_in(checkout, "write-tree")
        if tree.returncode != 0:
            raise GitError(
                tree.stderr.strip() or "integration conflict is not fully resolved"
            )
        created = self._run_in(
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
        reset = self._run_in(checkout, "reset", "--hard", candidate)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not install Merge-resolution Candidate"
            )
        if self.commit_parents(candidate) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution Candidate parents changed unexpectedly")
        return candidate

    def _require_repaired_unmerged_paths(
        self, checkout: Path, *, expected_conflict_paths: tuple[str, ...]
    ) -> None:
        """Reject paths whose worktree content is still Git's raw conflict file."""

        unmerged = self._run_in(
            checkout, "diff", "--name-only", "-z", "--diff-filter=U"
        )
        if unmerged.returncode != 0:
            raise GitError("could not inspect unresolved integration paths")
        paths = set(expected_conflict_paths)
        paths.update(value for value in unmerged.stdout.split("\0") if value)
        unchanged: list[str] = []
        for path in sorted(paths):
            compared = self._run_in(
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

    def integration_conflict_paths(self, checkout: Path) -> tuple[str, ...]:
        unmerged = self._run_in(
            checkout, "diff", "--name-only", "-z", "--diff-filter=U"
        )
        if unmerged.returncode != 0:
            raise GitError("could not inspect integration conflict paths")
        return tuple(value for value in unmerged.stdout.split("\0") if value)

    def create_integration_repair_snapshot(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        attempt: int,
    ) -> str | None:
        """Publisher-owned one-parent snapshot of a managed finding repair tree."""

        if self._resolve_in_optional(checkout, "MERGE_HEAD") is not None:
            raise GitError("Finding repair snapshot cannot contain an active merge")
        if self.checkout_head(checkout) != candidate_sha:
            raise GitError("Finding repair snapshot has a foreign Candidate head")
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect finding repair")
        if not status.stdout.strip():
            return None
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(added.stderr.strip() or "could not stage finding repair")
        tree = self._run_in(checkout, "write-tree")
        if tree.returncode != 0:
            raise GitError(tree.stderr.strip() or "could not save finding repair tree")
        created = self._run_in(
            checkout,
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            candidate_sha,
            "-m",
            f"chore(run-repair): preserved finding repair {attempt}",
        )
        if created.returncode != 0:
            raise GitError(
                created.stderr.strip() or "could not create finding repair snapshot"
            )
        snapshot = created.stdout.strip()
        if self.commit_parents(snapshot) != [candidate_sha]:
            raise GitError("Finding repair snapshot has a stale Candidate parent")
        return snapshot

    def create_publication_commit(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        base_sha: str,
        message: str,
    ) -> str:
        tree = self._resolve_in(checkout, f"{candidate_sha}^{{tree}}")
        current = self._resolve_in(checkout, "HEAD")
        if (
            self._resolve_in(checkout, "HEAD^{tree}") == tree
            and self.commit_parents(current) == [base_sha]
        ):
            current_message = self._run_in(
                checkout, "show", "-s", "--format=%B", current
            )
            if (
                current_message.returncode == 0
                and current_message.stdout.strip() == message.strip()
            ):
                return current
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

        if self.commit_parents(candidate_sha) != [run_head_sha, default_head_sha]:
            raise GitError("Merge-resolution Candidate parents do not match its boundary")
        tree = self._resolve_in(checkout, f"{candidate_sha}^{{tree}}")
        created = self._run_in(
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
        reset = self._run_in(checkout, "reset", "--hard", publication)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip()
                or "could not install Merge-resolution publication commit"
            )
        if (
            self._resolve_in(checkout, f"{publication}^{{tree}}") != tree
            or self.commit_parents(publication) != [run_head_sha, default_head_sha]
        ):
            raise GitError("Merge-resolution publication changed the accepted boundary")
        return publication

    def reset_checkout_to_base(self, checkout: Path, base_branch: str) -> None:
        """Discard an invalidated Candidate from its managed checkout."""
        base_sha = self.resolve(base_branch)
        reset = self._run_in(checkout, "reset", "--hard", base_sha)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip()
                or "could not reset managed checkout to the current base"
            )

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

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        return self._run(
            "merge-base", "--is-ancestor", ancestor_sha, descendant_sha
        ).returncode == 0

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

    def prune_worktrees(self) -> None:
        result = self._run("worktree", "prune")
        if result.returncode != 0:
            raise GitError(
                result.stderr.strip() or "could not prune Git worktree registry"
            )

    def _fetch_default_branch(self, default_branch: str) -> None:
        result = run_read_command(
            ["git", "fetch", "--no-tags", "origin", default_branch],
            cwd=self.root,
        )
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

    def _resolve_in_optional(self, directory: Path, reference: str) -> str | None:
        result = self._run_in(directory, "rev-parse", "--verify", reference)
        return result.stdout.strip() if result.returncode == 0 else None

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
