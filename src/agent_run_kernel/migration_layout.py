"""One original checkout's reversible HEAD layout, outside native execution.

The migration caller durably saves this exact intent and holds its Task locks
and original source-ref verification lock. This module owns only the bounded
HEAD transaction. It neither admits work nor grants attached-checkout access.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
from time import monotonic
from typing import Any, Literal

from .effects import LocalGit
from .migration import MigrationRefused
from .model import KernelError


LayoutState = Literal["attached", "detached"]
_KEYS = frozenset({"version", "nonce", "source_sha256", "source_uuid", "repository_root", "checkout",
                   "owned_root", "common_git_dir", "source_ref", "expected_oid", "before_head",
                   "checkout_archive_sha256"})


def _identity(layout: Mapping[str, Any]) -> tuple[LocalGit, Path, Path, Path, str, str]:
    if set(layout) != _KEYS or type(layout["version"]) is not int or layout["version"] != 1:
        raise MigrationRefused("source_layout_identity")
    for key, length in (("nonce", 32), ("source_uuid", 32), ("source_sha256", 64)):
        value = layout[key]
        if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
            raise MigrationRefused("source_layout_identity")
    archive = layout["checkout_archive_sha256"]
    if archive is not None and (not isinstance(archive, str) or re.fullmatch("[0-9a-f]{64}", archive) is None):
        raise MigrationRefused("source_layout_identity")
    values = [layout[key] for key in ("repository_root", "checkout", "owned_root", "common_git_dir")]
    if any(not isinstance(value, str) or not value or "\0" in value for value in values):
        raise MigrationRefused("source_layout_identity")
    root, checkout, owned, common = (Path(value) for value in values)
    if (any(not path.is_absolute() or path.resolve() != path for path in (root, checkout, owned, common))
            or common != root / ".git" or not checkout.is_relative_to(owned)
            or checkout in {root, owned}):
        raise MigrationRefused("source_layout_identity")
    ref, oid = layout["source_ref"], layout["expected_oid"]
    if (not isinstance(ref, str) or not ref.startswith("refs/heads/") or "\0" in ref
            or not isinstance(oid, str) or re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", oid) is None
            or layout["before_head"] != "ref: " + ref + "\n"):
        raise MigrationRefused("source_layout_identity")
    git = LocalGit(root)
    if git.run("check-ref-format", ref, check=False).returncode:
        raise MigrationRefused("source_layout_identity")
    descriptor = os.open(common / "agent-run-checkout-id", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as marker:
        physical = os.fstat(marker.fileno())
        if (not stat.S_ISREG(physical.st_mode) or physical.st_nlink != 1 or physical.st_size != 33
                or marker.read(34) != layout["source_uuid"].encode("ascii") + b"\n"):
            raise MigrationRefused("source_layout_identity")
    return git, checkout, owned, common, ref, oid


def verify_layout(layout: Mapping[str, Any], *, state: Literal["attached", "detached", "either"] = "either") -> LayoutState:
    """Read the recorded physical identity; reject unsupported Git before cutover."""
    try:
        git, checkout, _owned, common, ref, oid = _identity(layout)
        if (git.run("rev-parse", "--show-toplevel").stdout.strip() != str(git.root)
                or git.run("rev-parse", "--show-toplevel", cwd=checkout).stdout.strip() != str(checkout)
                or git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout).stdout.strip() != str(common)
                or git.run("symbolic-ref", "-q", ref, check=False).returncode != 1
                or git.run("rev-parse", "--verify", ref).stdout.strip() != oid
                or git.run("rev-parse", "--verify", "HEAD", cwd=checkout).stdout.strip() != oid):
            raise MigrationRefused("source_layout_identity")
        symbolic = git.run("symbolic-ref", "-q", "HEAD", cwd=checkout, check=False)
        if symbolic.returncode == 0 and symbolic.stdout == ref + "\n":
            actual: LayoutState = "attached"
        elif symbolic.returncode == 1:
            actual = "detached"
        else:
            raise MigrationRefused("source_layout_head_changed")
        head = _head_path(git, checkout, common)
        descriptor = os.open(head, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            physical = os.fstat(stream.fileno())
            expected_head = layout["before_head"].encode() if actual == "attached" else (oid + "\n").encode()
            if (not stat.S_ISREG(physical.st_mode) or physical.st_nlink != 1
                    or physical.st_size != len(expected_head) or stream.read(1024) != expected_head):
                raise MigrationRefused("source_layout_head_changed")
        if state not in {"attached", "detached", "either"} or state != "either" and actual != state:
            raise MigrationRefused("source_layout_head_changed")
        listed = git.run("worktree", "list", "--porcelain", "-z").stdout
        own = [block.split("\0") for block in listed.split("\0\0")
               if block.split("\0")[0] == f"worktree {checkout}"]
        expected = {f"worktree {checkout}", f"HEAD {oid}", "detached" if actual == "detached" else "branch " + ref}
        if len(own) != 1 or set(own[0]) - {""} != expected:
            raise MigrationRefused("source_layout_identity")
        # Parse and abort without preparing a write. Older Git must refuse
        # before the source is retired; ordinary native Git support is unchanged.
        capability = git.run("update-ref", "--no-deref", "--stdin", "-z", cwd=checkout, check=False,
            input_text=f"start\0symref-update HEAD\0{ref}\0oid\0{oid}\0abort\0")
        if capability.returncode:
            raise MigrationRefused("source_layout_git_capability")
        return actual
    except (OSError, KernelError, subprocess.SubprocessError) as error:
        raise MigrationRefused("source_layout_identity") from error


def _head_path(git: LocalGit, checkout: Path, common: Path) -> Path:
    head = Path(git.run("rev-parse", "--path-format=absolute", "--git-path", "HEAD", cwd=checkout).stdout.strip())
    if (not head.is_absolute() or head.resolve() != head or head.name != "HEAD"
            or not head.is_relative_to(common / "worktrees")):
        raise MigrationRefused("source_layout_identity")
    return head


def _change(layout: Mapping[str, Any], target: LayoutState) -> None:
    before = verify_layout(layout)
    if before == target:
        return
    git, checkout, _owned, common, ref, oid = _identity(layout)
    update = f"update HEAD\0{oid}\0{oid}\0" if target == "detached" else f"symref-update HEAD\0{ref}\0oid\0{oid}\0"
    process = subprocess.Popen(["git", "update-ref", "--no-deref", "--stdin", "-z"],
        cwd=checkout, env=git.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True)
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(("start\0" + update + "prepare\0").encode())
        process.stdin.flush()
        deadline, output = monotonic() + 5.0, bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"prepare: ok\n" not in output:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise MigrationRefused("source_layout_head_lock")
                block = os.read(process.stdout.fileno(), 4096)
                if not block or len(output) + len(block) > 65536:
                    raise MigrationRefused("source_layout_git_capability")
                output.extend(block)
        # The OID comparison alone also accepts a foreign same-OID symref.
        # HEAD is now locked: verify its exact original target before commit.
        verify_layout(layout, state=before)
        process.stdin.write(b"commit\0")
        process.stdin.flush()
        process.stdin.close()
        if process.wait(timeout=5) != 0:
            raise MigrationRefused("source_layout_commit_unknown")
        head = _head_path(git, checkout, common)
        descriptor = os.open(head, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(head.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        verify_layout(layout, state=target)
    except (OSError, KernelError, subprocess.SubprocessError) as error:
        raise MigrationRefused("source_layout_commit_unknown") from error
    finally:
        try:
            process.stdin.close()  # A prepared, uncommitted update aborts here.
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Popen still owns this unreaped leader. Never signal a saved
            # process/group ID after wait has released its physical identity.
            process.kill()
            process.wait(timeout=5)
        finally:
            process.stdout.close()


def detach_layout(layout: Mapping[str, Any]) -> None:
    """Keep the original OID and every branch/index/work byte, changing HEAD only."""
    _change(layout, "detached")


def restore_layout(layout: Mapping[str, Any]) -> None:
    """Restore only this original attachment before the caller releases old authority."""
    _change(layout, "attached")
