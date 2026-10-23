"""Read-only support boundary, never a state converter or import authenticator.

SQLite reads its committed WAL normally. Shared-memory reader coordination is
not a durable business fact; neither immutable=1 nor journal recovery is used
to classify the original database.
"""
from __future__ import annotations


from contextlib import contextmanager
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from agent_run.errors import KernelError


PROTOCOL = "independent-delivery-kernel-v19"
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


@contextmanager
def read_state(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing ordinary file with WAL visibility and no recovery writes.

    A rollback journal may require recovery even for a reader: refuse it rather
    than repairing the original. Missing files and aliases never mean new state.
    """
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsupportedState(path, "uncertain_state")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            try:
                metadata = sidecar.stat(follow_symlinks=False)
            except FileNotFoundError:
                # SQLite's last connection can remove its WAL/SHM normally.
                # Only an absent sidecar is optional; the database is not.
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or suffix == "-journal" and metadata.st_size:
                raise UnsupportedState(path, "uncertain_state")
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True,
                             isolation_level=None, timeout=1.0)
    except (OSError, sqlite3.Error) as error:
        raise UnsupportedState(path, "unreadable_state") from error
    try:
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
        except sqlite3.Error as error:
            raise UnsupportedState(path, "unreadable_state") from error
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


def require_supported_state(path: Path) -> str:
    """Return a supported native protocol, otherwise raise a located refusal."""
    with read_state(path) as db:
        return require_supported_connection(db, path)
