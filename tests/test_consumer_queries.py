"""查询与执行入口保留真实事实，历史不成为保存时的额外读取。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from agent_run.change_delivery_threads import require_development_thread
from agent_run.query_report import ExecutionReport
from agent_run.executor import DeliveryExecutor
from agent_run.object_state import object_store
from agent_run.state import StateStore
from agent_run.work_projection import JobProjection
from support.ticket_parallel import ticket_run


@pytest.mark.parametrize("different", [False, True])
def test_execution_totals_match_exact_bodies_with_duplicate_invocation_identity(different: bool) -> None:
    invocation = {
        "work_subject": "ticket:2", "started_at": "2026-01-01T00:00:00+00:00",
        "reported_thread_id": "thread-1", "status": "completed",
        "execution_segments": [{"started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:00:09+00:00"}],
    }
    observed = deepcopy(invocation)
    if different:
        observed["reported_thread_id"] = "different-thread"
    state = {"agent_invocation_history": [invocation, observed]}
    records = [{"attempt_id": "a-1", "invocations": [deepcopy(invocation)], "activity": "not_running"}]
    totals = ExecutionReport(state, records).totals(None)
    assert totals["execution_timing_complete"] is not different
    assert totals["recorded_execution_seconds"] == (18 if different else 9)
    assert ("total_seconds" in totals) is not different


def test_unloaded_thread_history_still_rejects_reusing_a_reviewer() -> None:
    job = JobProjection({"development_thread_id": None}, job_id="ticket:2:generation:1", revision=1,
                        archives={"reviewer_thread_ids": (15, 1), "development_thread_history": (2, 0)})
    job.history_reader = lambda _owner, fields: {
        field: ["old-reviewer"] if field == "reviewer_thread_ids" else [] for field in fields
    }
    with pytest.raises(ValueError, match="Reviewer Thread"):
        require_development_thread(job, "old-reviewer", requested_thread=None)


def test_narrow_identity_query_enforces_actual_utf8_bytes_and_shared_budget() -> None:
    from agent_run.sqlite_query_rows import bounded_rows
    from agent_run.state_limits import MAX_CURRENT_BYTES, QueryLimitError, query_scope

    with sqlite3.connect(":memory:") as c:
        c.execute("CREATE TABLE identities(value TEXT)")
        c.execute("INSERT INTO identities VALUES (?)", ("original-subject",))
        assert list(bounded_rows(c, "SELECT value FROM identities", (), ("value",))) == [("original-subject",)]
        with query_scope(max_bytes=16), pytest.raises(QueryLimitError, match="byte read limit"):
            list(bounded_rows(c, "SELECT value FROM identities", (), ("value",)))
        # Character length fits; actual UTF-8 bytes do not. SQL replaces the
        # oversized body with NULL, then the reader rejects its measured size.
        c.execute("UPDATE identities SET value=?", ("中" * (MAX_CURRENT_BYTES // 2),))
        with pytest.raises(ValueError, match="narrow query record exceeds byte limit"):
            list(bounded_rows(c, "SELECT value FROM identities", (), ("value",)))


@pytest.mark.parametrize("container", ["active_ticket_job", "run_acceptance"])
def test_current_status_preserves_latest_reviewer_thread_without_loading_its_archive(
    tmp_path: Path, container: str,
) -> None:
    from agent_run.cli_presentation import _current_worker
    from agent_run.sqlite_state import SQLiteStateRepository

    state = {"run_id": "run-1", "repository": "example/project", "language": "zh", "status": "active",
             "parent": {"number": 1}, container: {"phase": "reviewing", "ticket_number": 2,
                                                    "reviewer_thread_ids": ["first-reviewer", "latest-reviewer"]}}
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", state)
    queried = repository.load_status("run-1", include_timing=False)
    assert "reviewer_thread_ids" not in queried[container]
    assert _current_worker(queried) == _current_worker(repository.load_history("run-1"))
    assert _current_worker(queried)["thread_id"] == "latest-reviewer"


@pytest.mark.parametrize("enabled", [False, True])
def test_executor_saves_without_full_history_and_notification_failure_cannot_rollback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    from support.state import persist_run_fixture

    initial, states, _git, _fixture = ticket_run(git_repo, (2,))
    run_id = initial["run_id"]
    initial["notifications"] = {"enabled": enabled, "mode": "concise"}
    persist_run_fixture(states, run_id, initial)
    reads = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Executor expanded the complete Run")

    def notification(*_args, **_kwargs):
        reads.append("notification")
        raise OSError("notification source unavailable")

    monkeypatch.setattr(states, "load_current_run", forbidden)
    monkeypatch.setattr(states, "load_run", forbidden)
    monkeypatch.setattr(states, "load_notification_run", notification)
    scoped = object_store(states)

    def advance(current):
        assert states.run_saved_observer is not None if enabled else states.run_saved_observer is None
        current["updated_at"] = "2026-01-02T00:00:00+00:00"
        scoped.save_run(run_id, current)
        return current

    result = DeliveryExecutor(states=states, driver_factory=lambda *_: SimpleNamespace(advance=advance)).execute(run_id)
    assert result["updated_at"] == "2026-01-02T00:00:00+00:00"
    assert StateStore(states.root).load_current_run(run_id)["updated_at"] == result["updated_at"]
    assert bool(reads) is enabled
    assert states.run_saved_observer is None


def test_status_keeps_exact_totals_without_reading_a_damaged_unrelated_archive(git_repo: Path) -> None:
    from agent_run.state_errors import MissingRunMaterialsError
    from agent_run.status_actions import status_timing
    from support.state import persist_run_fixture
    from support.ticket_parallel import TicketAgents, ticket_engine

    initial, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = initial["run_id"]
    ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None)).deliver_ticket(run_id, 2)
    complete = states.load_current_run(run_id)
    complete["ticket_jobs"]["2"]["human_blocker_history"] = [{"phase": "developing", "human_blockers": ["Archived original evidence"]}]
    complete["active_ticket_job"] = complete["ticket_jobs"]["2"]
    persist_run_fixture(states, run_id, complete)
    expected = status_timing(states.load_current_run(run_id))
    with sqlite3.connect(states._repository.path) as c:
        material = c.execute("SELECT material_id FROM work_evidence WHERE run_id=? AND kind='human_blocker_history'", (run_id,)).fetchone()[0]
        c.execute("UPDATE work_evidence_materials SET body='damaged archive' WHERE material_id=?", (material,))
    before = states._repository.path.read_bytes()
    current = states.load_status_run(run_id)
    assert current["ticket_jobs"]["2"]["phase"] == "completed"
    assert "human_blocker_history" not in current["ticket_jobs"]["2"]
    assert status_timing(current) == expected
    with pytest.raises(MissingRunMaterialsError):
        states.load_history_run(run_id)
    assert states._repository.path.read_bytes() == before
