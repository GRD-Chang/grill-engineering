"""Canonical repository metadata routing on the one Storage owner.

The sole public protocol always selects an exact Run. Metadata access is a
separate explicit role, never an alternate schema or implicit Run inference.
No migration, fallback initialization or JSON publication is provided.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3
import os
import stat
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from agent_run.contracts.repository import normalize_repository
from agent_run.contracts.task import TaskKey
from agent_run.errors import KernelError
from agent_run.model import Requirements, RunSpec
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.tasks import read_repository_task
from agent_run.storage.preflight import UnsupportedState

if TYPE_CHECKING:
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.core.kernel import Kernel
    from agent_run.core.task_creation import TaskRun
    from agent_run.queries.run import QueryDocument



def open_run(database: Path, *, run_id: str | None = None, storage: Storage | None = None) -> Storage:
    """One public protocol with an exact Run or an already-selected owner."""
    if storage is not None:
        if (storage.path.absolute() != database.absolute() or storage.run_id is None
                or run_id is not None and storage.run_id != run_id):
            raise KernelError("run_scope_changed")
        return storage
    if type(run_id) is not str or not run_id:
        raise KernelError("run_scope_required")
    return Storage(database, run_id=run_id)


@dataclass(frozen=True)
class TaskLocation:
    task: TaskKey
    database: Path
    current_run: str | None


@dataclass(frozen=True)
class RunLocation:
    task: TaskKey
    database: Path
    run_id: str
    current: bool
    created_at: float


@dataclass(frozen=True)
class RunPage:
    items: tuple[RunLocation, ...]
    has_more: bool
    next_after: str | None


@dataclass(frozen=True)
class RepositoryRoute:
    """Repository route from an validated managed workspace.

    Caller supplies workspace.state_root/'kernel.sqlite3', repository_root and
    repository from that workspace's trusted metadata, outside any transaction.
    Missing/read-failed storage never calls initialize implicitly. No method
    publishes JSON, creates a new proposal ID or rereads personal defaults.
    """
    database: Path
    repository_root: Path
    repository: str

    @classmethod
    def for_workspace(cls, workspace: ManagedWorkspace) -> RepositoryRoute:
        """Canonical assembly from trusted workspace metadata, not directory discovery."""
        return cls(workspace.state_root / "kernel.sqlite3", workspace.repository_root, workspace.repository)

    @classmethod
    def from_database(cls, database: Path) -> RepositoryRoute:
        """Read only admitted namespace metadata; never infer a Run from a path.

        Used by explicit state-directory queries. The stored namespace, not a
        directory label or JSON body, supplies repository identity. Each later
        selected opening revalidates that namespace through the same Storage.
        """
        database = database.absolute()
        if not database.exists() and not database.is_symlink():
            cls._require_vacant_root(database)
        with Storage.metadata(database).transaction(write=False) as owner:
            row = owner.one("SELECT repository,repository_root FROM repository_binding WHERE singleton=1")
            route = cls(database, Path(row["repository_root"]), row["repository"])
            if (row["repository"], row["repository_root"]) != (route.repository, str(route.repository_root)):
                raise KernelError("repository_namespace_changed")
        return route

    def __post_init__(self) -> None:
        if not self.database.is_absolute() or not self.repository_root.is_absolute():
            raise ValueError("Exact managed repository paths are required")
        object.__setattr__(self, "repository_root", self.repository_root.resolve())
        object.__setattr__(self, "repository", normalize_repository(self.repository))

    def metadata(self) -> Storage:
        if not self.database.exists() and not self.database.is_symlink():
            self._require_vacant_root(self.database)
            raise UnsupportedState(self.database, "unreadable_state")
        return Storage.metadata(self.database,
            namespace=(self.repository, str(self.repository_root)))

    @staticmethod
    def _require_vacant_root(database: Path) -> None:
        """Fresh initialization refuses every occupied root without reading it.

        This is no old-state reader or migration. Old per-Run databases, orphan
        sidecars and raw/material directories must never become an empty new
        repository merely because the canonical filename is absent. A failed
        inspection is unknown, not permission to create. No directory/file is
        created, removed, repaired or renamed by this check.
        """
        directory = database.parent
        try:
            try:
                info = directory.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(info.st_mode):
                raise UnsupportedState(directory, "repository_state_occupied")
            with os.scandir(directory) as entries:
                for entry in entries:
                    raise UnsupportedState(Path(entry.path), "repository_state_occupied")
        except OSError as error:
            raise UnsupportedState(directory, "unreadable_state") from error

    def initialize(self) -> Storage:
        """Explicit exclusive initialization only, never a read-error repair."""
        self._require_vacant_root(self.database)
        Storage.create(self.database, namespace=(self.repository, str(self.repository_root)))
        return self.metadata()

    def _task(self, task: TaskKey) -> None:
        if (task.repository, task.workspace) != (self.repository, self.repository_root):
            raise KernelError("task_location_identity")

    def _location(self, owner: Transaction, run_id: str) -> RunLocation:
        row = owner.one("SELECT repository,repository_root,parent_issue,created_at FROM runs WHERE id=?", (run_id,))
        task = TaskKey(Path(row["repository_root"]), row["repository"], row["parent_issue"])
        self._task(task)
        task_row = read_repository_task(owner, task)
        link = owner.one("SELECT task_id FROM task_runs WHERE run_id=?", (run_id,))
        if task_row is None or link["task_id"] != task.fingerprint:
            raise KernelError("task_location_identity")
        created = row["created_at"]
        if type(created) not in (int, float) or not math.isfinite(created):
            raise KernelError("run_scope_invalid")
        return RunLocation(task, self.database, run_id, task_row["current_run"] == run_id, float(created))

    def task(self, task: TaskKey) -> TaskLocation | None:
        self._task(task)
        with self.metadata().transaction(write=False) as owner:
            row = read_repository_task(owner, task)
            if row is None:
                return None
            current = row["current_run"]
            if current is not None:
                if type(current) is not str or not current or self._location(owner, current).task != task:
                    raise KernelError("task_location_identity")
            return TaskLocation(task, self.database, current)

    def at_run(self, run_id: str) -> Storage:
        if type(run_id) is not str or not run_id:
            raise KernelError("run_scope_invalid")
        metadata = self.metadata()
        with metadata.transaction(write=False) as owner:
            self._location(owner, run_id)
        return metadata.at_run(run_id)

    def kernel(self, run_id: str) -> Kernel:
        from agent_run.core.kernel import Kernel
        return Kernel._from_storage(self.at_run(run_id))

    def open_task(self, task: TaskKey) -> Kernel | None:
        """The existing replaceability predicate in one canonical metadata snapshot.

        This observation neither binds nor replaces. Atomic create rechecks the
        pointer and predicate in its original writer after external preparation.
        Missing/unreadable database or incomplete facts do not mean new work.
        """
        from agent_run.core.kernel import Kernel
        from agent_run.storage.run_scope import RunTransaction
        self._task(task)
        if not self.database.exists() and not self.database.is_symlink():
            self._require_vacant_root(self.database)
            return None
        metadata = self.metadata()
        with metadata.transaction(write=False) as owner:
            row = read_repository_task(owner, task)
            if row is None or row["current_run"] is None:
                return None
            run_id = row["current_run"]
            if self._location(owner, run_id).task != task:
                raise KernelError("task_location_identity")
            if Kernel._from_storage(metadata)._replaceable_in(RunTransaction(owner, run_id), task, run_id):
                return None
        return Kernel._from_storage(metadata.at_run(run_id))

    def read(self, run_id: str, kind: Literal["status", "history", "settings"], *,
             after: int = 0, limit: int | None = None) -> QueryDocument:
        """A display consumer only; creation/control never parse this projection."""
        from agent_run.queries.run import read_snapshot
        if kind not in {"status", "history", "settings"}:
            raise ValueError("Unknown Run query")
        with self.at_run(run_id).transaction(write=False) as owner:
            return read_snapshot(owner, self.database, kind,
                after=after, limit=limit, expected_run=run_id, expected_repository=self.repository)

    def idle(self, task: TaskKey) -> None:
        from agent_run.core.task_creation import ensure_idle_task
        self._task(task)
        with self.metadata().transaction(ordered=False) as owner:
            ensure_idle_task(owner, task)

    def create(self, task: TaskKey, spec: RunSpec, requirements: Requirements) -> TaskRun:
        from agent_run.core.kernel import Kernel
        from agent_run.core.task_creation import create_repository_task
        self._task(task)
        return create_repository_task(Kernel._from_storage(self.metadata()), task, spec, requirements)

    def reconcile(self, task: TaskKey, proposal_run_id: str, *, deadline: float | None = None) -> TaskRun:
        from agent_run.core.kernel import Kernel
        from agent_run.core.task_creation import reconcile_repository_task
        self._task(task)
        return reconcile_repository_task(Kernel._from_storage(self.metadata()), task,
            proposal_run_id, deadline=deadline)

    def discover(self, *, after: str = "", limit: int = 128,
                 run_id: str | None = None, parent: int | None = None) -> RunPage:
        if (type(after) is not str or type(limit) is not int or not 1 <= limit <= 128
                or run_id is not None and (type(run_id) is not str or not run_id)
                or parent is not None and (type(parent) is not int or parent < 1)
                or run_id is not None and parent is not None):
            raise KernelError("query_limit")
        task = TaskKey(self.repository_root, self.repository, parent) if parent is not None else None
        with self.metadata().transaction(write=False) as owner:
            def page(cursor: str, size: int) -> list[sqlite3.Row]:
                if run_id is not None:
                    return owner.rows("SELECT id FROM runs WHERE id=? AND id>? ORDER BY id LIMIT ?",
                        (run_id, cursor, size), limit=size)
                if task is not None:
                    return owner.rows("SELECT run_id AS id FROM task_runs WHERE task_id=? AND run_id>? ORDER BY run_id LIMIT ?",
                        (task.fingerprint, cursor, size), limit=size)
                return owner.rows("SELECT id FROM runs WHERE id>? ORDER BY id LIMIT ?", (cursor, size), limit=size)
            rows = page(after, limit)
            items = tuple(self._location(owner, row["id"]) for row in rows)
            more = bool(items and page(items[-1].run_id, 1))
            return RunPage(items, more, items[-1].run_id if more else None)
