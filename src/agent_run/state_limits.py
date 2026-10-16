"""Independent budgets for records, query work and committed SQLite storage.

Evidence bodies live inside SQLite, so their physical cost is already included
in database/WAL files. No quota is renewed by a projection or a nested query.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any


MAX_CURRENT_BYTES = 256 * 1024
MAX_QUERY_BYTES = 32 * 1024 * 1024
MAX_QUERY_ROWS = 65_536
QUERY_BATCH_ROWS = 128
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_STORAGE_BYTES = 1024 * 1024 * 1024


class QueryLimitError(ValueError):
    """The requested view cannot be returned within its explicit budget."""


class StorageCapacityError(OSError):
    """The shared database has no admitted capacity for another commit."""


@dataclass
class QueryBudget:
    max_bytes: int
    read_bytes: int = 0
    rows: int = 0

    def consume_bytes(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("Invalid state query byte count")
        if self.read_bytes + size > self.max_bytes:
            raise QueryLimitError(f"State query exceeds {self.max_bytes} byte read limit")
        self.read_bytes += size

    def consume_rows(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("Invalid state query row count")
        if self.rows + count > MAX_QUERY_ROWS:
            raise QueryLimitError(f"State query exceeds {MAX_QUERY_ROWS} row limit")
        self.rows += count


_query: ContextVar[QueryBudget | None] = ContextVar("state_query_budget", default=None)


@contextmanager
def query_scope(*, max_bytes: int | None = None) -> Iterator[QueryBudget]:
    limit = MAX_QUERY_BYTES if max_bytes is None else min(MAX_QUERY_BYTES, max_bytes)
    if limit < 0:
        raise ValueError("State query limit must not be negative")
    active = _query.get()
    if active is not None:
        active.max_bytes = min(active.max_bytes, limit)
        yield active
        return
    budget = QueryBudget(limit)
    token = _query.set(budget)
    try:
        yield budget
    finally:
        _query.reset(token)


def consume_query_bytes(size: int) -> None:
    budget = _query.get()
    if budget is not None:
        budget.consume_bytes(size)


def consume_query_rows(count: int = 1) -> None:
    budget = _query.get()
    if budget is not None:
        budget.consume_rows(count)


def account_batch(sizes: Iterable[int]) -> None:
    """Check narrow row metadata before transferring a batch of actual bodies."""
    count = total = 0
    for size in sizes:
        if type(size) is not int or size < 0:
            raise ValueError("Invalid state query batch size")
        count += 1
        total += size
        if count > QUERY_BATCH_ROWS or total > MAX_BATCH_BYTES:
            raise QueryLimitError("State query batch exceeds row or byte limit")
    consume_query_rows(count)
    consume_query_bytes(total)


_RESPONSE_CHUNK_BYTES = 16 * 1024
_RESPONSE_STRING_CHARS = (_RESPONSE_CHUNK_BYTES - 2) // 6


def _response_upper_bound(value: Any, remaining: int, depth: int = 8) -> int | None:
    """Admit only small built-in subtrees, without encoding or copying them.

    Six bytes cover one escaped control character or any literal UTF-8 code
    point. Container scans stop at the byte/depth bound; subclasses keep their
    normal iteration behavior in the sequential path below.
    """
    kind = type(value)
    if kind is str:
        size = 6 * len(value) + 2
    elif value is None:
        size = 4
    elif kind is bool:
        size = 5
    elif kind is int and value.bit_length() <= 2048:
        # Also stays below Python's minimum configurable decimal-digit limit.
        size = 3 + value.bit_length() // 3
    elif kind is float and math.isfinite(value):
        size = 32
    elif depth > 0 and kind in (dict, list, tuple):
        size = 2
        if kind is dict:
            for key, item in value.items():
                if type(key) not in (str, int, float, bool, type(None)):
                    return None
                key_size = _response_upper_bound(key, remaining - size - 2, 0)
                if key_size is None:
                    return None
                size += key_size + 2 + (0 if type(key) is str else 2)
                item_size = _response_upper_bound(item, remaining - size, depth - 1)
                if item_size is None:
                    return None
                size += item_size
        else:
            for item in value:
                item_size = _response_upper_bound(item, remaining - size - 1, depth - 1)
                if item_size is None:
                    return None
                size += item_size + 1
    else:
        return None
    return size if size <= remaining else None


class _ResponseMeter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.size = 0
        self.encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.ancestors: set[int] = set()

    def consume(self, size: int) -> None:
        self.size += size
        if self.size > self.limit:
            raise QueryLimitError(f"State query response exceeds {self.limit} byte limit")

    def measure(self, value: Any) -> None:
        bound = min(_RESPONSE_CHUNK_BYTES, self.limit - self.size)
        if _response_upper_bound(value, bound) is not None:
            # JSONEncoder.encode selects the standard library's native encoder.
            self.consume(len(self.encoder.encode(value).encode("utf-8")))
        elif isinstance(value, str):
            self.consume(1)
            for offset in range(0, str.__len__(value), _RESPONSE_STRING_CHARS):
                part = str.__getitem__(value, slice(offset, offset + _RESPONSE_STRING_CHARS))
                self.consume(len(self.encoder.encode(part).encode("utf-8")) - 2)
            self.consume(1)
        elif isinstance(value, (dict, list, tuple)):
            if not value:
                self.consume(2)
                return
            identity = id(value)
            if identity in self.ancestors:
                raise ValueError("Circular reference detected")
            self.ancestors.add(identity)
            try:
                self.consume(1)
                if isinstance(value, dict):
                    for index, (key, item) in enumerate(value.items()):
                        if index:
                            self.consume(1)
                        self.measure_key(key)
                        self.consume(1)
                        self.measure(item)
                else:
                    for index, item in enumerate(value):
                        if index:
                            self.consume(1)
                        self.measure(item)
                self.consume(1)
            finally:
                self.ancestors.remove(identity)
        else:
            # Preserve standard errors for unsupported values, non-finite
            # floats and integers beyond the interpreter's decimal-digit limit.
            self.consume(len(self.encoder.encode(value).encode("utf-8")))

    def measure_key(self, key: Any) -> None:
        if isinstance(key, str):
            self.measure(key)
        elif key is None or isinstance(key, (int, float)):
            # JSON renders these scalar keys as their quoted JSON spellings.
            self.consume(2)
            self.measure(key)
        else:
            raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def check_response(state: Mapping[str, Any], *, max_bytes: int | None = None) -> int:
    """Measure exact UTF-8 JSON bytes using bounded native-encoded subtrees."""
    limit = MAX_QUERY_BYTES if max_bytes is None else min(MAX_QUERY_BYTES, max_bytes)
    budget = _query.get()
    if budget is not None:
        limit = min(limit, budget.max_bytes)
    meter = _ResponseMeter(limit)
    meter.measure(state)
    return meter.size


def require_record_size(
    connection: sqlite3.Connection, table: str, columns: Iterable[str], where: str,
    parameters: tuple[Any, ...], *, limit: int | None = None,
) -> None:
    """Bound the actual fixed columns and references before fetching a record."""
    selected = tuple(columns)
    if not selected or any(re.fullmatch(r"[a-zA-Z_]\w*", value) is None for value in (table, *selected)):
        raise ValueError("Invalid state record columns")
    expression = "+".join(f"COALESCE(LENGTH(CAST({column} AS BLOB)),0)+16" for column in selected)
    maximum = MAX_CURRENT_BYTES if limit is None else limit
    for (size,) in connection.execute(f"SELECT {expression} FROM {table} WHERE {where}", parameters):
        if type(size) is not int or size > maximum:
            raise ValueError(f"State current record exceeds {maximum} byte limit")
        consume_query_rows()
        consume_query_bytes(size)


def storage_usage(path: Path) -> dict[str, int]:
    result = {}
    for name, suffix in (("database", ""), ("wal", "-wal"), ("shm", "-shm")):
        try:
            result[name] = Path(str(path) + suffix).stat().st_size
        except FileNotFoundError:
            result[name] = 0
    return result


def check_storage(path: Path, connection: sqlite3.Connection) -> None:
    """Reserve the remaining commit frames before admitting a durable change.

    Already spilled WAL frames are included in file sizes. At commit SQLite
    can still write each currently dirty database page; reserve one frame per
    database page plus a WAL header. Also reserve eventual database growth on
    checkpoint. This conservative admission can reject before the hard cap;
    it never deletes evidence, forces a reader checkpoint or changes quotas.
    """
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    database = pages * page_size
    usage = storage_usage(path)
    if max(database, usage["database"]) > MAX_DATABASE_BYTES:
        raise StorageCapacityError(f"SQLite database exceeds {MAX_DATABASE_BYTES} byte capacity")
    reserve = pages * (page_size + 24) + 32
    admitted = max(database, usage["database"]) + usage["wal"] + usage["shm"] + reserve
    if admitted > MAX_STORAGE_BYTES:
        raise StorageCapacityError(
            f"SQLite storage exceeds {MAX_STORAGE_BYTES} byte capacity including commit reserve"
        )


def configure_storage(connection: sqlite3.Connection) -> None:
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    pages = MAX_DATABASE_BYTES // page_size
    if connection.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] > pages:
        raise StorageCapacityError(f"SQLite database exceeds {MAX_DATABASE_BYTES} byte capacity")
    # These are cleanup preferences, not substitutes for physical admission:
    # a pinned read snapshot can legitimately prevent WAL reset.
    connection.execute("PRAGMA journal_size_limit=16777216")
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BATCH_BYTES)
