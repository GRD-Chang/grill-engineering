"""Supported SQLite runtime for the repository's WAL durability contract."""
from __future__ import annotations

import sqlite3
import time

from agent_run.state_errors import IncompatibleRunStateError


def enable_wal(connection: sqlite3.Connection, *, timeout: float) -> None:
    """Let simultaneous first creators finish the WAL mode transition."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if mode != "wal":
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise OSError("SQLite Run state requires WAL journal support")
            return
        except sqlite3.OperationalError as error:
            # SQLite can refuse a competing journal-mode promotion before its
            # busy handler runs. Retry only lock contention, outside a write
            # transaction and within the existing admission timeout.
            if (getattr(error, "sqlite_errorcode", None) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or time.monotonic() >= deadline):
                raise
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))


def require_wal_runtime() -> None:
    version = sqlite3.sqlite_version_info
    # Official WAL-reset fixes: https://sqlite.org/wal.html#walreset
    fixed = (version >= (3, 51, 3)
             or (3, 50, 7) <= version < (3, 51, 0)
             or (3, 44, 6) <= version < (3, 45, 0))
    if not fixed:
        raise IncompatibleRunStateError(
            f"SQLite {sqlite3.sqlite_version} lacks a verified WAL-reset fix; "
            "use SQLite >= 3.51.3, 3.50.7, or 3.44.6 "
            "(https://sqlite.org/wal.html#walreset)"
        )
