"""原始返回及消费字段共用准确证据；重开后仍核验拥有者与实际正文。"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_work_evidence import (
    TABLES, MAX_EVIDENCE_BYTES, evidence_size, get_evidence, put_evidence, put_evidence_member,
)


def _create(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO runs VALUES ('run-1')")
    for ddl in TABLES.values():
        connection.execute(ddl)
    return connection


def test_consumed_member_references_saved_return_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    original = {"kind": "development", "summary": "原始结果\x00保持" * 200}
    with closing(_create(path)) as connection, connection:
        returned = put_evidence(connection, "run-1", "attempt:one", "returned_result", original)
        consumed = put_evidence_member(connection, "run-1", "job:one", "development_summary", returned, ("summary",))
        assert consumed == put_evidence_member(
            connection, "run-1", "job:one", "development_summary", returned, ("summary",),
        )
        assert connection.execute("SELECT COUNT(*) FROM work_evidence_materials").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM work_evidence").fetchone() == (2,)
    with closing(sqlite3.connect(path)) as connection:
        assert get_evidence(connection, "run-1", consumed, owner_id="job:one", kind="development_summary") == original["summary"]
        assert evidence_size(connection, "run-1", consumed) == len(('"' + original["summary"].replace("\x00", "\\u0000") + '"').encode())
        with pytest.raises(ValueError, match="owner"):
            get_evidence(connection, "run-1", returned, owner_id="attempt:another")


def test_evidence_versions_preserve_exact_prior_result(tmp_path: Path) -> None:
    with closing(_create(tmp_path / "state.sqlite3")) as connection, connection:
        first = put_evidence(connection, "run-1", "attempt:one", "review_context", {"candidate": "old"})
        second = put_evidence(connection, "run-1", "attempt:one", "review_context", {"candidate": "new"})
        assert first != second
        assert get_evidence(connection, "run-1", first) == {"candidate": "old"}
        assert get_evidence(connection, "run-1", second) == {"candidate": "new"}
        assert connection.execute("SELECT version FROM work_evidence ORDER BY version").fetchall() == [(1,), (2,)]


def test_publication_narrative_reuses_original_wire_return(tmp_path: Path) -> None:
    narrative = {"commit_message": "commit", "pr_title": "title", "pr_body_markdown": "body"}
    with closing(_create(tmp_path / "state.sqlite3")) as connection, connection:
        original = {"kind": "publication", "thread_id": "thread", "artifact": {
            "result_kind": "publication", "human_blockers": None, **narrative,
        }}
        returned = put_evidence(connection, "run-1", "attempt:publication", "returned_result", original)
        consumed = put_evidence_member(connection, "run-1", "job:one", "publication", returned, ("publication_fields",))
        assert get_evidence(connection, "run-1", consumed) == narrative
        assert connection.execute("SELECT COUNT(*) FROM work_evidence_materials").fetchone() == (1,)


def test_damaged_or_oversized_evidence_is_rejected_before_body_read(tmp_path: Path) -> None:
    with closing(_create(tmp_path / "state.sqlite3")) as connection, connection:
        reference = put_evidence(connection, "run-1", "job:one", "review_context", {"body": "原文"})
        connection.execute("UPDATE work_evidence_materials SET body=zeroblob(?)", (MAX_EVIDENCE_BYTES + 1,))
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match="byte length"):
            get_evidence(connection, "run-1", reference)
        assert not any(statement.startswith("SELECT body FROM") for statement in statements)
        connection.set_trace_callback(None)
        with pytest.raises(ValueError, match="exceeds"):
            put_evidence(connection, "run-1", "job:one", "review_context", "x" * MAX_EVIDENCE_BYTES)
