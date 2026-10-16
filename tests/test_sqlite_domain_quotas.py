"""明确领域当前记录完整准入与真实查询传输额度。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tracemalloc

import pytest

from agent_run import sqlite_change_job_codec as codec
from agent_run import sqlite_change_job_gates as gates
from agent_run.sqlite_query_rows import bounded_query, bounded_rows
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_limits import MAX_CURRENT_BYTES, QueryLimitError, query_scope


def _repository(root: Path) -> SQLiteStateRepository:
    repository = SQLiteStateRepository(root)
    repository.save("run-1", {"status": "active", "parent_job": {
        "phase": "developing", "parent_branch": "x" * 160_000,
        "human_blocker_history": [{"question": "retained evidence"}],
    }})
    return repository


def test_partial_current_job_growth_checks_complete_record_and_rolls_back(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = repository.load("run-1")
    assert original is not None
    changed = deepcopy(original)
    changed["parent_job"]["repair_checkout"] = "y" * 160_000
    with pytest.raises(ValueError, match="current record exceeds"):
        repository.save_objects("run-1", changed, fields={"parent_job"})
    assert repository.load("run-1") == original


@pytest.mark.parametrize("field", ["integration_conflict_paths", "integrated_parents"])
def test_job_string_lists_join_complete_current_record_limit(tmp_path: Path, field: str) -> None:
    repository = _repository(tmp_path)
    original = repository.load("run-1")
    assert original is not None
    changed = deepcopy(original)
    changed["parent_job"][field] = ["y" * 160_000]
    with pytest.raises(ValueError, match="current record exceeds"):
        repository.save_objects("run-1", changed, fields={"parent_job"})
    assert repository.load("run-1") == original
    # A single list member cannot bypass the same rule in the initial insert.
    fresh = SQLiteStateRepository(tmp_path / "oversized")
    with pytest.raises(ValueError, match="current record exceeds"):
        fresh.save("run-2", {"parent_job": {field: ["z" * 300_000]}})
    assert fresh.load("run-2") is None


@pytest.mark.parametrize("damage", ["core", "strings"])
def test_stored_job_size_is_checked_before_selecting_even_narrow_fields(tmp_path: Path, damage: str) -> None:
    repository = _repository(tmp_path)
    with repository.atomic():
        c = repository._local.connection
        job_id = c.execute("SELECT job_id FROM work_job_slots WHERE container='parent_job'").fetchone()[0]
        if damage == "core":
            c.execute("UPDATE work_jobs SET value_repair_checkout=? WHERE job_id=?", ("y" * 160_000, job_id))
        else:
            c.execute("INSERT INTO work_job_record_strings VALUES (?,?,?,?,?,?)",
                      ("run-1", job_id, "job", "integration_conflict_paths", 0, "y" * 300_000))
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="current record exceeds"):
                codec.load_core(c, "run-1", job_id, fields={"phase"})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024


def test_selected_root_columns_share_budget_without_transferring_unrequested_text(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"status": "active", "run_branch": "x" * 200_000})
    restricted = SQLiteStateRepository(tmp_path, max_bytes=1024)
    with query_scope(max_bytes=1024) as budget:
        assert restricted.load_fields("run-1", {"status"}) == {"status": "active"}
        assert 0 < budget.read_bytes < 1024
    with query_scope(max_bytes=1024), pytest.raises(QueryLimitError, match="byte read limit"):
        restricted.load_fields("run-1", {"run_branch"})
    assert repository.load_fields("run-1", {"run_branch"}) == {"run_branch": "x" * 200_000}


def test_each_transferred_row_consumes_one_shared_budget_and_failure_keeps_rows() -> None:
    with closing(sqlite3.connect(":memory:")) as c:
        c.execute("CREATE TABLE current_strings (body TEXT)")
        c.executemany("INSERT INTO current_strings VALUES (?)", [("a" * 100,), ("b" * 100,)])
        with query_scope(max_bytes=232) as budget:
            assert bounded_query(c, "SELECT body FROM current_strings").fetchall() == [("a" * 100,), ("b" * 100,)]
            assert budget.read_bytes == 232
            assert budget.rows == 2
            with query_scope(max_bytes=232), pytest.raises(QueryLimitError, match="byte read limit"):
                bounded_query(c, "SELECT body FROM current_strings LIMIT 1").fetchone()
        with query_scope(max_bytes=116):
            assert bounded_query(c, "SELECT body FROM current_strings LIMIT 1").fetchone() == ("a" * 100,)
        assert c.execute("SELECT COUNT(*) FROM current_strings").fetchone() == (2,)


def test_warm_projection_checks_new_data_and_current_limit_before_body_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import sqlite_query_rows

    with closing(sqlite3.connect(":memory:")) as c:
        c.execute("CREATE TABLE current_strings (body TEXT)")
        c.execute("INSERT INTO current_strings VALUES ('small')")
        assert bounded_query(c, "SELECT body FROM current_strings").fetchone() == ("small",)
        c.execute("UPDATE current_strings SET body=?", ("x" * 64,))
        monkeypatch.setattr(sqlite_query_rows, "MAX_CURRENT_BYTES", 32)

        def reject_body(value: bytes) -> str:
            pytest.fail("oversized body crossed the SQLite boundary")

        c.text_factory = reject_body
        with pytest.raises(ValueError, match="record exceeds byte limit"):
            bounded_query(c, "SELECT body FROM current_strings").fetchone()
        assert c.execute("SELECT LENGTH(body) FROM current_strings").fetchone() == (64,)


def test_partial_bounded_query_cleanup_allows_already_closed_connection() -> None:
    c = sqlite3.connect(":memory:")
    rows = bounded_rows(c, "SELECT 1 AS identity UNION ALL SELECT 2", (), ("identity",))
    assert next(rows) == (1,)
    c.close()
    rows.close()


def test_bounded_query_streams_before_evaluating_later_archive_rows() -> None:
    observed: list[int] = []
    with closing(sqlite3.connect(":memory:")) as c:
        def observe(value: int) -> int:
            observed.append(value)
            return value

        c.create_function("observe", 1, observe)
        rows = bounded_rows(c, "WITH RECURSIVE archive(value) AS (SELECT 1 UNION ALL "
                            "SELECT value+1 FROM archive WHERE value<1000) "
                            "SELECT observe(value) AS identity FROM archive", (), ("identity",))
        assert next(rows) == (1,)
        # SQLite may prepare the next row and evaluate the identity for both
        # its value and length, but it must not materialize the archive.
        assert len(observed) <= 4
        rows.close()


@pytest.mark.parametrize("job", [
    {"parent_branch": "x" * 20_000},
    {"integration_conflict_paths": ["x" * 20_000]},
    {"candidate_commit_intent": {"expected_head": "x" * 20_000}},
    {"integration_resync": {"history": [{"reason": "x" * 20_000}]}},
    {"review_budget": {"review_artifacts": [{"reviewer_thread_id": "x" * 20_000}]}},
], ids=["core", "strings", "record", "budget-events", "reviewer-reference"])
def test_current_job_domains_charge_actual_selected_text_once(tmp_path: Path, job: dict[str, object]) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"parent_job": job})
    original = repository.load("run-1")
    with query_scope(max_bytes=10_000), pytest.raises(QueryLimitError, match="byte read limit"):
        repository.load_fields("run-1", {"parent_job"})
    # A separate query gets its own allowance; a failed query changes no fact.
    assert repository.load("run-1") == original


@pytest.mark.parametrize("collection", ["events", "artifacts"])
def test_budget_current_record_includes_events_and_reviewer_references(tmp_path: Path, collection: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"parent_job": {"integration_resync": {"history": []},
                                           "review_budget": {"checkpoint_reason": "retained"}}})
    original = repository.load("run-1")
    assert original is not None
    changed = deepcopy(original)
    if collection == "events":
        changed["parent_job"]["integration_resync"] = {"history": [{"reason": "x" * 140_000}, {"reason": "y" * 140_000}]}
    else:
        changed["parent_job"]["review_budget"]["review_artifacts"] = [{"reviewer_thread_id": "x" * 140_000},
                                                                      {"reviewer_thread_id": "y" * 140_000}]
    with pytest.raises(ValueError, match="record exceeds byte limit"):
        repository.save_objects("run-1", changed, fields={"parent_job"})
    assert repository.load("run-1") == original


def test_gate_current_record_is_bounded_before_reason_transfer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with repository.atomic():
        c = repository._local.connection
        job_id = c.execute("SELECT job_id FROM work_job_slots WHERE container='parent_job'").fetchone()[0]
        c.execute("INSERT INTO work_job_gates VALUES (?,?,?,?,?,?,?,?,?)",
                  ("run-1", job_id, "exact", "condition", "x" * (MAX_CURRENT_BYTES + 1), "blocked", None, None, 15))
        with pytest.raises(ValueError, match="current collection exceeds"):
            gates.load_gate(c, "run-1", job_id, {}, "developing")
