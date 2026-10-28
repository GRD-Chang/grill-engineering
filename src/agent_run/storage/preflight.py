"""Read-only support boundary, never a state converter or import authenticator.

SQLite reads its committed WAL normally. Shared-memory reader coordination is
not a durable business fact; neither immutable=1 nor journal recovery is used
to classify the original database.
"""
from __future__ import annotations


from contextlib import contextmanager
import errno
import math
from pathlib import Path
import sqlite3
import stat
from time import get_clock_info
from typing import Iterator

from agent_run.errors import KernelError
from agent_run.storage.repository_schema import PROTOCOL, repository_schema_objects


LEGACY_PROTOCOL = "sqlite-work-records-v6"
SOURCE_FENCE_PREFIX = "original-source-to-native/1:"


class UnsupportedState(KernelError):
    """An identifiable location is not permission to open it for native writes."""

    def __init__(self, path: Path, reason: str, protocol: str | None = None) -> None:
        self.path = path.absolute()
        self.reason = reason
        self.protocol = protocol
        self.recovery_hint = (
            "Use a Runner supporting sqlite-work-records-v6 for the original state."
            if reason == "legacy_v6" else
            "Use the matching migration-enabled Runner for the current authority; do not clear the fence or assume v6 is current."
            if reason == "fenced_source" else
            "Use the matching experimental Runner; the native protocol alone does not identify imported-state compatibility."
            if reason in {"imported_state", "source_origin"} else
            "Preserve the database and sidecars and identify the matching Runner before recovery."
        )
        super().__init__("unsupported_state", f"{reason}; path={self.path}; protocol={protocol!r}; {self.recovery_hint}")

    def diagnostic(self) -> dict[str, str | None]:
        return {"code": self.code, "reason": self.reason, "path": str(self.path),
                "protocol": self.protocol, "recovery_hint": self.recovery_hint}


class _ReadBudget:
    """One local observation allowance, never a persisted save clock authority."""

    def __init__(self, path: Path, timeout: float, deadline: float | None) -> None:
        self.path = path
        try:
            clock = get_clock_info('monotonic')
            if not clock.monotonic or clock.adjustable:
                raise ValueError('Monotonic observation budget unavailable')
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('Finite positive observation allowance required')
            self.last = self._now()
            self.deadline = self.last + timeout
            if deadline is not None:
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    raise ValueError('Finite observation deadline required')
                self.deadline = min(self.deadline, deadline)
            if not math.isfinite(self.deadline) or self.deadline <= self.last:
                raise ValueError('Observation allowance expired')
        except (OSError, TypeError, ValueError, OverflowError) as error:
            raise UnsupportedState(path, 'uncertain_state') from error

    def _now(self) -> float:
        # Use the caller's existing contention-clock source. Import lazily:
        # save_order also uses UnsupportedState, but no save clock is captured.
        from agent_run.storage import save_order
        try:
            value = save_order.monotonic()  # type: ignore[attr-defined]  # Share the owner's internal source.
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Monotonic observation budget unavailable')
            return value
        except (OSError, TypeError, ValueError, OverflowError) as error:
            raise UnsupportedState(self.path, 'uncertain_state') from error

    def remaining(self) -> float:
        now = self._now()
        if now < self.last or now >= self.deadline:
            raise UnsupportedState(self.path, 'uncertain_state')
        self.last = now
        return self.deadline - now


def _wait_sidecar_absent(path: Path, sidecar: Path, identity: tuple[int, int], budget: _ReadBudget) -> None:
    # A pending unlink can remain visible to several fresh pathname lookups.
    # Only absence resolves it; an existing replacement is never adopted.
    for _ in range(32):
        budget.remaining()
        try:
            current = sidecar.stat(follow_symlinks=False)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            budget.remaining()
            return
        budget.remaining()
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 0
                or (current.st_dev, current.st_ino) != identity):
            raise UnsupportedState(path, 'uncertain_state')
    raise UnsupportedState(path, 'uncertain_state')


@contextmanager
def read_state(path: Path, *, timeout: float = 1.0,
               deadline: float | None = None) -> Iterator[sqlite3.Connection]:
    """Open an existing ordinary file with WAL visibility and no recovery writes.

    A rollback journal may require recovery even for a reader: refuse it rather
    than repairing the original. Missing files and aliases never mean new state.
    """
    budget = _ReadBudget(path, timeout, deadline)
    waited = False
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsupportedState(path, "uncertain_state")
        directory = path.parent.stat()
        if not stat.S_ISDIR(directory.st_mode):
            raise UnsupportedState(path, "uncertain_state")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            try:
                metadata = sidecar.stat(follow_symlinks=False)
            except OSError as error:
                # SQLite's last connection can remove its WAL/SHM normally.
                # Only an absent sidecar is optional; the database is not.
                if error.errno == errno.ENOENT:
                    continue
                raise
            if (suffix in {"-wal", "-shm"} and stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink == 0):
                _wait_sidecar_absent(path, sidecar, (metadata.st_dev, metadata.st_ino), budget)
                waited = True
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or suffix == "-journal" and metadata.st_size:
                raise UnsupportedState(path, "uncertain_state")
        if waited:
            current, parent = path.lstat(), path.parent.stat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISDIR(parent.st_mode)
                    or (parent.st_dev, parent.st_ino) != (directory.st_dev, directory.st_ino)):
                raise UnsupportedState(path, 'uncertain_state')
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True,
                             isolation_level=None, timeout=budget.remaining())
    except (OSError, sqlite3.Error) as error:
        raise UnsupportedState(path, "unreadable_state") from error
    try:
        try:
            budget.remaining()
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            # BEGIN is deferred: read the database before checking the identity
            # of the snapshot we are about to expose to the caller.
            try:
                db.execute(f"PRAGMA busy_timeout={int(budget.remaining() * 1000)}")
                db.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
            except sqlite3.Error as error:
                # This schema read formerly happened in support classification.
                raise UnsupportedState(path, "uncertain_state") from error
            current = path.lstat()
            parent = path.parent.stat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISDIR(parent.st_mode)
                    or (parent.st_dev, parent.st_ino) != (directory.st_dev, directory.st_ino)):
                raise UnsupportedState(path, "uncertain_state")
        except (OSError, sqlite3.Error) as error:
            raise UnsupportedState(path, "unreadable_state") from error
        budget.remaining()
        yield db
    finally:
        db.close()


def _protocol(db: sqlite3.Connection, table: str, path: Path) -> str:
    # Validate the named object before reading it; never run a supplied view.
    kind = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (table,)).fetchall()
    if (len(kind) != 1 or kind[0][0] != "table" or not isinstance(kind[0][1], str)
            or not kind[0][1].upper().startswith("CREATE TABLE ")):
        raise UnsupportedState(path, "uncertain_state")
    query = ("SELECT value FROM schema_meta WHERE key='protocol'" if table == "schema_meta"
             else "SELECT protocol AS value FROM meta")
    rows = db.execute(f"SELECT typeof(value),length(value),substr(value,1,256) FROM ({query}) AS p").fetchmany(2)
    if len(rows) != 1 or rows[0][0] != "text" or not 0 < rows[0][1] <= 16384:
        raise UnsupportedState(path, "uncertain_state")
    protocol = rows[0][2]
    return SOURCE_FENCE_PREFIX + "<fenced>" if protocol.startswith(SOURCE_FENCE_PREFIX) else protocol


def require_supported_connection(db: sqlite3.Connection, path: Path) -> str:
    """The same predicate before opening for writes and under the write lock."""
    try:
        old = db.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_meta'").fetchone()
        if old is not None:
            protocol = _protocol(db, "schema_meta", path)
            reason = ("legacy_v6" if protocol == LEGACY_PROTOCOL else
                      "fenced_source" if protocol.startswith(SOURCE_FENCE_PREFIX) else "unknown_protocol")
            raise UnsupportedState(path, reason, protocol)
        protocol = _protocol(db, "meta", path)
        if protocol != PROTOCOL:
            raise UnsupportedState(path, "unknown_protocol", protocol)
        objects = db.execute("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'").fetchmany(128)
        actual = {(row[0], row[1]): row[2] for row in objects}
        if actual != repository_schema_objects():
            raise UnsupportedState(path, "uncertain_state", protocol)
        for table, column in (("history", "kind"), ("material", "purpose")):
            kind = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (table,)).fetchall()
            if (len(kind) != 1 or kind[0][0] != "table" or not isinstance(kind[0][1], str)
                    or not kind[0][1].upper().startswith("CREATE TABLE ")):
                raise UnsupportedState(path, "uncertain_state", protocol)
            # All bridge producers reserve v6_* names, including partial installs
            # and later native Runs inheriting an original-source authority.
            marker = db.execute(f"SELECT {column} FROM {table} WHERE {column} GLOB 'v6_*' LIMIT 1").fetchone()
            if marker is not None:
                origin = db.execute(f"SELECT 1 FROM {table} WHERE {column} IN ('v6_original_cutover','v6_source_origin') LIMIT 1").fetchone()
                raise UnsupportedState(path, "source_origin" if origin else "imported_state", protocol)
        return protocol
    except sqlite3.Error as error:
        raise UnsupportedState(path, "uncertain_state") from error


def require_supported_state(path: Path, *, timeout: float = 1.0, deadline: float | None = None) -> str:
    """Return a supported native protocol, otherwise raise a located refusal."""
    with read_state(path, timeout=timeout, deadline=deadline) as db:
        return require_supported_connection(db, path)
