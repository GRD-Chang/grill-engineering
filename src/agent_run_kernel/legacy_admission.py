"""Preserve old Task occupancy; no transfer, compatibility or fence release."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from agent_run.managed_workspace import ManagedWorkspace, ManagedWorkspaceError, normalize_repository
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey

from .model import KernelError
from .state_preflight import (
    LEGACY_PROTOCOL, SOURCE_FENCE_PREFIX, UnsupportedState, read_state, require_supported_state,
)
from .task_location import locked_task, read_task, task_path


@dataclass(frozen=True, order=True)
class _Source:
    state: Path
    run_id: str = ""
    routed: bool = False


def _refuse(path: Path) -> UnsupportedState:
    return UnsupportedState(path, "uncertain_task")


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
        raise _refuse(locator)
    descriptor = os.open(locator.parent / ".run-locator.lock", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        value = json.loads(locator.read_bytes())
    if not isinstance(value, dict) or set(value) != {"entries"} or not isinstance(value["entries"], list):
        raise _refuse(locator)
    if len(value["entries"]) > 32:
        raise _refuse(locator)
    for entry in value["entries"]:
        if not isinstance(entry, dict):
            raise _refuse(locator)
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
            raise _refuse(locator)
        if Path(root_value).resolve() != task.workspace:
            continue
        if not routed and ("repository" in entry or "parent_number" in entry):
            raise _refuse(locator)
        state, run_id = entry.get("state_dir"), entry.get("run_id")
        if not isinstance(state, str) or not Path(state).is_absolute() or not isinstance(run_id, str) or not run_id:
            raise _refuse(locator)
        sources.add(_Source(Path(state), run_id, routed))
    return tuple(sorted(sources))


@contextmanager
def legacy_task_lock(task: TaskKey, state_root: Path) -> Iterator[None]:
    """Use the unchanged v6 TaskControl lock; callers then take native Task/storage locks.

    This coordination lock proves no Host exit and changes no v6 database byte.
    This lock never authorizes interpretation or transfer of old work.
    """
    if not state_root.is_dir() or state_root.resolve() != state_root:
        raise _refuse(state_root)
    directory = state_root / "task-control"
    if directory.is_symlink():
        raise _refuse(state_root)
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory / ("." + task.fingerprint + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _check(task: TaskKey, source: _Source) -> None:
    sentinel = source.state / "task-control" / (task.fingerprint + ".json")
    if sentinel.exists() or sentinel.is_symlink():
        raise _refuse(sentinel)
    path = source.state / "state.sqlite3"
    if not path.exists() and not path.is_symlink():
        if source.run_id or any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
                                for suffix in ("-wal", "-shm", "-journal")):
            raise _refuse(path)
        return
    with read_state(path) as db:
        # Only protocol and Task identities are inspected. No Attempt, proof,
        # receipt, old owner, source fence payload or process exit is decoded.
        objects = db.execute("SELECT name,type FROM sqlite_schema WHERE name IN ('schema_meta','control_tasks','runs')").fetchall()
        if set(objects) != {('schema_meta', 'table'), ('control_tasks', 'table'), ('runs', 'table')}:
            raise _refuse(path)
        metadata = db.execute("SELECT key,substr(value,1,256) FROM schema_meta LIMIT 2").fetchall()
        if len(metadata) != 1 or metadata[0][0] != "protocol" or not isinstance(metadata[0][1], str):
            raise _refuse(path)
        protocol = metadata[0][1]
        if protocol == LEGACY_PROTOCOL:
            reason = "legacy_v6"
        elif protocol.startswith(SOURCE_FENCE_PREFIX):
            reason, protocol = "fenced_source", SOURCE_FENCE_PREFIX + "<fenced>"
        else:
            raise UnsupportedState(path, "unknown_protocol", protocol)
        control = db.execute("SELECT 1 FROM control_tasks WHERE task_id=? OR "
            "(workspace=? AND repository=? COLLATE NOCASE AND parent_number=?) LIMIT 1",
            (task.fingerprint, str(task.workspace), task.repository, task.parent_number)).fetchone()
        run = db.execute("SELECT 1 FROM runs WHERE repository=? COLLATE NOCASE AND parent_number=? LIMIT 1",
                         (task.repository, task.parent_number)).fetchone()
        if control is not None or run is not None or source.routed:
            raise UnsupportedState(path, reason, protocol)
        if source.run_id:
            rows = db.execute("SELECT repository,parent_number FROM runs WHERE run_id=? LIMIT 2", (source.run_id,)).fetchall()
            if len(rows) != 1:
                raise _refuse(path)
            repository, parent = rows[0]
            if not isinstance(repository, str) or type(parent) is not int or parent <= 0:
                raise _refuse(path)
            if normalize_repository(repository) == task.repository and parent == task.parent_number:
                raise UnsupportedState(path, reason, protocol)


def _check_current(task: TaskKey) -> None:
    current = read_task(task_path(task))
    if current is not None:
        require_supported_state(current[1])


@contextmanager
def locked_admission(task: TaskKey) -> Iterator[Path]:
    """Refuse occupied/uncertain old Tasks before creating native locks or state."""
    with ExitStack() as held:
        try:
            sources = _sources(task)
            for source in sources:
                _check(task, source)
            _check_current(task)
            for state in sorted({source.state for source in sources}):
                held.enter_context(legacy_task_lock(task, state))
            location = held.enter_context(locked_task(task))
            if _sources(task) != sources:
                raise _refuse(location)
            for source in sources:
                _check(task, source)
            _check_current(task)
        except UnsupportedState:
            raise
        except (KernelError, OSError, ValueError, sqlite3.Error) as error:
            raise _refuse(task_path(task)) from error
        yield location
