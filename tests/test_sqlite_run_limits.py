"""Run 当前记录超限时保留既有事实，不截断、不部分提交。"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("field", ["frontier", "ticket_pause_requests"])
def test_run_scheduling_boundary_roundtrips_and_overflow_preserves_current(
    tmp_path: Path, field: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    numbers = list(range(1, 4097))
    value = numbers if field == "frontier" else {str(number): "paused" for number in numbers}
    original = {"status": "active", field: value}
    repository.save("run-1", original)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == original
    overflow = [*numbers, 4097] if field == "frontier" else {**value, "4097": "paused"}
    with pytest.raises(ValueError, match="oversized Run scheduling"):
        reopened.save_objects("run-1", {"status": "paused", field: overflow}, fields={"status", field})
    assert reopened.load("run-1") == original


def test_oversized_persisted_queue_fails_instead_of_truncating(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    repository.save("run-1", {"frontier": list(range(1, 4097))})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute("INSERT INTO run_frontier VALUES ('run-1',4096,4097)")
    with pytest.raises(ValueError, match="scheduling record exceeds"):
        repository.load_fields("run-1", {"frontier"})
    with closing(sqlite3.connect(repository.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_frontier").fetchone() == (4097,)


def test_run_diagnostic_boundary_and_overflow_keep_original_gate(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    diagnostics = [{"code": f"fault-{number}", "message": "original report"} for number in range(32)]
    original = {"status": "execution_failed", "diagnostics": diagnostics}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    with pytest.raises(ValueError, match="oversized Run diagnostics"):
        repository.save_objects("run-1", {"status": "active", "diagnostics": [*diagnostics, {"code": "overflow"}]},
                                fields={"status", "diagnostics"})
    assert SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1") == original


def test_closeout_record_overflow_keeps_pending_intent_and_error(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    cleanup = {"status": "cleanup_pending", "last_error": "dirty checkout", "items": {}}
    original = {"status": "completed", "delivery_cleanup": cleanup}
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="closeout current record exceeds"):
        repository.save_objects("run-1", {"delivery_cleanup": {**cleanup, "last_error": "X" * (256 * 1024)}},
                                fields={"delivery_cleanup"})
    assert SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1") == original


def test_conflict_paths_share_the_current_record_limit_on_save_and_reopen(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    original = {"status": "run_acceptance_pending", "run_acceptance": {"phase": "pending",
                "run_integration": {"candidate_sha": "candidate", "conflict_paths": ["normal.py"]}}}
    repository.save("run-1", original)
    paths = [f"conflicts/{'x' * 72}/file-{number}.py" for number in range(4096)]
    with pytest.raises(ValueError, match="record.*(exceeds|byte limit)"):
        repository.save_objects("run-1", {"run_acceptance": {"phase": "pending", "run_integration": {
            "candidate_sha": "candidate", "conflict_paths": paths}}}, fields={"run_acceptance"})
    assert repository.load("run-1") == original
    # Stored damage must also fail on read, rather than loading a truncated
    # path list or trusting only the fixed columns' small size.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        job_id = connection.execute("SELECT job_id FROM work_jobs WHERE owner_kind='run_acceptance'").fetchone()[0]
        connection.execute("DELETE FROM work_job_record_strings WHERE record_kind='run_integration'")
        connection.executemany("INSERT INTO work_job_record_strings VALUES (?,?,?,?,?,?)",
            (("run-1", job_id, "run_integration", "conflict_paths", position, path) for position, path in enumerate(paths)))
    with pytest.raises(ValueError, match="record.*(exceeds|byte limit)"):
        SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1")


@pytest.mark.parametrize("field", ["base", "delivery_cleanup"])
def test_root_record_checks_complete_stored_size_before_committing(tmp_path: Path, field: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    value = {"branch": "main", "sha": "base"} if field == "base" else {
        "status": "cleanup_pending", "last_error": "dirty checkout", "items": {}}
    original = {"status": "active", field: value}
    repository.save("run-1", original)
    proposed = {**value, "branch" if field == "base" else "last_error": "X" * 262080}
    with pytest.raises(ValueError, match="current record exceeds"):
        repository.save_objects("run-1", {field: proposed}, fields={field})
    assert SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1") == original
