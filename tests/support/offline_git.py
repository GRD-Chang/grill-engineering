"""The Scenario's private bare repository is its only external ref authority.

No SQL, effect intent, grant or recovery journal belongs here. Callers authorize
each external write before invoking push/delete/merge. Reads and object fetches
never advance the Scenario's local branch refs.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import copy
import os
from pathlib import Path
import re
import selectors
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from agent_run.adapters.git_objects import OBJECT_BATCH_FORMAT, object_rows
from agent_run.errors import GitError, KernelError
from agent_run.model import Commit
from agent_run.runtime.git_command import MAX_GIT_ERROR_BYTES, _kill_group, run_git


# Exact files created by this fixture; existing repositories are not imported.
_CANONICAL_CONFIG = {
    False: (b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
            b"\tbare = false\n\tlogallrefupdates = true\n"
            b"[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"),
    True: (b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n"
           b"[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"),
}
_PRIVATE_GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}


class OfflineGit:
    def __init__(self, root: Path, *, environment: Mapping[str, str],
                 written: Callable[[str, Mapping[str, Any], subprocess.CompletedProcess[str]], None] | None = None) -> None:
        self.written = written
        if root.is_symlink():
            raise ValueError("offline world root must not redirect paths")
        self.root = root.resolve(strict=True)
        self.repository = self.root / "authority"
        self.remote = self.root / "remote.git"
        # Only Scenario-owned paths are accepted, never a configured remote URL.
        for path in (self.repository, self.remote):
            if path.is_symlink() or path.resolve().parent != self.root:
                raise ValueError("offline repository escapes its Scenario")
        self.environment = {key: value for key, value in environment.items()
                            if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}
                            or key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        self.environment.update(HOME=str(self.root), **_PRIVATE_GIT_ENV)

    def _with_written(self, written: Callable[[str, Mapping[str, Any], subprocess.CompletedProcess[str]], None]) -> OfflineGit:
        # Configuration only: every physical operation still calls _owned().
        # Keep the read backend and other requests' callbacks/environment apart.
        backend = copy(self)
        backend.environment = dict(self.environment)
        backend.written = written
        return backend

    def _command(self, directory: Path, *args: str) -> list[str]:
        return ["git", "-c", "core.hooksPath=" + os.devnull,
                "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(directory), *args]

    def _run(self, directory: Path, *args: str, input: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = run_git(self._command(directory, *args), cwd=self.root, input=input,
                timeout=30, environment=self.environment)
        except GitError as error:
            raise OSError(str(error)) from error
        if check and result.returncode:
            raise OSError(result.stderr or result.stdout or "offline Git operation failed")
        return result

    def _owned(self) -> None:
        if (any(self.environment.get(key) != value for key, value in _PRIVATE_GIT_ENV.items())
                or any(key.startswith("GIT_") and key not in _PRIVATE_GIT_ENV
                       and not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
                       for key in self.environment)):
            raise ValueError("offline Git environment must not redirect repositories or configuration")
        self._owned_repository(self.repository, bare=False)
        if self.remote.exists() or self.remote.is_symlink():
            self._owned_repository(self.remote, bare=True)

    def _owned_repository(self, repository: Path, *, bare: bool) -> None:
        """Check the fixed fixture boundary, not arbitrary hostile Git metadata."""
        directory = repository if bare else repository / ".git"
        if (self.root.is_symlink() or repository.is_symlink() or directory.is_symlink()
                or not directory.resolve().is_relative_to(self.root) or not directory.is_dir()
                or os.path.lexists(directory / "commondir")
                or os.path.lexists(directory / "config.worktree")):
            raise ValueError("offline Git requires a private ordinary repository")
        for name in ("HEAD", "config", "packed-refs", "refs", "objects", "objects/info", "logs"):
            if (directory / name).is_symlink():
                raise ValueError("offline Git metadata must not redirect paths")
        if (not (directory / "refs").is_dir() or not (directory / "objects").is_dir()
                or os.path.lexists(directory / "objects/info/alternates")):
            raise ValueError("offline objects must be self-contained")
        for name, expected in (("config", _CANONICAL_CONFIG[bare]),
                               ("HEAD", b"ref: refs/heads/main\n")):
            path = directory / name
            if not path.is_file():
                raise ValueError("offline Git requires regular fixture metadata")
            with path.open("rb") as stream:
                body = stream.read(len(expected) + 1)
            if body != expected:
                raise ValueError("unsupported offline Git configuration or layout")

    @staticmethod
    def _oid(value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise ValueError("expected a complete Git object ID")
        return value

    def _ref(self, value: str) -> str:
        if not value.startswith("refs/heads/"):
            raise ValueError("expected an exact branch ref")
        self._run(self.repository, "check-ref-format", value)
        return value

    def initialize(self, refs: Mapping[str, str]) -> None:
        """Create a new bare world once; never import or overwrite a saved one."""
        self._owned()
        refs = {self._ref(ref): self._oid(sha) for ref, sha in refs.items()}
        for sha in refs.values():
            self._run(self.repository, "cat-file", "-e", sha + "^{commit}")
        if self.remote.exists():
            raise FileExistsError("offline remote already exists")
        object_format = self._run(self.repository, "rev-parse", "--show-object-format").stdout.strip()
        with TemporaryDirectory(prefix=".remote-git-create-", dir=self.root) as temporary:
            staged = Path(temporary) / "remote.git"
            self._run(self.root, "init", "--bare", "--initial-branch=main", "--template=", "--object-format=" + object_format, str(staged))
            # Local push transport clears the client's -c configuration before
            # starting receive-pack. The receiver must be quiescent itself.
            self._run(staged, "config", "--local", "maintenance.auto", "false")
            self._run(staged, "config", "--local", "gc.auto", "0")
            self._owned_repository(staged, bare=True)
            # No shared alternates and no refs copied from the local workspace.
            for sha in sorted(set(refs.values())):
                self._run(staged, "fetch", "--no-tags", "--no-write-fetch-head", "--", str(self.repository), sha)
            commands = "".join(f"create {ref} {sha}\n" for ref, sha in refs.items())
            self._run(staged, "update-ref", "--stdin", input="start\n" + commands + "prepare\ncommit\n")
            if self.remote.exists():
                raise OSError("offline remote appeared during initialization")
            staged.rename(self.remote)

    def refs(self) -> dict[str, str]:
        self._owned()
        result = self._run(self.remote, "for-each-ref", "--format=%(objectname)%09%(refname)")
        return {ref: self._oid(sha) for sha, ref in (line.split("\t") for line in result.stdout.splitlines())}

    def commit(self, sha: str) -> dict[str, Any]:
        self._owned()
        sha = self._oid(sha)
        raw = self._run(self.remote, "cat-file", "commit", sha).stdout
        header, _, message = raw.partition("\n\n")
        fields = [line.split(" ", 1) for line in header.splitlines()]
        return {"sha": sha, "tree": {"sha": next(value for key, value in fields if key == "tree")},
                "parents": [{"sha": value} for key, value in fields if key == "parent"], "message": message}

    def read_commits(self, heads: tuple[str, ...], *, transfer_missing: bool = False) -> dict[str, Commit]:
        """Read exact heads and trees, transporting only absent head objects."""
        self._owned()
        heads = tuple(dict.fromkeys(self._oid(head) for head in heads))
        if not heads:
            return {}
        queries = tuple(query for head in heads for query in (head, head + "^{tree}"))
        rows = self._object_rows(queries)
        missing = []
        for index, head in enumerate(heads):
            commit, tree = rows[2 * index:2 * index + 2]
            if commit == [head, "missing"]:
                missing.append(head)
            elif commit != [head, "commit", head]:
                raise KernelError("git_operation_failed", "Invalid offline commit observation")
            elif not self._tree_row(tree, head):
                raise KernelError("git_operation_failed", "Invalid offline commit observation")
        if missing:
            if not transfer_missing:
                raise KernelError("git_operation_failed", "Missing offline commit observation")
            self._owned()
            self._run(self.repository, "fetch", "--no-tags", "--no-write-fetch-head", "--",
                      str(self.remote), *missing)
            # Revalidate the physical response after transport, including trees.
            return self.read_commits(heads)
        return {head: Commit(head, rows[2 * index + 1][0]) for index, head in enumerate(heads)}

    @staticmethod
    def _tree_row(row: list[str], head: str) -> bool:
        return (len(row) == 3 and row[1:] == ["tree", head + "^{tree}"]
                and len(row[0]) == len(head) and re.fullmatch(r"[0-9a-f]+", row[0]) is not None)

    def _object_rows(self, queries: tuple[str, ...]) -> list[list[str]]:
        result = self._run(self.repository, "cat-file", OBJECT_BATCH_FORMAT,
            input="".join(query + " " + query + "\n" for query in queries), check=False)
        return object_rows(result, queries)

    def require_commits(self, shas: tuple[str, ...]) -> None:
        """Retain cat-file -e <oid>^{commit} semantics for historical anchors."""
        self._owned()
        shas = tuple(dict.fromkeys(self._oid(sha) for sha in shas))
        if not shas:
            return
        queries = tuple(sha + "^{commit}" for sha in shas)
        try:
            rows = self._object_rows(queries)
            for sha, query, row in zip(shas, queries, rows):
                if (len(row) != 3 or row[1:] != ["commit", query]
                        or len(row[0]) != len(sha) or re.fullmatch(r"[0-9a-f]+", row[0]) is None):
                    raise KernelError("git_ancestry_unknown")
        except KernelError as error:
            raise KernelError("git_ancestry_unknown") from error
        except OSError as error:
            # Anchor inspection previously used LocalGit, whose native child
            # failures are KernelError rather than retryable transport outages.
            if isinstance(error.__cause__, GitError):
                raise KernelError("git_operation_failed", str(error.__cause__)) from error.__cause__
            raise

    def compare(self, base: str, head: str) -> dict[str, Any]:
        self._owned()
        base, head = self._oid(base), self._oid(head)
        common = self._run(self.remote, "merge-base", base, head).stdout.strip()
        return {"status": "identical" if base == head else "ahead" if common == base else "diverged",
                "base_commit": {"sha": base}, "merge_base_commit": {"sha": common}}

    def push(self, ref: str, before: str | None, after: str) -> subprocess.CompletedProcess[str]:
        self._owned()
        ref, after = self._ref(ref), self._oid(after)
        self._direct(ref)
        expected = "" if before is None else self._oid(before)
        result = self._run(self.repository, "push", "--force-with-lease=" + ref + ":" + expected,
                           "--", str(self.remote), after + ":" + ref, check=False)
        if self.written is not None:
            self.written('push', {'ref': ref, 'before': before, 'after': after}, result)
        return result

    def delete(self, ref: str, expected: str) -> subprocess.CompletedProcess[str]:
        self._owned()
        ref, expected = self._ref(ref), self._oid(expected)
        self._direct(ref)
        result = self._run(self.repository, "push", "--force-with-lease=" + ref + ":" + expected,
                           "--", str(self.remote), ":" + ref, check=False)
        if self.written is not None:
            self.written('delete', {'ref': ref, 'before': expected}, result)
        return result

    def merge(self, base_ref: str, head_ref: str, *, sha: str, merge_method: str,
              commit_title: str, commit_message: str) -> str:
        self._owned()
        base_ref, head_ref = self._ref(base_ref), self._ref(head_ref)
        refs = self.refs()
        base, head = refs[base_ref], refs[head_ref]
        self._direct(base_ref)
        self._direct(head_ref)
        if self._oid(sha) != head:
            raise OSError("pull request head changed")
        if base_ref == head_ref or base == head:
            raise OSError("nothing to merge")
        if merge_method not in {"merge", "squash"}:
            raise ValueError("unsupported offline merge method")
        tree = self._run(self.remote, "merge-tree", "--write-tree", base, head).stdout.splitlines()[0]
        parents = (base, head) if merge_method == "merge" else (base,)
        args = ["commit-tree", self._oid(tree)]
        for parent in parents:
            args.extend(("-p", parent))
        message = commit_title + ("\n\n" + commit_message if commit_message else "")
        merged = self._run(self.remote, *args, input=message if message.endswith("\n") else message + "\n").stdout.strip()
        self._publish_merge(base_ref, base, head_ref, head, merged)
        return self._oid(merged)

    def _direct(self, ref: str, expected: str | None = None) -> None:
        symbolic = self._run(self.remote, "symbolic-ref", "-q", ref, check=False)
        if symbolic.returncode != 1:
            raise OSError("offline write requires the original direct ref")
        if expected is not None and self._run(self.remote, "rev-parse", "--verify", ref).stdout.strip() != expected:
            raise OSError("offline ref changed under its lock")

    def _publish_merge(self, base_ref: str, base: str, head_ref: str, head: str, merged: str) -> None:
        # Like cleanup's prepared verification: hold both original refs while
        # rechecking symref identity. --no-deref prevents a referent write even
        # if the selected ref was changed to a same-OID alias before prepare.
        self._owned()
        process = subprocess.Popen(self._command(self.remote, "update-ref", "--no-deref", "--stdin", "-z"),
            cwd=self.root, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write((f"start\0verify {head_ref}\0{head}\0"
                + f"update {base_ref}\0{merged}\0{base}\0prepare\0").encode())
            process.stdin.flush()
            self._ack(process, b"prepare: ok\n")
            self._owned()
            self._direct(base_ref, base)
            self._direct(head_ref, head)
            process.stdin.write(b"commit\0")
            process.stdin.flush()
            self._ack(process, b"commit: ok\n")
        finally:
            try:
                process.stdin.close()  # EOF aborts a prepared but uncommitted transaction.
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid)
                process.wait()
            finally:
                _kill_group(process.pid)
                process.stdout.close()

    @staticmethod
    def _ack(process: subprocess.Popen[bytes], expected: bytes) -> None:
        assert process.stdout is not None
        deadline, output = monotonic() + 5, bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while expected not in output:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise OSError("offline ref transaction timed out")
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk or len(output) + len(chunk) > MAX_GIT_ERROR_BYTES:
                    raise OSError("offline ref transaction failed")
                output.extend(chunk)

    def fetch(self, sha: str) -> None:
        """Copy an observed physical object for local inspection, without refs."""
        self._owned()
        sha = self._oid(sha)
        # Git objects are immutable by full OID. Recheck possession, not a cached
        # ref or authority; only objects missing locally need transport.
        if self._run(self.repository, "cat-file", "-e", sha + "^{commit}", check=False).returncode:
            self._run(self.repository, "fetch", "--no-tags", "--no-write-fetch-head", "--",
                      str(self.remote), sha)
