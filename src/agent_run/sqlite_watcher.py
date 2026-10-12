"""One scoped, idle-between-polls SQLite observer for scheduling."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from agent_run.sqlite_rows import ReadConnection


class SQLiteWatcher:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: ReadConnection | None = None
        self._identity: tuple[int, int] | None = None
        self._generation = 0
        self._active = False

    def __enter__(self) -> SQLiteWatcher:
        if self._active:
            raise RuntimeError("SQLite Run watcher scope is already active")
        self._active = True
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._active = False
        self.close()

    def close(self) -> None:
        connection, self.connection = self.connection, None
        self._identity = None
        if connection is not None:
            connection.close()

    def require_active(self) -> None:
        if not self._active:
            raise RuntimeError("SQLite Run watcher scope is not active")

    def version(self) -> tuple[int, int] | None:
        """Compare data_version only on this same read-only connection.

        All business writes use other connections, including the same thread's
        commits. SQLite reports those commits across threads and processes.
        Replacing/removing the database also invalidates this observation.
        """
        self.require_active()
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.close()
            return None
        identity = stat.st_dev, stat.st_ino
        try:
            if identity != self._identity or self.connection is None:
                self.close()
                self.connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True,
                    timeout=5.0, isolation_level=None, factory=ReadConnection,
                )
                self.connection.execute("PRAGMA query_only = ON")
                self._identity = identity
                self._generation += 1
            assert not self.connection.in_transaction
            value = self.connection.execute("PRAGMA data_version").fetchone()
            if value is None:
                raise ValueError("SQLite Run watcher requires data_version support")
            return self._generation, value[0]
        except sqlite3.Error as error:
            self.close()
            raise ValueError(f"Cannot observe SQLite Run state: {error}") from error
