"""Supported SQLite runtime for the repository's WAL durability contract."""
from __future__ import annotations


import sqlite3

from agent_run.errors import IncompatibleRunStateError


# Current projections use more than sqlite3's default 128 statement slots.
# Keep this per-connection cache bounded; closing its scope releases all SQL.
STATEMENT_CACHE_SIZE = 256


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
