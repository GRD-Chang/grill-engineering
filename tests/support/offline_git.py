"""The Scenario's private bare repository is its only external ref authority.

No SQL, effect intent, grant or recovery journal belongs here. Callers authorize
each external write before invoking push/delete/merge. Reads and object fetches
never advance the Scenario's local branch refs.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import re
import selectors
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from agent_run.errors import GitError
from agent_run.runtime.git_command import MAX_GIT_ERROR_BYTES, _kill_group, run_git


class OfflineGit:
    def __init__(self, root: Path, *, environment: Mapping[str, str],
                 written: Callable[[str, Mapping[str, Any], subprocess.CompletedProcess[str]], None] | None = None) -> None:
        self.written = written
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
        self.environment.update(HOME=str(self.root), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
        self._owned()

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
        self._owned_repository(self.repository, bare=False)
        if self.remote.exists() or self.remote.is_symlink():
            self._owned_repository(self.remote, bare=True)

    def _owned_repository(self, repository: Path, *, bare: bool) -> None:
        directory = repository if bare else repository / ".git"
        if (repository.is_symlink() or directory.is_symlink() or not directory.is_dir()
                or not directory.resolve().is_relative_to(self.root)
                or os.path.lexists(directory / "commondir")
                or os.path.lexists(directory / "config.worktree")):
            raise ValueError("offline Git requires a private ordinary repository")
        for name in ("HEAD", "config", "packed-refs", "refs", "objects", "logs"):
            path = directory / name
            if path.is_symlink():
                raise ValueError("offline Git metadata must not redirect paths")
            for entry in path.rglob("*"):
                if entry.is_symlink():
                    raise ValueError("offline Git metadata must not redirect paths")
        # Parse only this file, without following include/includeIf. Unknown
        # configuration (including URL rewrites and alternate transports) fails
        # closed; do not sanitize or mutate a saved world's configuration.
        allowed = {"core.repositoryformatversion": {"0", "1"}, "core.filemode": {"true", "false"},
            "core.bare": {str(bare).lower()}, "core.logallrefupdates": {"true", "false"},
            "core.ignorecase": {"true", "false"}, "core.precomposeunicode": {"true", "false"},
            "extensions.objectformat": {"sha1", "sha256"}, "gc.auto": {"0"}, "maintenance.auto": {"false"},
            "user.name": None, "user.email": None}
        config = self._run(self.root, "config", "--no-includes", "--file", str(directory / "config"), "--null", "--list")
        for item in config.stdout.split("\0"):
            if not item:
                continue
            key, _, value = item.partition("\n")
            permitted = allowed.get(key)
            if key not in allowed or permitted is not None and value not in permitted:
                raise ValueError("unsupported offline Git configuration: " + key)
        actual = self._run(repository, "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-path", "refs").stdout.splitlines()
        if actual != [str(directory), str(directory / "refs")]:
            raise ValueError("offline Git common directory or refs redirected")
        self._owned_objects(directory / "objects")

    def _owned_objects(self, objects: Path) -> None:
        if not objects.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("offline objects escape their Scenario")
        for path in objects.rglob("*"):
            if path.is_symlink():
                raise ValueError("offline object paths must not redirect")
        alternates = objects / "info/alternates"
        if alternates.exists():
            raise ValueError("offline objects must be self-contained")

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
            self._run(self.root, "init", "--bare", "--template=", "--object-format=" + object_format, str(staged))
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
        result = self._run(self.repository, "ls-remote", "--refs", "--", str(self.remote))
        return {ref: self._oid(sha) for sha, ref in (line.split("\t") for line in result.stdout.splitlines())}

    def commit(self, sha: str) -> dict[str, Any]:
        self._owned()
        sha = self._oid(sha)
        row = self._run(self.remote, "show", "--no-patch", "--format=%H %T %P", sha + "^{commit}").stdout.split()
        raw = self._run(self.remote, "cat-file", "commit", sha).stdout
        return {"sha": row[0], "tree": {"sha": row[1]}, "parents": [{"sha": parent} for parent in row[2:]],
                "message": raw.partition("\n\n")[2]}

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
        self._run(self.repository, "fetch", "--no-tags", "--no-write-fetch-head", "--",
                  str(self.remote), self._oid(sha))
