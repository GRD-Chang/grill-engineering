"""有界传输查询所需的窄身份；超限正文不会进入 Python。"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from collections import OrderedDict
from functools import lru_cache
import re
import sqlite3
from typing import Any

from agent_run.state_limits import MAX_CURRENT_BYTES, account_batch
from agent_run.sqlite_runtime import STATEMENT_CACHE_SIZE


def _query_columns(c: sqlite3.Connection, sql: str, parameters: Sequence[Any]) -> tuple[str, ...]:
    probe = c.execute("SELECT * FROM (" + sql + ") LIMIT 0", parameters)
    try:
        columns = tuple(f"field_{index}" for index in range(len(probe.description or ())))
    finally:
        probe.close()
    if not columns:
        raise ValueError("State bounded query requires SELECT result columns")
    return columns


class QueryConnection(sqlite3.Connection):
    """Retain bounded SQL result shapes for this connection, never row facts.

    Explicit SELECT expressions have a fixed column count independent of
    bindings, data and schema changes. Wildcards may expand differently, so
    those queries always ask SQLite for their current shape.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._query_shapes: OrderedDict[str, tuple[str, ...]] = OrderedDict()

    def query_columns(self, sql: str, parameters: Sequence[Any]) -> tuple[str, ...]:
        # COUNT(*) is a single expression, not a column expansion. All other
        # asterisks conservatively keep the live probe (including literals).
        if "*" in sql.replace("COUNT(*)", ""):
            return _query_columns(self, sql, parameters)
        columns = self._query_shapes.get(sql)
        if columns is None:
            columns = _query_columns(self, sql, parameters)
            self._query_shapes[sql] = columns
            if len(self._query_shapes) > STATEMENT_CACHE_SIZE:
                self._query_shapes.popitem(last=False)
        else:
            self._query_shapes.move_to_end(sql)
        return columns

    def close(self) -> None:
        self._query_shapes.clear()
        super().close()


@lru_cache(maxsize=32)
def _row_projection(columns: tuple[str, ...], maximum: int) -> tuple[str, str]:
    """Retain only bounded column templates, never SQL inputs or state facts."""
    if not columns or any(re.fullmatch(r"[a-zA-Z_]\w*", column) is None for column in columns):
        raise ValueError("Invalid narrow query columns")
    size = "+".join(f"COALESCE(LENGTH(CAST(q.{column} AS BLOB)),0)+16" for column in columns)
    selected = ",".join(f"CASE WHEN q._state_record_bytes<={maximum} THEN q.{column} END" for column in columns)
    return size, selected


def bounded_rows(
    c: sqlite3.Connection, sql: str, parameters: tuple[Any, ...], columns: tuple[str, ...],
) -> Iterator[tuple[Any, ...]]:
    size, selected = _row_projection(columns, MAX_CURRENT_BYTES)
    # The CASE is evaluated inside SQLite before a damaged long TEXT is
    # copied to Python. One row is one batch, below the shared 8 MiB ceiling.
    # OFFSET prevents subquery flattening. SQLite can stream this coroutine
    # one row at a time and evaluate the length expression once, rather than
    # expand it into every CASE of a wide Job or Invocation record.
    measured_sql = f"SELECT q.*,{size} AS _state_record_bytes FROM ({sql}) q LIMIT -1 OFFSET 0"
    cursor = c.execute(f"SELECT {selected},q._state_record_bytes FROM ({measured_sql}) q", parameters)
    try:
        while (row := cursor.fetchone()) is not None:
            measured = row[-1]
            if type(measured) is not int or measured > MAX_CURRENT_BYTES:
                raise ValueError("State narrow query record exceeds byte limit")
            account_batch([measured])
            yield row[:-1]
    finally:
        try:
            cursor.close()
        except sqlite3.ProgrammingError:
            # An exception traceback can keep this generator alive until
            # after its owning read connection has already been closed.
            pass


class QueryRows(Iterator[tuple[Any, ...]]):
    """Keep SQLite's small read interface while every returned row is metered."""

    def __init__(self, rows: Iterator[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __iter__(self) -> QueryRows:
        return self

    def __next__(self) -> tuple[Any, ...]:
        return next(self._rows)

    def fetchone(self) -> Any:
        return next(self._rows, None)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


def bounded_query(c: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> QueryRows:
    """Meter explicit-domain SELECT results, including scalar and identity reads.

    LIMIT 0 obtains only the prepared result shape. Fixed aliases then let the
    shared CASE guard check actual lengths before any selected value enters
    Python, without parsing SQL or evaluating an archival query twice.
    """
    columns = c.query_columns(sql, parameters) if isinstance(c, QueryConnection) else _query_columns(c, sql, parameters)
    named = "WITH measured(" + ",".join(columns) + ") AS (" + sql + ") SELECT * FROM measured"
    return QueryRows(bounded_rows(c, named, tuple(parameters), columns))


def integer_aggregate(c: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> Any:
    """Read one fixed COUNT/LENGTH aggregate without wrapping its expression.

    Callers supply only SQLite built-in integer aggregates, never stored
    columns or evidence. SQLite's signed 64-bit integers need at most 20
    bytes; their row/byte charges remain identical to bounded_query. Bodies
    still pass through the original guarded query after this size admission.
    """
    cursor = c.execute(sql, parameters)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is not None:
        if any(type(value) is not int for value in row):
            raise ValueError("State aggregate must return only integers")
        measured = sum(len(str(value)) + 16 for value in row)
        if measured > MAX_CURRENT_BYTES:
            raise ValueError("State narrow query record exceeds byte limit")
        account_batch([measured])
    return row


def current_collection_size(
    c: sqlite3.Connection, sql: str, parameters: tuple[Any, ...], columns: tuple[str, ...],
    *, limit: int = MAX_CURRENT_BYTES,
) -> None:
    """Admit one explicit current collection without charging its body twice."""
    if not columns or any(re.fullmatch(r"[a-zA-Z_]\w*", column) is None for column in columns):
        raise ValueError("Invalid current collection columns")
    expression = "+".join(f"COALESCE(LENGTH(CAST(q.{column} AS BLOB)),0)+16" for column in columns)
    size = integer_aggregate(c, f"SELECT COALESCE(SUM({expression}),0) FROM ({sql}) q", parameters)[0]
    if type(size) is not int or size > limit:
        raise ValueError(f"State current collection exceeds {limit} byte limit")
