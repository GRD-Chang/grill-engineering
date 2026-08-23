from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_run.git_errors import GitError as GitError
from agent_run.git_errors import (
    GitIntegrityError as GitIntegrityError,
    MergeConflictError as MergeConflictError,
)
from agent_run.git_integration_commit import IntegrationRepairCommitGit
from agent_run.git_integration_scene import IntegrationRepairSceneGit
from agent_run.github_retry import run_read_command


MANAGED_DELIVERY_BRANCH_PREFIXES = (
    "agent-run/",
    "agent-run-repair/",
)


def is_managed_delivery_branch(branch: str) -> bool:
    return branch.startswith(MANAGED_DELIVERY_BRANCH_PREFIXES)

class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._integration_scene = IntegrationRepairSceneGit(self)
        self._integration_commit = IntegrationRepairCommitGit(self)

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

    def restore_managed_checkout(
        self, checkout: Path, *, expected_head: str
    ) -> None:
        """Restore a managed worktree after an Agent-owned Git mutation.

        The observed foreign commit remains available in the raw integrity
        evidence, but the managed branch and worktree are moved back to the
        last Controller-owned boundary before another Candidate is created.
        """

        branch = self._run_in(checkout, "symbolic-ref", "--short", "HEAD")
        branch_name = branch.stdout.strip()
        if branch.returncode != 0 or not is_managed_delivery_branch(branch_name):
            raise GitError(
                "refusing to restore a checkout that is not on a managed delivery branch"
            )
        if not self._commit_exists(expected_head):
            raise GitError("managed checkout recovery boundary does not exist")
        reset = self._run_in(checkout, "reset", "--hard", expected_head)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not restore managed checkout boundary"
            )
        cleaned = self._run_in(checkout, "clean", "-fd")
        if cleaned.returncode != 0:
            raise GitError(
                cleaned.stderr.strip() or "could not clean managed checkout boundary"
            )
        if self.checkout_head(checkout) != expected_head:
            raise GitError("managed checkout recovery did not restore its expected head")

    def seed_managed_checkout(self, checkout: Path, *, expected_head: str) -> None:
        """Move a fresh managed repair checkout to a preserved Candidate."""

        branch = self._run_in(checkout, "symbolic-ref", "--short", "HEAD")
        branch_name = branch.stdout.strip()
        if branch.returncode != 0 or not is_managed_delivery_branch(branch_name):
            raise GitError(
                "refusing to seed a checkout that is not on a managed delivery branch"
            )
        if not self._commit_exists(expected_head):
            raise GitError("preserved repair Candidate does not exist")
        reset = self._run_in(checkout, "reset", "--hard", expected_head)
        if reset.returncode != 0:
            raise GitError(
                reset.stderr.strip() or "could not seed the managed repair checkout"
            )
        cleaned = self._run_in(checkout, "clean", "-fd")
        if cleaned.returncode != 0:
            raise GitError(
                cleaned.stderr.strip() or "could not clean the managed repair checkout"
            )
        if self.checkout_head(checkout) != expected_head:
            raise GitError("managed repair checkout seed did not match its Candidate")

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
        return self._integration_scene.prepare_integration_repair_checkout(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            squash_candidate_sha=squash_candidate_sha,
            allow_clean_merge=allow_clean_merge,
            allow_staged_resolution=allow_staged_resolution,
        )

    def convert_squash_candidate_to_integration_repair(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
    ) -> str:
        return self._integration_scene.convert_squash_candidate_to_integration_repair(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            candidate_sha=candidate_sha,
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
        return self._integration_scene.reprepare_integration_repair_checkout(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            superseded_candidate_sha=superseded_candidate_sha,
            superseded_default_head_sha=superseded_default_head_sha,
            superseded_publication_sha=superseded_publication_sha,
            squash_candidate_sha=squash_candidate_sha,
            finding_snapshot_sha=finding_snapshot_sha,
            discard_invocation_changes=discard_invocation_changes,
        )

    def _replay_finding_delta(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
        snapshot_sha: str,
    ) -> None:
        self._integration_scene._replay_finding_delta(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            candidate_sha=candidate_sha,
            snapshot_sha=snapshot_sha,
        )

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
        self,
        checkout: Path,
        *,
        ticket_number: int,
        attempt: int,
        expected_head: str | None = None,
        candidate_intent: dict[str, object] | None = None,
    ) -> str | None:
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(status.stderr.strip() or "could not inspect ticket checkout")
        observed_head = self._resolve_in(checkout, "HEAD")
        if status.stdout.strip() and expected_head is not None and observed_head != expected_head:
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            raise GitIntegrityError(
                "managed checkout has an unowned commit before Candidate creation",
                evidence={
                    "kind": "git_integrity",
                    "status": "fail",
                    "reason": "checkout HEAD differs from the managed Candidate boundary",
                    "observed_head": observed_head,
                    "actual_subject": subject.stdout.strip()
                    if subject.returncode == 0
                    else "",
                    "expected_subject": f"chore(ticket-{ticket_number}): candidate {attempt}",
                    "workspace_clean": "false",
                },
            )
        expected_message = f"chore(ticket-{ticket_number}): candidate {attempt}"
        if not status.stdout.strip():
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            if self._candidate_commit_matches_intent(
                checkout,
                observed_head=observed_head,
                expected_head=expected_head,
                expected_subject=expected_message,
                candidate_intent=candidate_intent,
            ):
                return self._resolve_in(checkout, "HEAD")
            if expected_head is not None and observed_head != expected_head:
                actual_subject = subject.stdout.strip() if subject.returncode == 0 else ""
                raise GitIntegrityError(
                    "managed checkout contains an unowned clean commit",
                    evidence={
                        "kind": "git_integrity",
                        "status": "fail",
                        "reason": "clean checkout head has an unexpected commit subject",
                        "observed_head": observed_head,
                        "actual_subject": actual_subject,
                        "expected_subject": expected_message,
                        "workspace_clean": "true",
                    },
                )
            return None
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(added.stderr.strip() or "could not stage candidate")
        committed = self._run_in(
            checkout,
            "commit",
            "-m",
            expected_message,
            *self._candidate_intent_message(candidate_intent),
        )
        if committed.returncode != 0:
            raise GitError(committed.stderr.strip() or "could not commit candidate")
        return self._resolve_in(checkout, "HEAD")

    def commit_run_repair_candidate(
        self,
        checkout: Path,
        *,
        attempt: int,
        expected_head: str | None = None,
        candidate_intent: dict[str, object] | None = None,
    ) -> str | None:
        status = self._run_in(checkout, "status", "--porcelain=v1")
        if status.returncode != 0:
            raise GitError(
                status.stderr.strip() or "could not inspect Run Repair checkout"
            )
        observed_head = self._resolve_in(checkout, "HEAD")
        if status.stdout.strip() and expected_head is not None and observed_head != expected_head:
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            raise GitIntegrityError(
                "managed Run Repair checkout has an unowned commit before Candidate creation",
                evidence={
                    "kind": "git_integrity",
                    "status": "fail",
                    "reason": "checkout HEAD differs from the managed Candidate boundary",
                    "observed_head": observed_head,
                    "actual_subject": subject.stdout.strip()
                    if subject.returncode == 0
                    else "",
                    "expected_subject": f"chore(run-repair): candidate {attempt}",
                    "workspace_clean": "false",
                },
            )
        expected_message = f"chore(run-repair): candidate {attempt}"
        if not status.stdout.strip():
            subject = self._run_in(checkout, "log", "-1", "--format=%s")
            if self._candidate_commit_matches_intent(
                checkout,
                observed_head=observed_head,
                expected_head=expected_head,
                expected_subject=expected_message,
                candidate_intent=candidate_intent,
            ):
                return self._resolve_in(checkout, "HEAD")
            if expected_head is not None and observed_head != expected_head:
                actual_subject = subject.stdout.strip() if subject.returncode == 0 else ""
                raise GitIntegrityError(
                    "managed Run Repair checkout contains an unowned clean commit",
                    evidence={
                        "kind": "git_integrity",
                        "status": "fail",
                        "reason": "clean checkout head has an unexpected commit subject",
                        "observed_head": observed_head,
                        "actual_subject": actual_subject,
                        "expected_subject": expected_message,
                        "workspace_clean": "true",
                    },
                )
            return None
        added = self._run_in(checkout, "add", "--all")
        if added.returncode != 0:
            raise GitError(
                added.stderr.strip() or "could not stage Run Repair candidate"
            )
        committed = self._run_in(
            checkout,
            "commit",
            "-m",
            expected_message,
            *self._candidate_intent_message(candidate_intent),
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
        candidate_intent: dict[str, object] | None = None,
        expected_conflict_paths: tuple[str, ...] = (),
    ) -> str | None:
        return self._integration_commit.commit_merge_resolution_candidate(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            attempt=attempt,
            squash_candidate_sha=squash_candidate_sha,
            candidate_intent=candidate_intent,
            expected_conflict_paths=expected_conflict_paths,
        )

    def integration_conflict_paths(self, checkout: Path) -> tuple[str, ...]:
        return self._integration_scene.integration_conflict_paths(checkout)

    def create_integration_repair_snapshot(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        attempt: int,
    ) -> str | None:
        return self._integration_scene.create_integration_repair_snapshot(
            checkout,
            candidate_sha=candidate_sha,
            attempt=attempt,
        )

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
        return self._integration_commit.create_merge_resolution_publication_commit(
            checkout,
            candidate_sha=candidate_sha,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            message=message,
        )

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

    def diff_name_status(self, base_sha: str, head_sha: str) -> list[dict[str, str]]:
        """Return a compact, durable description of a Candidate code delta."""
        result = self._run(
            "diff", "--no-ext-diff", "--no-textconv", "--name-status", base_sha, head_sha
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not read Candidate delta")
        changed: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            status, separator, path = line.partition("\t")
            if separator and status and path:
                changed.append({"status": status, "path": path})
        return changed

    def verify_candidate_integrity(
        self, base_sha: str, candidate_sha: str
    ) -> dict[str, str]:
        """Verify only the immutable Git boundary needed by fallback publication."""
        candidate_tree = self.resolve(f"{candidate_sha}^{{tree}}")
        relation = self._run("merge-base", "--is-ancestor", base_sha, candidate_sha)
        if relation.returncode != 0:
            raise GitError("Candidate is not a descendant of the bound base")
        return {
            "status": "pass",
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "base_is_ancestor": "true",
        }

    def commit_subject(self, sha: str) -> str:
        result = self._run("log", "-1", "--format=%s", sha)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "could not read commit subject")
        return result.stdout.strip()

    @staticmethod
    def _candidate_intent_message(
        candidate_intent: dict[str, object] | None,
    ) -> tuple[str, ...]:
        token = candidate_intent.get("token") if isinstance(candidate_intent, dict) else None
        if not isinstance(token, str) or not token:
            return ()
        return ("-m", f"agent-run-candidate-intent: {token}")

    def _candidate_commit_matches_intent(
        self,
        checkout: Path,
        *,
        observed_head: str,
        expected_head: str | None,
        expected_subject: str,
        candidate_intent: dict[str, object] | None,
    ) -> bool:
        if not isinstance(expected_head, str) or observed_head == expected_head:
            return False
        if not isinstance(candidate_intent, dict):
            return False
        if candidate_intent.get("expected_head") != expected_head:
            return False
        if self.commit_subject(observed_head) != expected_subject:
            return False
        parents = self.commit_parents(observed_head)
        if parents != [expected_head]:
            return False
        token = candidate_intent.get("token")
        if not isinstance(token, str) or not token:
            return False
        message = self._run_in(checkout, "show", "-s", "--format=%B", observed_head)
        return message.returncode == 0 and (
            f"agent-run-candidate-intent: {token}" in message.stdout
        )

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
