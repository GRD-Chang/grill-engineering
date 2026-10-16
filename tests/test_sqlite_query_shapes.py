"""SQL 形状可复用，行事实、额度和通配列展开仍逐次读取。"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tracemalloc

import pytest

from agent_run.sqlite_query_rows import QueryConnection, bounded_query, integer_aggregate
from agent_run.state_limits import MAX_CURRENT_BYTES, QueryLimitError, query_scope


def test_repeated_explicit_query_avoids_redundant_shape_reads_and_reads_new_facts(tmp_path: Path) -> None:
    path = tmp_path / "facts.sqlite3"
    with closing(sqlite3.connect(path, factory=QueryConnection, isolation_level=None)) as reader:
        reader.execute("CREATE TABLE facts (identity TEXT, value TEXT)")
        reader.execute("INSERT INTO facts VALUES ('current','before')")
        statements: list[str] = []
        reader.set_trace_callback(statements.append)
        sql = "SELECT value FROM facts WHERE identity=?"
        assert bounded_query(reader, sql, ("current",)).fetchall() == [("before",)]
        first = len(statements)
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE facts SET value='after'")
            writer.commit()
        assert bounded_query(reader, sql, ("current",)).fetchall() == [("after",)]
        # Only the bounded result is executed again; no second shape-only SELECT.
        assert first == 2 and len(statements) - first == 1
        assert bounded_query(reader, sql, ("missing",)).fetchall() == []


@pytest.mark.parametrize("sql", ["SELECT * FROM facts", "SELECT facts.* FROM facts",
                                 "WITH current AS (SELECT * FROM facts) SELECT * FROM current"])
def test_wildcard_result_shape_follows_actual_schema(sql: str) -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        connection.execute("CREATE TABLE facts (value INTEGER)")
        connection.execute("INSERT INTO facts VALUES (1)")
        assert bounded_query(connection, sql).fetchall() == [(1,)]
        connection.execute("ALTER TABLE facts ADD COLUMN extra TEXT DEFAULT 'new'")
        assert bounded_query(connection, sql).fetchall() == [(1, "new")]


def test_fixed_expression_shape_keeps_sqlite_types_and_binding_checks() -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        sql = "SELECT ?,?,?"
        assert bounded_query(connection, sql, (1, "中文", b"bytes")).fetchone() == (1, "中文", b"bytes")
        assert bounded_query(connection, sql, (None, 1.25, -3)).fetchone() == (None, 1.25, -3)
        with pytest.raises(sqlite3.ProgrammingError, match="bindings"):
            bounded_query(connection, sql, (1,)).fetchall()


def test_count_expression_reuses_its_scalar_shape_across_schema_changes() -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        connection.execute("CREATE TABLE facts (value INTEGER)")
        assert bounded_query(connection, "SELECT COUNT(*) FROM facts").fetchone() == (0,)
        connection.execute("ALTER TABLE facts ADD COLUMN extra TEXT")
        connection.execute("INSERT INTO facts VALUES (1,'new')")
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        assert bounded_query(connection, "SELECT COUNT(*) FROM facts").fetchone() == (1,)
        assert len(statements) == 1


def test_shape_reuse_does_not_bypass_sqlite_authorization_or_missing_columns() -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        connection.execute("CREATE TABLE facts (value INTEGER)")
        connection.execute("INSERT INTO facts VALUES (1)")
        sql = "SELECT value FROM facts"
        assert bounded_query(connection, sql).fetchone() == (1,)

        def deny_read(action, first, second, database, source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK

        connection.set_authorizer(deny_read)
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            bounded_query(connection, sql).fetchone()
        connection.set_authorizer(None)
        connection.execute("ALTER TABLE facts RENAME COLUMN value TO changed")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            bounded_query(connection, sql).fetchone()


def test_shape_reuse_does_not_reuse_or_extend_a_query_budget() -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        with query_scope(max_bytes=17):
            assert bounded_query(connection, "SELECT ?", ("x",)).fetchone() == ("x",)
            with pytest.raises(QueryLimitError, match="byte read limit"):
                bounded_query(connection, "SELECT ?", ("y",)).fetchone()
        with query_scope(max_bytes=17):
            assert bounded_query(connection, "SELECT ?", ("z",)).fetchone() == ("z",)


def test_cached_shape_still_rejects_oversized_body_before_transfer() -> None:
    with closing(sqlite3.connect(":memory:", factory=QueryConnection)) as connection:
        connection.execute("CREATE TABLE facts (value BLOB)")
        connection.execute("INSERT INTO facts VALUES (X'00')")
        assert bounded_query(connection, "SELECT value FROM facts").fetchone() == (b"\0",)
        connection.execute("UPDATE facts SET value=zeroblob(?)", (MAX_CURRENT_BYTES + 1,))
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="record exceeds"):
                bounded_query(connection, "SELECT value FROM facts").fetchone()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024
        assert connection.execute("SELECT length(value) FROM facts").fetchone() == (MAX_CURRENT_BYTES + 1,)


def test_eviction_and_close_leave_no_old_facts_or_live_queries() -> None:
    connection = sqlite3.connect(":memory:", factory=QueryConnection)
    try:
        for value in range(300):
            assert bounded_query(connection, f"SELECT ?+{value}", (1,)).fetchone() == (value + 1,)
        assert bounded_query(connection, "SELECT ?+0", (7,)).fetchone() == (7,)
    finally:
        connection.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        bounded_query(connection, "SELECT ?+0", (8,)).fetchone()


@pytest.mark.parametrize("parameters", [(0, 1), (-1, 100), (-(2 ** 63), 2 ** 63 - 1)])
def test_integer_aggregate_keeps_identical_row_and_byte_charges(parameters: tuple[int, int]) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = "SELECT ?+0,?+0"
        with query_scope() as original:
            expected = bounded_query(connection, sql, parameters).fetchone()
        with query_scope() as direct:
            assert integer_aggregate(connection, sql, parameters) == expected
            assert (direct.read_bytes, direct.rows) == (original.read_bytes, original.rows)
            with query_scope(max_bytes=direct.read_bytes):
                with pytest.raises(QueryLimitError, match="byte read limit"):
                    integer_aggregate(connection, sql, parameters)


def test_integer_aggregate_transfers_only_fixed_metadata_and_checks_result_type() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE facts (value BLOB)")
        connection.execute("INSERT INTO facts VALUES (zeroblob(?))", (MAX_CURRENT_BYTES + 1,))
        tracemalloc.start()
        try:
            assert integer_aggregate(connection, "SELECT COUNT(*),SUM(LENGTH(value)) FROM facts") == (1, MAX_CURRENT_BYTES + 1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024
        assert integer_aggregate(connection, "SELECT LENGTH(value) FROM facts WHERE 0") is None
        with pytest.raises(ValueError, match="only integers"):
            integer_aggregate(connection, "SELECT 'invalid aggregate'")
        with pytest.raises(ValueError, match="record exceeds"):
            bounded_query(connection, "SELECT value FROM facts").fetchone()
