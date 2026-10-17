"""SQLite 连接、只读快照、单事务和物理容量的统一生命周期。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_run import sqlite_control
from agent_run import sqlite_work_records as work_records
from agent_run import sqlite_run_audit as audit
from agent_run.sqlite_runtime import STATEMENT_CACHE_SIZE, enable_wal, require_wal_runtime
from agent_run.sqlite_query_rows import QueryConnection
from agent_run.sqlite_scope import context_for
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.state_limits import MAX_QUERY_BYTES, query_scope, configure_storage, check_storage

_PROTOCOL = "sqlite-work-records-v6"
_BASE_TABLES = {
    "schema_meta": "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "runs": "CREATE TABLE runs (run_id TEXT PRIMARY KEY, repository TEXT, parent_number INTEGER, status TEXT, phase TEXT, created_at TEXT, updated_at TEXT)",
}
_BASE_INDEXES = {"runs_parent": "CREATE INDEX runs_parent ON runs(repository COLLATE NOCASE,parent_number,created_at)"}
_CANONICAL_TABLES = {**_BASE_TABLES, **sqlite_control.TABLES, **work_records.TABLES, **audit.TABLES}
_CANONICAL_INDEXES = {**_BASE_INDEXES, **sqlite_control.INDEXES, **work_records.INDEXES, **audit.INDEXES}
_SCHEMA = (*_CANONICAL_TABLES.values(), *_CANONICAL_INDEXES.values())
_SCHEMA_QUERY = (
    "SELECT type,name,sql FROM sqlite_master WHERE type!='index' OR name IN ("
    + ",".join("?" for _ in _CANONICAL_INDEXES) + ")"
)
ReadConnection = QueryConnection


@dataclass
class _WorkConnections:
    identity: tuple[int, int] | None = None
    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None

    def close(self) -> None:
        reader, writer = self.reader, self.writer
        self.reader = self.writer = None
        self.identity = None
        try:
            if reader is not None:
                reader.close()
        finally:
            if writer is not None:
                writer.close()


class SQLiteStateConnection:
    """连接只属于一次边界或本线程work_session；空闲连接不持有事务。"""

    def __init__(self, root: Path, *, max_bytes: int = MAX_QUERY_BYTES) -> None:
        self.root = root
        self.path = root / "state.sqlite3"
        self.max_bytes = max_bytes
        self._local = context_for(root)

    @contextmanager
    def work_session(self) -> Iterator[None]:
        """Reuse only prepared statements during this thread's business work."""
        if getattr(self._local, "work_connections", None) is not None:
            yield
            return
        connections = _WorkConnections()
        self._local.work_connections = connections
        try:
            yield
        finally:
            self._local.work_connections = None
            connections.close()

    def _work_connection(self, *, readonly: bool) -> sqlite3.Connection | None:
        connections = cast(_WorkConnections, self._local.work_connections)
        try:
            stat = self.path.stat()
            identity: tuple[int, int] | None = stat.st_dev, stat.st_ino
        except FileNotFoundError:
            identity = None
        if identity != connections.identity:
            connections.close()
        try:
            if readonly:
                if identity is None:
                    return None
                if connections.reader is None:
                    connections.reader = sqlite3.connect(
                        self.path.resolve().as_uri() + "?mode=ro",
                        uri=True,
                        timeout=5.0,
                        isolation_level=None,
                        factory=ReadConnection,
                        cached_statements=STATEMENT_CACHE_SIZE,
                    )
                    connections.reader.execute("PRAGMA query_only = ON")
                connection: sqlite3.Connection = connections.reader
            else:
                if connections.writer is None:
                    connections.writer = sqlite3.connect(
                        self.path, timeout=5.0, isolation_level=None, cached_statements=STATEMENT_CACHE_SIZE,
                        factory=QueryConnection,
                    )
                connection = connections.writer
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                connections.close()
                raise
            opened_identity = stat.st_dev, stat.st_ino
            if identity is not None and identity != opened_identity:
                connections.close()
                raise OSError("SQLite Run database changed while opening a work connection")
            connections.identity = opened_identity
            return connection
        except BaseException:
            connections.close()
            raise

    @staticmethod
    def _check_schema(connection: sqlite3.Connection, *, allow_empty: bool = False) -> bool:
        # Every table/view/trigger and required index is checked each time.
        # Compatible extra indexes never affect validity; omit their unused
        # definitions and the ordering that the name-based comparison ignores.
        definitions = list(connection.execute(_SCHEMA_QUERY, tuple(_CANONICAL_INDEXES)))
        tables = {
            name: ddl for kind, name, ddl in definitions if kind == "table" and not name.startswith("sqlite_")
        }
        if not tables and allow_empty:
            return False
        if any(kind in {"view", "trigger"} for kind, _, _ in definitions) or {
            key: value.rstrip() for key, value in tables.items()
        } != {key: value.rstrip() for key, value in _CANONICAL_TABLES.items()}:
            raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if dict(connection.execute("SELECT key,value FROM schema_meta")) != {"protocol": _PROTOCOL}:
            raise IncompatibleRunStateError("Incompatible SQLite Run state protocol")
        indexes = {name: ddl for kind, name, ddl in definitions if kind == "index"}
        if any(indexes.get(name) != ddl for name, ddl in _CANONICAL_INDEXES.items()):
            raise IncompatibleRunStateError("Incompatible SQLite Run state indexes")
        return True

    @contextmanager
    def atomic(self, *, timeout: float = 5.0) -> Iterator[None]:
        """Commit all nested repository calls together, or roll them back.

        Reentry joins the current transaction; it is not an independent commit
        or savepoint. A caught inner failure marks the whole transaction failed.
        """
        active = getattr(self._local, "connection", None)
        if active is not None:
            try:
                yield
            except BaseException:
                self._local.failed = True
                raise
            return
        require_wal_runtime()
        self.root.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        session = getattr(self._local, "work_connections", None)
        try:
            connection = (
                self._work_connection(readonly=False)
                if session is not None
                else sqlite3.connect(
                    self.path, timeout=timeout, isolation_level=None, cached_statements=STATEMENT_CACHE_SIZE,
                    factory=QueryConnection,
                )
            )
            assert connection is not None
            connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
            initialized = self._check_schema(connection, allow_empty=True)
            # Reapplying this flag expires all prepared statements, even when
            # unchanged. A scoped writer still proves its actual flag at every
            # boundary and restores it before BEGIN when necessary.
            if session is None or connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                connection.execute("PRAGMA foreign_keys = ON")
            configure_storage(connection)
            enable_wal(connection, timeout=timeout)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            if initialized:
                self._check_schema(connection)
            else:
                # A concurrent creator may have initialized the database while
                # this connection waited for the write transaction.
                if not self._check_schema(connection, allow_empty=True):
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_meta(key, value) VALUES ('protocol', ?)", (_PROTOCOL,))
            check_storage(self.path, connection)
            self._local.connection = connection
            self._local.schema_proof = (
                connection.total_changes, connection.execute("PRAGMA schema_version").fetchone()[0],
            )
            self._local.failed = False
            self._local.rollback_callbacks = []
            self._local.work_view = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            self._local.object_archives = {}
            self._local.object_attempts = {}
            try:
                yield
                if self._local.failed:
                    raise RuntimeError("SQLite Run transaction contains a failed nested operation")
                check_storage(self.path, connection)
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                finally:
                    for invalidate in self._local.rollback_callbacks:
                        invalidate()
                raise
        except sqlite3.Error as error:
            if session is not None:
                session.close()
            raise OSError(f"Cannot persist SQLite Run state: {error}") from error
        except BaseException:
            if session is not None:
                session.close()
            raise
        finally:
            self._local.connection = None
            self._local.schema_proof = None
            self._local.failed = False
            self._local.rollback_callbacks = []
            self._local.work_view = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            self._local.object_archives = {}
            self._local.object_attempts = {}
            if connection is not None and session is None:
                # SQLite handles checkpoints and WAL/SHM cleanup on writer
                # close when other connections allow it. Readers never perform
                # this maintenance or manually unlink coordination files.
                connection.close()

    @property
    def in_transaction(self) -> bool:
        return getattr(self._local, "connection", None) is not None

    def _on_rollback(self, invalidate: Callable[[], None]) -> None:
        """Invalidate provisional in-memory baselines if the shared commit fails."""
        if self.in_transaction:
            self._local.rollback_callbacks.append(invalidate)

    @contextmanager
    def use_reader(self, connection: sqlite3.Connection | None) -> Iterator[None]:
        previous = getattr(self._local, "reader", None)
        self._local.reader = connection
        try:
            yield
        finally:
            self._local.reader = previous

    @contextmanager
    def _reading(self, run_id: str | None = None, *, check_schema: bool = True) -> Iterator[sqlite3.Connection | None]:
        with query_scope(max_bytes=self.max_bytes):
            active = getattr(self._local, "connection", None)
            if active is not None:
                if check_schema:
                    # BEGIN IMMEDIATE excludes other writers. Reuse only the
                    # schema already checked in this exact transaction, while
                    # detecting both DDL and direct writable_schema/protocol
                    # changes made by the transaction's own connection.
                    proof = (active.total_changes, active.execute("PRAGMA schema_version").fetchone()[0])
                    if getattr(self._local, "schema_proof", None) != proof:
                        self._check_schema(active)
                        self._local.schema_proof = proof
                yield active
                return
            session = getattr(self._local, "work_connections", None)
            if not self.path.is_file():
                if session is not None:
                    session.close()
                yield None
                return
            require_wal_runtime()
            borrowed = getattr(self._local, "reader", None)
            connection = borrowed
            failed = False
            try:
                if connection is None:
                    connection = (
                        self._work_connection(readonly=True)
                        if session is not None
                        else sqlite3.connect(
                            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0, isolation_level=None,
                            cached_statements=STATEMENT_CACHE_SIZE,
                            factory=QueryConnection,
                        )
                    )
                    if connection is None:
                        yield None
                        return
                    # Like foreign_keys, setting query_only invalidates prepared
                    # SQL even when unchanged. Prove the actual flag on each
                    # boundary without discarding the work session's statements.
                    if session is None or connection.execute("PRAGMA query_only").fetchone() != (1,):
                        connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                yield connection if not check_schema or self._check_schema(connection, allow_empty=True) else None
            except sqlite3.Error as error:
                failed = True
                raise ValueError(f"Cannot read SQLite Run state: {error}") from error
            except BaseException:
                failed = True
                raise
            finally:
                if connection is not None:
                    try:
                        connection.rollback()
                    finally:
                        if borrowed is None and session is None:
                            connection.close()
                        elif borrowed is None and session is not None and failed:
                            session.close()
