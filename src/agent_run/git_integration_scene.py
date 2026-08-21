from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from agent_run.git_errors import GitError


class GitSceneOperations(Protocol):
    def checkout_head(self, checkout: Path) -> str: ...

    def commit_parents(self, sha: str) -> list[str]: ...

    def _resolve(self, reference: str) -> str | None: ...

    def _resolve_in_optional(self, directory: Path, reference: str) -> str | None: ...

    def _replay_finding_delta(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
        snapshot_sha: str,
    ) -> None: ...

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]: ...

    def _run_in(
        self, directory: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]: ...


class IntegrationRepairSceneGit:
    def __init__(self, git: GitSceneOperations) -> None:
        self._git = git

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

        checkout_head = self._git.checkout_head(checkout)
        if checkout_head != run_head_sha:
            if self._git.commit_parents(checkout_head) == [run_head_sha, default_head_sha]:
                return ""
            if not (
                isinstance(squash_candidate_sha, str)
                and checkout_head == squash_candidate_sha
                and self._git.commit_parents(checkout_head) == [run_head_sha]
            ):
                raise GitError("Integration-repair Worktree is not at an allowed parent")
        merge_head = self._git._resolve_in_optional(checkout, "MERGE_HEAD")
        if merge_head is not None:
            if merge_head != default_head_sha:
                raise GitError(
                    "Integration-repair Worktree targets a different default head"
                )
            unmerged = self._git._run_in(
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
        status = self._git._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Integration-repair Worktree"
            )
        if status.stdout.strip():
            raise GitError("Integration-repair Worktree contains unrelated changes")
        merged = self._git._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if merged.returncode == 0:
            self._git._run_in(checkout, "merge", "--abort")
            raise GitError("Integration-repair Worktree no longer reproduces a conflict")
        if self._git._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
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

        if self._git.commit_parents(candidate_sha) != [run_head_sha]:
            raise GitError("Squash Candidate does not have the exact Run parent")
        checkout_head = self._git.checkout_head(checkout)
        merge_head = self._git._resolve_in_optional(checkout, "MERGE_HEAD")
        if checkout_head == candidate_sha and merge_head == default_head_sha:
            return self._integration_conflict_evidence(checkout)
        candidate_tree = self._git._resolve(f"{candidate_sha}^{{tree}}")
        if checkout_head != candidate_sha and not (
            self._git.commit_parents(checkout_head) == [run_head_sha]
            and self._git._resolve(f"{checkout_head}^{{tree}}") == candidate_tree
        ):
            raise GitError("Run Repair checkout is not at its managed squash tree")
        status = self._git._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect Run Repair checkout")
        if status.stdout.strip() or merge_head is not None:
            raise GitError("Run Repair checkout changed before conflict conversion")
        reset = self._git._run_in(checkout, "reset", "--hard", candidate_sha)
        if reset.returncode != 0:
            raise GitError(reset.stderr.strip() or "could not restore the squash Candidate")
        merged = self._git._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if self._git._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
            raise GitError("Git did not preserve the converted integration conflict")
        if merged.returncode == 0:
            self._git._run_in(checkout, "merge", "--abort")
            raise GitError("Squash Candidate no longer conflicts with the latest default")
        return self._integration_conflict_evidence(checkout, merge_result=merged)

    def _require_staged_integration_resolution(self, checkout: Path) -> None:
        status = self._git._run_in(
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

        checkout_head = self._git.checkout_head(checkout)
        merge_head = self._git._resolve_in_optional(checkout, "MERGE_HEAD")
        if checkout_head == run_head_sha and merge_head == default_head_sha:
            if finding_snapshot_sha is not None:
                if superseded_candidate_sha is None or self._git.commit_parents(
                    finding_snapshot_sha
                ) != [superseded_candidate_sha]:
                    raise GitError(
                        "Finding repair snapshot has a stale Candidate parent"
                    )
                self._git._replay_finding_delta(
                    checkout,
                    run_head_sha=run_head_sha,
                    default_head_sha=default_head_sha,
                    candidate_sha=superseded_candidate_sha,
                    snapshot_sha=finding_snapshot_sha,
                )
            unmerged = self._git._run_in(
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
            if self._git.commit_parents(squash_candidate_sha) != [run_head_sha]:
                raise GitError("Superseded squash Candidate has a stale Run parent")
            allowed_heads.add(squash_candidate_sha)
            reset_head = squash_candidate_sha
        if superseded_candidate_sha is not None:
            if self._git.commit_parents(superseded_candidate_sha) != [
                run_head_sha,
                superseded_default_head_sha,
            ]:
                raise GitError("Superseded Merge-resolution Candidate has stale parents")
            allowed_heads.add(superseded_candidate_sha)
        if finding_snapshot_sha is not None:
            if superseded_candidate_sha is None or self._git.commit_parents(
                finding_snapshot_sha
            ) != [superseded_candidate_sha]:
                raise GitError("Finding repair snapshot has a stale Candidate parent")
        if superseded_publication_sha is not None:
            if superseded_candidate_sha is None:
                raise GitError("Superseded publication requires its Candidate")
            if (
                self._git.commit_parents(superseded_publication_sha)
                != [run_head_sha, superseded_default_head_sha]
                or self._git._resolve(f"{superseded_publication_sha}^{{tree}}")
                != self._git._resolve(f"{superseded_candidate_sha}^{{tree}}")
            ):
                raise GitError(
                    "Superseded Merge-resolution publication changed its boundary"
                )
            allowed_heads.add(superseded_publication_sha)
        if checkout_head not in allowed_heads:
            raise GitError("Integration-repair Worktree has a foreign superseded head")
        if merge_head is not None and merge_head != superseded_default_head_sha:
            raise GitError("Integration-repair Worktree has a foreign superseded merge")
        status = self._git._run_in(checkout, "status", "--porcelain=v1")
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
            aborted = self._git._run_in(checkout, "merge", "--abort")
            if aborted.returncode != 0:
                raise GitError(
                    aborted.stderr.strip() or "could not discard stale integration conflict"
                )
        reset = self._git._run_in(checkout, "reset", "--hard", reset_head)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not restore the exact Run parent"
            )
        if discard_invocation_changes:
            cleaned = self._git._run_in(checkout, "clean", "-fd")
            if cleaned.returncode != 0:
                raise GitError(
                    cleaned.stderr.strip()
                    or "could not discard stale managed repair changes"
                )
            clean_status = self._git._run_in(checkout, "status", "--porcelain=v1")
            if clean_status.returncode != 0 or clean_status.stdout.strip():
                raise GitError("could not restore a clean Integration-repair Worktree")
        merged = self._git._run_in(
            checkout, "merge", "--no-commit", "--no-ff", default_head_sha
        )
        if self._git._resolve_in_optional(checkout, "MERGE_HEAD") != default_head_sha:
            raise GitError("Git did not preserve the reprepared integration merge")
        if finding_snapshot_sha is not None:
            if superseded_candidate_sha is None:
                raise GitError("Finding repair snapshot requires its Candidate")
            self._git._replay_finding_delta(
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

        changed = self._git._run_in(
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
        scene_preview = self._git._run(
            "merge-tree", "--write-tree", run_head_sha, default_head_sha
        )
        if scene_preview.returncode == 1:
            scene_tree = self._git._resolve_in_optional(checkout, "AUTO_MERGE")
            if scene_tree is None:
                raise GitError("conflicting integration scene has no AUTO_MERGE tree")
        elif scene_preview.returncode == 0:
            scene_tree = scene_preview.stdout.splitlines()[0].strip()
        else:
            raise GitError(
                scene_preview.stderr.strip() or "could not inspect integration scene"
            )
        scene = self._git._run_in(
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
        replayed = self._git._run_in(
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
        overlay = self._git._run_in(
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
            removed = self._git._run_in(
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
            expected = self._git._run_in(checkout, "ls-tree", tree_sha, "--", path)
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
            hashed = self._git._run_in(
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
        unmerged = self._git._run_in(
            checkout, "diff", "--name-only", "--diff-filter=U"
        )
        if unmerged.returncode != 0 or not unmerged.stdout.strip():
            raise GitError("Integration-repair Worktree has no unresolved paths")
        details: list[str] = []
        if merge_result is not None:
            details.extend((merge_result.stdout.strip(), merge_result.stderr.strip()))
        details.append(f"Unresolved paths:\n{unmerged.stdout.strip()}")
        return "\n".join(detail for detail in details if detail)


    def integration_conflict_paths(self, checkout: Path) -> tuple[str, ...]:
        unmerged = self._git._run_in(
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

        if self._git._resolve_in_optional(checkout, "MERGE_HEAD") is not None:
            raise GitError("Finding repair snapshot cannot contain an active merge")
        if self._git.checkout_head(checkout) != candidate_sha:
            raise GitError("Finding repair snapshot has a foreign Candidate head")
        status = self._git._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect finding repair")
        if not status.stdout.strip():
            return None
        added = self._git._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(added.stderr.strip() or "could not stage finding repair")
        tree = self._git._run_in(checkout, "write-tree")
        if tree.returncode != 0:
            raise GitError(tree.stderr.strip() or "could not save finding repair tree")
        created = self._git._run_in(
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
        if self._git.commit_parents(snapshot) != [candidate_sha]:
            raise GitError("Finding repair snapshot has a stale Candidate parent")
        return snapshot
