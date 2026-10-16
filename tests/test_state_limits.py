"""分层额度不依赖整份 Run 的旧展开大小，也不丢证据或续期业务预算。"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tracemalloc

import pytest

from agent_run import state_limits as limits
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_work_evidence import get_evidence, put_evidence, put_human_response_member


def test_nested_query_work_keeps_one_budget_and_failure_does_not_poison_next_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_QUERY_ROWS", 3)
    with limits.query_scope(max_bytes=12):
        limits.account_batch([4, 4])
        with limits.query_scope(max_bytes=12):
            limits.account_batch([4])
            with pytest.raises(limits.QueryLimitError, match="byte read limit"):
                limits.consume_query_bytes(1)
            with pytest.raises(limits.QueryLimitError, match="row limit"):
                limits.consume_query_rows()
    with limits.query_scope(max_bytes=12):
        limits.account_batch([12])


def test_query_batch_and_response_have_independent_utf8_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_BATCH_BYTES", 8)
    monkeypatch.setattr(limits, "QUERY_BATCH_ROWS", 2)
    with limits.query_scope(max_bytes=100):
        limits.account_batch([4, 4])
        with pytest.raises(limits.QueryLimitError, match="batch"):
            limits.account_batch([9])
        with pytest.raises(limits.QueryLimitError, match="batch"):
            limits.account_batch([1, 1, 1])
    # Literal UTF-8 response: braces 2, key/colon 10, value quotes 2, CJK 6.
    assert limits.check_response({"summary": "中文"}, max_bytes=20) == 20
    with pytest.raises(limits.QueryLimitError, match="response"):
        limits.check_response({"summary": "中文"}, max_bytes=19)


def test_current_columns_are_bounded_before_transfer(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "row.sqlite3")) as connection:
        connection.execute("CREATE TABLE records (identity TEXT PRIMARY KEY, body BLOB)")
        connection.execute("INSERT INTO records VALUES ('exact',zeroblob(1048576))")
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="current record exceeds"):
                limits.require_record_size(connection, "records", ("identity", "body"),
                                           "identity=?", ("exact",))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024
        assert connection.execute("SELECT length(body) FROM records").fetchone() == (1048576,)


@pytest.mark.parametrize("domain", ["resume", "query"])
def test_audit_current_metadata_is_bounded_before_transfer(domain: str) -> None:
    from agent_run import sqlite_run_audit, sqlite_run_resume

    with closing(sqlite3.connect(":memory:")) as connection:
        if domain == "resume":
            connection.execute("CREATE TABLE run_resume_roots (run_id TEXT, total INTEGER, rolling_digest TEXT)")
            connection.execute("INSERT INTO run_resume_roots VALUES ('exact',0,?)",
                               ("x" * (limits.MAX_CURRENT_BYTES + 1),))
            read = lambda: sqlite_run_resume.load(connection, "exact", attempt_ids=set(), include_responses=False)
        else:
            connection.execute("CREATE TABLE run_query_roots (run_id TEXT, presence BLOB, timeline_at_capacity INTEGER)")
            connection.execute("INSERT INTO run_query_roots VALUES ('exact',?,NULL)",
                               (b"x" * (limits.MAX_CURRENT_BYTES + 1),))
            read = lambda: sqlite_run_audit.load(connection, "exact", fields=set())
        tracemalloc.start()
        try:
            with limits.query_scope(max_bytes=1024), pytest.raises(ValueError, match="current record exceeds"):
                read()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024


def test_physical_capacity_admits_boundary_then_refuses_without_reclaiming_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "capacity.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE records (body TEXT)")
        connection.execute("INSERT INTO records VALUES ('necessary evidence')")
        connection.commit()
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        boundary = sum(limits.storage_usage(path).values()) + pages * (page_size + 24) + 32
        monkeypatch.setattr(limits, "MAX_STORAGE_BYTES", boundary)
        limits.check_storage(path, connection)
        monkeypatch.setattr(limits, "MAX_STORAGE_BYTES", boundary - 1)
        with pytest.raises(limits.StorageCapacityError, match="commit reserve"):
            limits.check_storage(path, connection)
        assert connection.execute("SELECT body FROM records").fetchone() == ("necessary evidence",)


def test_capacity_counts_pinned_wal_and_preserves_both_reader_and_writer_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "wal.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE records (body TEXT)")
        writer.execute("INSERT INTO records VALUES ('old')")
        with closing(sqlite3.connect(path, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT body FROM records").fetchone() == ("old",)
            writer.execute("UPDATE records SET body=?", ("new" * 4096,))
            usage = limits.storage_usage(path)
            assert usage["wal"] > 0
            monkeypatch.setattr(limits, "MAX_STORAGE_BYTES", usage["database"])
            with pytest.raises(limits.StorageCapacityError):
                limits.check_storage(path, writer)
            assert reader.execute("SELECT body FROM records").fetchone() == ("old",)
            assert writer.execute("SELECT body FROM records").fetchone() == ("new" * 4096,)
            reader.rollback()


def test_original_human_response_has_one_body_and_exact_owned_member(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES)
    repository.save("run-1", {})
    with repository.atomic():
        connection = repository._local.connection
        source = put_evidence(connection, "run-1", "job:ticket:2:generation:1",
                              "human_response_history", {"generation": 1, "response": "请保留原成果"})
        target = put_human_response_member(connection, "run-1", "run:run-1:resume:r1", source,
                                           source_owner_id="job:ticket:2:generation:1")
        assert get_evidence(connection, "run-1", target, owner_id="run:run-1:resume:r1",
                            kind="human_response") == "请保留原成果"
        assert connection.execute("SELECT COUNT(*) FROM work_evidence_materials").fetchone() == (1,)
        with pytest.raises(ValueError, match="another Job"):
            put_human_response_member(connection, "run-1", "run:run-1:resume:r1", source,
                                      source_owner_id="job:ticket:3:generation:1")


def test_evidence_headers_are_bounded_before_transfer(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES)
    repository.save("run-1", {})
    with repository.atomic():
        connection = repository._local.connection
        identity = put_evidence(connection, "run-1", "job:exact", "report", "retained")
        connection.execute("UPDATE work_evidence SET owner_id=? WHERE evidence_id=?",
                           ("x" * (limits.MAX_CURRENT_BYTES + 1), identity))
        tracemalloc.start()
        try:
            with limits.query_scope(), pytest.raises(ValueError, match="query record exceeds"):
                get_evidence(connection, "run-1", identity)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024


@pytest.mark.parametrize("deduplicate", [False, True])
def test_evidence_body_and_headers_share_query_budget_once(tmp_path: Path, deduplicate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES)
    repository.save("run-1", {})
    value = "retained" * 1024
    size = len(value.encode()) + 2
    with repository.atomic():
        connection = repository._local.connection
        identity = put_evidence(connection, "run-1", "job:exact", "report", value)

        def read() -> object:
            if deduplicate:
                return put_evidence(connection, "run-1", "job:exact", "report", value)
            return get_evidence(connection, "run-1", identity)

        with limits.query_scope(max_bytes=1024), pytest.raises(limits.QueryLimitError, match="read limit"):
            read()
        with limits.query_scope(max_bytes=size + 1024) as budget:
            assert read() == (identity if deduplicate else value)
            assert size < budget.read_bytes < size + 1024
            assert budget.rows >= 3


def test_separate_materials_can_exceed_old_total_limit_and_query_failure_preserves_them(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES)
    body = "原文" * 380_000
    original = {"status": "active", "parent": {"number": 1, "title": "需求", "body": body},
                "prompt_resources": {"text": body}, "parent_job": {"modification_attempts": 2}}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    restricted = SQLiteStateRepository(tmp_path, max_bytes=1024)
    with pytest.raises(ValueError, match="query.*limit"):
        restricted.load("run-1")
    assert repository.load("run-1") == original
    assert restricted.load_fields("run-1", {"status"}) == {"status": "active"}


def test_database_page_capacity_failure_rolls_back_current_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES)
    original = {"status": "active", "parent_job": {"modification_attempts": 2}, "prompt_resources": {"text": "old"}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
    monkeypatch.setattr(limits, "MAX_DATABASE_BYTES", pages * page_size)
    with pytest.raises(OSError, match="(full|capacity)"):
        repository.save_objects("run-1", {"status": "paused", "prompt_resources": {"text": "X" * 1_000_000}},
                                fields={"status", "prompt_resources"})
    assert SQLiteStateRepository(tmp_path, max_bytes=limits.MAX_QUERY_BYTES).load("run-1") == original


@pytest.mark.parametrize("lookup", [False, True])
def test_run_identity_queries_share_byte_budget_and_leave_subsequent_query_healthy(
    tmp_path: Path, lookup: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    run_id = "run-" + "x" * 256
    repository.save(run_id, {"repository": "example/project", "parent": {"number": 1}})

    def read() -> list[str]:
        return (repository.find_ids("example/project", 1, unfinished_only=False)
                if lookup else repository.list_ids())

    with limits.query_scope(max_bytes=128), pytest.raises(limits.QueryLimitError, match="read limit"):
        read()
    with limits.query_scope(max_bytes=4096):
        assert read() == [run_id]
