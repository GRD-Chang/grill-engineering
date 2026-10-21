"""Reject existing v6 Task authority at native admission, without running v6.

The original Task lock precedes the native Task lock and both outlive the
native transaction. This read barrier does not make an unchanged old CLI obey
native ownership: actual migration must durably retire the original database.
Runner installation separately excludes switching a live installed runtime.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import base64
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Mapping

from agent_run.managed_workspace import ManagedWorkspace, ManagedWorkspaceError, normalize_repository
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey

from .migration import SOURCE_PROTOCOL, SOURCE_TABLES, source_identity
from .model import KernelError, wire_json
from .task_location import locked_task


_SOURCE_FENCE_PREFIX = "original-source-to-native/1:"
_SOURCE_FENCE_KEYS = frozenset({"nonce", "source_db", "source_sha256", "run_id", "task_fingerprint",
                                "source_uuid", "target_db"})
_SOURCE_FENCE_LIMIT = 16 * 1024


@dataclass(frozen=True, order=True)
class _Source:
    state: Path
    run_id: str = ""
    routed: bool = False


def _refuse() -> KernelError:
    return KernelError("legacy_requires_offline_migration",
                       "The original Task authority must be retired by an explicit offline migration")


def _fence_material(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _SOURCE_FENCE_KEYS:
        raise _refuse()
    receipt: dict[str, str] = {}
    for key in sorted(_SOURCE_FENCE_KEYS):
        item = value[key]
        if not isinstance(item, str) or not item or any(ord(character) < 32 for character in item):
            raise _refuse()
        receipt[key] = item
    for key, length in (("nonce", 32), ("source_uuid", 32), ("source_sha256", 64), ("task_fingerprint", 64)):
        if len(receipt[key]) != length or any(character not in "0123456789abcdef" for character in receipt[key]):
            raise _refuse()
    for key in ("source_db", "target_db"):
        path = Path(receipt[key])
        if (not path.is_absolute() or str(path) != receipt[key] or ".." in path.parts
                or receipt[key].startswith("//") or len(receipt[key]) > 4096):
            raise _refuse()
    if receipt["source_db"] == receipt["target_db"] or len(receipt["run_id"]) > 256:
        raise _refuse()
    return receipt


def source_fence(receipt: Mapping[str, str]) -> str:
    """Encode the shared original-source receipt; encoding grants no admission."""
    protocol = _SOURCE_FENCE_PREFIX + base64.urlsafe_b64encode(wire_json(_fence_material(dict(receipt)))).decode("ascii")
    if len(protocol) > _SOURCE_FENCE_LIMIT:
        raise _refuse()
    return protocol


def read_source_fence(protocol: str) -> dict[str, str]:
    """Read only this exact canonical protocol, never a private-copy marker."""
    if not protocol.startswith(_SOURCE_FENCE_PREFIX) or len(protocol) > _SOURCE_FENCE_LIMIT:
        raise _refuse()
    try:
        encoded = protocol[len(_SOURCE_FENCE_PREFIX):].encode("ascii")
        receipt = _fence_material(json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True)))
    except (ValueError, UnicodeError) as error:
        raise _refuse() from error
    if source_fence(receipt) != protocol:
        raise _refuse()
    return receipt


def _sources(task: TaskKey) -> tuple[_Source, ...]:
    sources: set[_Source] = set()
    try:
        managed = ManagedWorkspace.for_repository(task.repository)
    except ManagedWorkspaceError:
        pass  # Non-managed library Tasks can still have an exact legacy locator.
    else:
        if managed.repository_root.resolve() == task.workspace and managed.state_root.exists():
            sources.add(_Source(managed.state_root))
    locator = app_data_root() / "run-locator.json"
    if not locator.exists() and not locator.is_symlink():
        return tuple(sorted(sources))
    if locator.is_symlink() or locator.stat().st_size > 1024 * 1024:
        raise _refuse()
    descriptor = os.open(locator.parent / ".run-locator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        value = json.loads(locator.read_bytes())
    if not isinstance(value, dict) or set(value) != {"entries"} or not isinstance(value["entries"], list):
        raise _refuse()
    if len(value["entries"]) > 32:
        raise _refuse()
    for entry in value["entries"]:
        if not isinstance(entry, dict):
            raise _refuse()
        repository = entry.get("repository")
        parent = entry.get("parent_number")
        canonical_repository = None
        if isinstance(repository, str):
            try:
                canonical_repository = normalize_repository(repository)
            except ManagedWorkspaceError:
                pass
        routed = canonical_repository is not None and type(parent) is int and parent > 0
        if routed and (canonical_repository != task.repository or parent != task.parent_number):
            # Only a complete, valid route proves another Parent is unrelated.
            continue
        root_value = entry.get("repository_root")
        if not isinstance(root_value, str) or not Path(root_value).is_absolute():
            raise _refuse()
        if Path(root_value).resolve() != task.workspace:
            continue
        if not routed and ("repository" in entry or "parent_number" in entry):
            raise _refuse()
        state, run_id = entry.get("state_dir"), entry.get("run_id")
        if not isinstance(state, str) or not Path(state).is_absolute() or not isinstance(run_id, str) or not run_id:
            raise _refuse()
        sources.add(_Source(Path(state), run_id, routed))
    return tuple(sorted(sources))


@contextmanager
def legacy_task_lock(task: TaskKey, state_root: Path) -> Iterator[None]:
    """Use the unchanged v6 TaskControl lock; callers then take native Task/storage locks.

    This coordination lock proves no Host exit and changes no v6 database byte.
    The mapper must separately establish actual exit and a source SQLite fence.
    """
    if not state_root.is_dir() or state_root.resolve() != state_root:
        raise _refuse()
    directory = state_root / "task-control"
    if directory.is_symlink():
        raise _refuse()
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory / ("." + task.fingerprint + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _check(task: TaskKey, source: _Source) -> None:
    sentinel = source.state / "task-control" / (task.fingerprint + ".json")
    if sentinel.exists() or sentinel.is_symlink():
        raise _refuse()
    path = source.state / "state.sqlite3"
    if path.is_symlink():
        raise _refuse()
    if not path.exists():
        if source.run_id:
            raise _refuse()
        return
    before = source_identity(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0, isolation_level=None)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        # A retired *copy* is not authority to admit against the original.
        if db.execute("SELECT key,value FROM schema_meta").fetchmany(2) != [("protocol", SOURCE_PROTOCOL)]:
            raise _refuse()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchmany(513)}
        if not SOURCE_TABLES <= tables or "control_tasks" not in tables:
            raise _refuse()
        control = db.execute("SELECT task_id,workspace,repository,parent_number FROM control_tasks "
                             "WHERE task_id=? OR (workspace=? AND repository=? COLLATE NOCASE AND parent_number=?) LIMIT 1",
                             (task.fingerprint, str(task.workspace), task.repository, task.parent_number)).fetchone()
        if control is not None:
            # Any original record needs explicit migration. Do not reinterpret
            # old exited/failed/unknown/control-target facts as new authority.
            raise _refuse()
        run = db.execute("SELECT run_id FROM runs WHERE repository=? COLLATE NOCASE AND parent_number=? LIMIT 1",
                         (task.repository, task.parent_number)).fetchone()
        if run is not None or source.routed:
            raise _refuse()
        if source.run_id:
            original = db.execute("SELECT repository,parent_number FROM runs WHERE run_id=?", (source.run_id,)).fetchmany(2)
            if len(original) != 1:
                raise _refuse()
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _refuse()
    finally:
        db.close()


@contextmanager
def locked_admission(task: TaskKey) -> Iterator[Path]:
    """Check existing legacy authority while holding both original admission locks."""
    with ExitStack() as held:
        try:
            sources = _sources(task)
            for state in sorted({source.state for source in sources}):
                held.enter_context(legacy_task_lock(task, state))
            location = held.enter_context(locked_task(task))
            if _sources(task) != sources:
                raise _refuse()
            for source in sources:
                _check(task, source)
        except (OSError, ValueError, sqlite3.Error) as error:
            raise _refuse() from error
        yield location
