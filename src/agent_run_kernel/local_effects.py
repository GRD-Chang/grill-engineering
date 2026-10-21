"""Managed local Git effects; policy and durable intent attachment belong to Kernel."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from agent_run.git_errors import GitError
from agent_run.git_publication_commit import (
    install_publication_commit, observe_publication_commit, prepare_publication_commit,
)

from .cleanup_adapter import cleanup_local_ref, cleanup_worktree, inspect_local_ref, inspect_worktree
from .effects import LocalGit
from .model import Effect, EffectResult, KernelError


class LocalEffects:
    OPERATIONS = frozenset({"workspace", "validation_checkout", "validation_cleanup", "candidate",
                            "integrate", "publication_commit", "cleanup_worktree", "cleanup_local_ref"})

    def __init__(self, git: LocalGit, *, owned_root: Path) -> None:
        self.git = LocalGit(git.root)
        self.git.env = git.env.copy()
        self.owned_root = owned_root

    def _checkout(self, effect: Effect) -> tuple[Path, str | None]:
        values = [effect.payload.get(key) for key in ("checkout", "owned_root", "common_git_dir")]
        if any(not isinstance(value, str) or not value or "\0" in value for value in values):
            raise KernelError("foreign_local_checkout")
        checkout, owned, common = (Path(str(value)) for value in values)
        if (owned != self.owned_root or not checkout.is_relative_to(owned) or checkout == owned
                or any(not path.is_absolute() or path.resolve() != path for path in (checkout, owned, common))):
            raise KernelError("foreign_local_checkout")
        actual_common = self.git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        if Path(actual_common).resolve() != common:
            raise KernelError("foreign_local_checkout")
        listed = self.git.run("worktree", "list", "--porcelain", "-z").stdout
        registered = [block.split("\0") for block in listed.split("\0\0")
                      if block.split("\0")[0] == f"worktree {checkout}"]
        if not checkout.exists():
            if registered:
                raise KernelError("local_checkout_unavailable")
            return checkout, None
        if (not checkout.is_dir() or len(registered) != 1 or "detached" not in registered[0]
                or any(field.startswith(("locked", "prunable")) for field in registered[0])):
            raise KernelError("foreign_local_checkout")
        own_common = self.git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout).stdout.strip()
        top = self.git.run("rev-parse", "--show-toplevel", cwd=checkout).stdout.strip()
        head = self.git.run("rev-parse", "--verify", "HEAD", cwd=checkout).stdout.strip()
        symbolic = self.git.run("symbolic-ref", "-q", "HEAD", cwd=checkout, check=False)
        if (Path(own_common).resolve() != common or Path(top).resolve() != checkout
                or f"HEAD {head}" not in registered[0] or symbolic.returncode != 1):
            raise KernelError("foreign_local_checkout")
        return checkout, head

    def prepare_publication(self, effect: Effect) -> dict[str, Any]:
        """Compute a read-only object witness, or validate the original saved one."""
        if effect.operation != "publication_commit" or effect.payload.get("reuse_sha"):
            raise KernelError("publication_prepare_not_required")
        checkout, head = self._checkout(effect)
        if head is None:
            raise KernelError("local_checkout_unavailable")
        intent = effect.payload.get("publication_commit_intent")
        if intent is not None and (not isinstance(intent, dict) or not intent):
            raise KernelError("invalid_publication_intent")
        return prepare_publication_commit(
            self.git, checkout, tree=effect.payload["integrated_tree"],
            parents=effect.payload["parents"], message=effect.payload["commit_message"], intent=intent,
        )

    def _cleanup(self, effect: Effect) -> Effect:
        if (effect.payload.get("owned_root") != str(self.owned_root)
                or not self.owned_root.is_absolute() or self.owned_root.resolve() != self.owned_root):
            raise KernelError("foreign_local_checkout")
        if effect.operation == "cleanup_local_ref":
            common = effect.payload.get("common_git_dir")
            if (not isinstance(common, str) or not common or "\0" in common
                    or not Path(common).is_absolute() or Path(common).resolve() != Path(common)):
                raise KernelError("foreign_local_checkout")
            actual = self.git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            if Path(actual).resolve() != Path(common):
                raise KernelError("foreign_local_checkout")
        if effect.operation != "validation_cleanup":
            return effect
        return replace(effect, payload={**effect.payload,
                       "branch_ref": effect.source_ref, "expected_head": effect.payload["reviewed_commit"]})

    def _merge_tree(self, effect: Effect, before: str, target: str) -> str | None:
        # merge-tree writes objects even when used only to calculate a tree.
        # Isolate those writes; the original exact object store is an alternate.
        with tempfile.TemporaryDirectory(prefix="agent-run-merge-") as directory:
            scratch = LocalGit(self.git.root)
            scratch.env = {**self.git.env, "GIT_OBJECT_DIRECTORY": directory,
                           "GIT_ALTERNATE_OBJECT_DIRECTORIES": json.dumps(
                               str(Path(effect.payload["common_git_dir"]) / "objects"), ensure_ascii=False)}
            result = scratch.run("merge-tree", "--write-tree", before, target, check=False)
            if result.returncode not in {0, 1}:
                raise KernelError("integration_tree_unknown")
            return result.stdout.splitlines()[0] if result.returncode == 0 else None

    def _integration(self, effect: Effect, checkout: Path, head: str) -> EffectResult:
        if effect.candidate is None:
            raise KernelError("missing_candidate")
        stages = [(suffix, target) for suffix, target in
                  (("source", effect.payload["source_sha"]), ("target", effect.base.sha)) if target]
        proven: dict[str, str] = {}
        current = head
        for suffix, target in reversed(stages):
            message = self.git.run("show", "-s", "--format=%B", current).stdout.rstrip("\n")
            if message == f"integration {effect.effect_id}-{suffix}":
                parents = self.git.parents(current)
                if len(parents) != 2 or parents[1] != target:
                    return EffectResult("third")
                proven[suffix], current = current, parents[0]
        if current != effect.candidate.sha:
            return EffectResult("third")
        merge = self.git.run("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=checkout, check=False)
        if merge.returncode not in {0, 1}:
            raise KernelError("integration_merge_unknown")
        for index, (suffix, target) in enumerate(stages):
            if self.git.is_ancestor(target, current):
                if suffix in proven:
                    return EffectResult("third")
                continue
            if suffix not in proven:
                if any(later in proven for later, _target in stages[index + 1:]):
                    return EffectResult("third")
                if merge.returncode == 0:
                    if head != current or merge.stdout.strip() != target:
                        return EffectResult("third")
                    diff = self.git.run("diff", "--name-only", "--diff-filter=U", cwd=checkout)
                    return EffectResult("failure", commit=self.git.commit(current), raw={
                        "head": current, "merge_head": target, "command": ["git", "diff", "--name-only", "--diff-filter=U"],
                        "returncode": diff.returncode, "stdout": diff.stdout, "stderr": diff.stderr})
                if self.git.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout:
                    return EffectResult("third")
                return EffectResult("prior", commit=self.git.commit(current))
            commit = self.git.commit(proven[suffix])
            if (self.git.parents(commit.sha) != (current, target)
                    or commit.tree != self._merge_tree(effect, current, target)):
                return EffectResult("third")
            current = commit.sha
        if (current != head or merge.returncode == 0
                or self.git.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout):
            return EffectResult("third")
        commit = self.git.commit(current)
        return EffectResult("target", commit, commit.tree, raw={"parents": self.git.parents(current)})

    def _candidate(self, effect: Effect, checkout: Path, head: str) -> EffectResult:
        parents = tuple(effect.payload["parents"])
        if not parents or parents[0] != effect.before_sha or len(parents) not in {1, 2} or len(set(parents)) != len(parents):
            return EffectResult("third")
        merge = self.git.run("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=checkout, check=False)
        if merge.returncode not in {0, 1}:
            raise KernelError("candidate_merge_unknown")
        if head != effect.before_sha:
            message = self.git.run("show", "-s", "--format=%B", head).stdout.rstrip("\n")
            if (message != f"candidate {effect.effect_id}" or self.git.parents(head) != parents or merge.returncode == 0
                    or self.git.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout):
                return EffectResult("third")
            self.git.run("diff", "--check", effect.before_sha, head, cwd=checkout)
            return EffectResult("target", self.git.commit(head), raw={"parents": parents, "no_change": False})
        if (merge.stdout.strip() if merge.returncode == 0 else None) != (parents[1] if len(parents) == 2 else None):
            return EffectResult("third")
        diff = self.git.run("diff", "--check", cwd=checkout, check=False)
        if diff.returncode:
            return EffectResult("failure", raw={"command": ["git", "diff", "--check"], "returncode": diff.returncode,
                                                 "stdout": diff.stdout, "stderr": diff.stderr})
        if len(parents) == 1 and not self.git.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout:
            return EffectResult("target", self.git.commit(head), raw={"parents": self.git.parents(head), "no_change": True})
        return EffectResult("prior")

    def _publication(self, effect: Effect, checkout: Path, head: str) -> EffectResult:
        if self.git.run("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout:
            return EffectResult("third", raw={"reason": "dirty_local_checkout"})
        if effect.payload.get("reuse_sha"):
            commit = self.git.commit(effect.payload["reuse_sha"])
            if commit.tree != effect.payload["integrated_tree"] or head != commit.sha:
                return EffectResult("third")
            return EffectResult("target", commit=commit, raw={"parents": self.git.parents(commit.sha)})
        if not effect.payload.get("publication_commit_intent"):
            return EffectResult("prior" if head == effect.payload["integrated_sha"] else "third")
        intent = self.prepare_publication(effect)
        exists = observe_publication_commit(self.git, checkout, intent)
        if exists and head == intent["expected_sha"]:
            commit = self.git.commit(head)
            return EffectResult("target", commit=commit, raw={"parents": self.git.parents(head)})
        return EffectResult("prior" if head == effect.payload["integrated_sha"] else "third")

    def reconcile(self, effect: Effect) -> EffectResult:
        """Observe original physical facts without using response receipts as proof."""
        try:
            if effect.operation == "cleanup_local_ref":
                return inspect_local_ref(self.git, self._cleanup(effect))
            if effect.operation in {"validation_cleanup", "cleanup_worktree"}:
                return inspect_worktree(self.git, self._cleanup(effect))
            checkout, head = self._checkout(effect)
            if effect.operation in {"workspace", "validation_checkout"}:
                expected = effect.before_sha if effect.operation == "workspace" else effect.payload["reviewed_commit"]
                if effect.operation == "workspace" and effect.payload["create_ref"]:
                    refs = self.git.run("for-each-ref", "--format=%(refname) %(objectname)", effect.source_ref).stdout.splitlines()
                    if refs and refs != [f"{effect.source_ref} {expected}"]:
                        return EffectResult("third")
                    if not refs:
                        return EffectResult("prior" if head in {None, expected} else "third")
                if head is None:
                    return EffectResult("prior")
                return EffectResult("target", self.git.commit(head)) if head == expected else EffectResult("third")
            if head is None:
                return EffectResult("unknown")
            if effect.operation == "candidate":
                return self._candidate(effect, checkout, head)
            if effect.operation == "integrate":
                return self._integration(effect, checkout, head)
            if effect.operation == "publication_commit":
                return self._publication(effect, checkout, head)
            raise KernelError("unsupported_local_effect", effect.operation)
        except (GitError, KernelError, OSError, subprocess.SubprocessError) as error:
            code = error.code if isinstance(error, KernelError) else "local_read_failed"
            return EffectResult("third" if code == "foreign_local_checkout" or isinstance(error, GitError)
                                else "unknown", raw={"reason": code})

    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        observed = self.reconcile(effect)
        no_write = (
            effect.operation == "integrate" and effect.candidate is not None and observed.commit == effect.candidate
            or effect.operation == "candidate" and observed.raw.get("no_change") is True
            and observed.commit is not None and observed.commit.sha == effect.before_sha
            or effect.operation == "publication_commit" and bool(effect.payload.get("reuse_sha"))
            and observed.commit is not None and observed.commit.sha == effect.payload["reuse_sha"]
            or effect.operation in {"validation_cleanup", "cleanup_worktree"}
            and observed.raw.get("reason") == "worktree_absent"
            or effect.operation == "cleanup_local_ref" and observed.raw.get("reason") == "ref_absent"
        )
        if observed.disposition == "target" and no_write:
            # Only explicit, verified no-write outcomes may acquire first-dispatch
            # authority here; an arbitrary pre-existing target remains read-only.
            authorize()
            fresh = self.reconcile(effect)
            if fresh != observed:
                raise KernelError("local_effect_boundary_changed")
            return fresh
        if observed.disposition != "prior":
            return observed

        def guard() -> None:
            authorize()
            current = self.reconcile(effect)
            if current.disposition not in {"prior", "target"}:
                raise KernelError("local_effect_boundary_changed")

        writer = LocalGit(self.git.root, guard)
        writer.env = self.git.env.copy()
        if effect.operation == "cleanup_local_ref":
            return cleanup_local_ref(writer, effect)
        if effect.operation in {"validation_cleanup", "cleanup_worktree"}:
            return cleanup_worktree(writer, self._cleanup(effect))
        checkout, head = self._checkout(effect)
        if effect.operation in {"workspace", "validation_checkout"}:
            expected = effect.before_sha if effect.operation == "workspace" else effect.payload["reviewed_commit"]
            if effect.operation == "workspace" and effect.payload["create_ref"]:
                refs = self.git.run("for-each-ref", "--format=%(refname)", effect.source_ref).stdout
                if not refs:
                    writer.run("update-ref", effect.source_ref, expected, "0" * len(expected))
            if head is None:
                writer.run("worktree", "add", "--detach", str(checkout), expected)
        elif effect.operation == "candidate":
            writer.candidate(checkout, effect.before_sha, effect.effect_id, tuple(effect.payload["parents"]))
        elif effect.operation == "integrate":
            assert observed.commit is not None
            current = observed.commit
            for suffix, target in (("source", effect.payload["source_sha"]), ("target", effect.base.sha)):
                if not target or self.git.is_ancestor(target, current.sha):
                    continue
                try:
                    current = writer.combine(current.sha, target, effect.effect_id + "-" + suffix)
                except KernelError as error:
                    if error.code != "integration_failed":
                        raise
                    raw = writer.conflict_checkout(checkout, target)
                    return EffectResult("failure", commit=self.git.commit(str(raw["head"])), raw=raw)
                writer.install_checkout(checkout, current.sha)
        elif effect.operation == "publication_commit":
            if not effect.payload.get("publication_commit_intent"):
                raise KernelError("publication_intent_missing")
            intent = self.prepare_publication(effect)

            def missing_intent() -> None:
                raise KernelError("publication_intent_missing")

            install_publication_commit(
                writer, checkout, tree=effect.payload["integrated_tree"], parents=effect.payload["parents"],
                message=effect.payload["commit_message"], intent=intent, save_intent=missing_intent,
            )
        else:
            raise KernelError("unsupported_local_effect", effect.operation)
        return self.reconcile(effect)
