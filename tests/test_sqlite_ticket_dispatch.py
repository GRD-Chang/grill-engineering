"""调度只读事实与完整查询一致，保存和执行准入仍独立重读。"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.semantic_attempt import allocate_semantic_attempt
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_watcher import SQLiteWatcher
from agent_run.state import StateStore
from agent_run.state_limits import QueryLimitError, query_scope
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_pause import TicketStopRequested
from agent_run.ticket_work import TicketWorkAdmission, capacity, generation
from agent_run.work_facts import TicketDispatchJob, ticket_program_ready
from support.state import current_query_fixture


def _facts() -> dict[str, Any]:
    jobs: dict[str, Any] = {
        str(number): {"ticket_number": number, "ticket_branch_generation": 1, "phase": "developing",
                      "review_context": {"body": "Unrelated review context" * 100},
                      "human_blocker_history": [{"question": "Unrelated historical report"}]}
        for number in range(2, 7)
    }
    jobs["2"]["phase"] = "committing_candidate"
    attempt = allocate_semantic_attempt(jobs["3"], role="development", work_subject="ticket:3", generation=1,
                                        currentness_boundary={"candidate": "exact"}, ordinal=1)
    attempt["review_context"] = {"body": "Unrelated Attempt context" * 100}
    attempt["returned_result"] = {"kind": "development", "thread_id": "worker-3", "summary": "Prepared",
                                  "replaced_thread_id": None}
    jobs["4"]["pending_review_result"] = {"summary": "Ready for deterministic consumption"}
    jobs["5"]["external_wait"] = {
        "status": "waiting_external", "diagnostics": [{"code": "retry_later"}],
        "supervision_window": {"identity": "retained-wait", "kind": "github_convergence",
                               "phase": "developing", "started_at": 100.0, "deadline": 300.0,
                               "next_retry_at": 125.0},
    }
    jobs["6"].update(phase="blocked", blocked_reason="ticket_paused", pause_resume_phase="developing")
    return current_query_fixture({
        "run_id": "run-1", "repository": "example/project", "status": "active", "phase": "ticket_delivery",
        "parent": {"number": 1, "title": "Parent", "body": "Requirements", "revision": "parent-r1"},
        "base": {"branch": "main", "sha": "a" * 40},
        "ticket_graph": {"revision": "graph-r1", "tickets": {str(number): {"number": number}
                                                                 for number in range(2, 7)}},
        "accepted_parent_spec_revision": "parent-r1", "accepted_ticket_graph_revision": "graph-r1",
        "frontier": list(range(2, 7)), "ticket_jobs": jobs,
        "ticket_pause_requests": {}, "retired_ticket_generations": {}, "pending_ticket_retirements": {},
        "ticket_integration_holder": None, "ticket_integration_queue": [],
        "ticket_work_queue": [{"ticket_number": 5, "generation": 1, "role": "development", "lease_id": None}],
    })


@pytest.fixture
def dispatch_store(tmp_path: Path) -> StateStore:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    return states


def test_dispatch_matches_full_query_readiness_gates_fifo_and_seed_preview(
    dispatch_store: StateStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = dispatch_store
    complete = states._repository.load_work("run-1", None, thin_timeline=True)
    current = states.load_dispatch_run("run-1")
    assert complete is not None and current is not None
    for field in ("status", "frontier", "ticket_work_queue", "ticket_pause_requests",
                  "retired_ticket_generations", "ticket_integration_holder", "ticket_integration_queue"):
        assert current[field] == complete[field]
    assert capacity(current) == capacity(complete)
    for key, job in current["ticket_jobs"].items():
        original = complete["ticket_jobs"][key]
        assert isinstance(job, TicketDispatchJob)
        assert job.get("phase") == original.get("phase")
        assert generation(current, int(key)) == generation(complete, int(key))
        assert ticket_program_ready(job) is ticket_program_ready(original)
        assert not job.keys() & {"pending_semantic_attempt", "pending_review_result", "review_context",
                                 "human_blocker_history", "program_ready"}
    wait = current["ticket_jobs"]["5"]["external_wait"]
    assert wait["supervision_window"] == complete["ticket_jobs"]["5"]["external_wait"]["supervision_window"]
    assert "diagnostics" not in wait
    assert current["ticket_jobs"]["6"]["phase"] == "blocked"
    assert not current.keys() & {"agent_invocation_history", "active_agent_invocation", "ticket_agent_invocations"}

    def forbid_commit(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("An unchanged dispatch preview entered a write boundary")

    monkeypatch.setattr(states, "_commit_run", forbid_commit)
    assert TicketWorkAdmission(states, "run-1").seed([2, 3, 4, 5], current=current) is False


def test_dispatch_does_not_read_unselected_reports_invocations_or_job_history(dispatch_store: StateStore) -> None:
    repository = dispatch_store._repository
    with closing(sqlite3.connect(repository.path)) as writer:
        writer.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1 WHERE material_id IN "
                       "(SELECT material_id FROM work_evidence WHERE kind='review_context')")
        writer.commit()
    forbidden = {"work_invocations", "work_job_archives", "work_job_attempt_history", "run_timeline_events"}
    observed: set[str] = set()

    def authorize(action: int, table: str, _column: str, _database: str, _source: str) -> int:
        if action == sqlite3.SQLITE_READ:
            observed.add(table)
            if table in forbidden:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with SQLiteWatcher(repository.path) as watcher:
        watcher.version()
        assert watcher.connection is not None
        watcher.connection.set_authorizer(authorize)
        with repository.use_reader(watcher.connection):
            assert dispatch_store.load_dispatch_run("run-1") is not None
        assert not watcher.connection.in_transaction
    assert observed.isdisjoint(forbidden)
    with pytest.raises(ValueError, match="byte length"):
        repository.load_work("run-1", None)


@pytest.mark.parametrize("damage", ["generation", "phase", "attempt-owner", "attempt-generation", "return", "policy"])
def test_dispatch_rejects_damage_in_selected_facts(dispatch_store: StateStore, damage: str) -> None:
    statements = {
        "generation": "UPDATE work_jobs SET value_ticket_branch_generation='bad' WHERE owner_key='5'",
        "phase": "UPDATE work_jobs SET stage='unsupported' WHERE owner_key='5'",
        "attempt-owner": "UPDATE work_attempts SET job_id='ticket:5:generation:1'",
        "attempt-generation": "UPDATE work_attempts SET generation=2",
        "return": "UPDATE work_evidence_materials SET body=body||' ' WHERE material_id IN "
                  "(SELECT material_id FROM work_evidence WHERE kind='returned_result')",
        "policy": "UPDATE work_evidence_materials SET byte_length=byte_length+1 WHERE material_id IN "
                  "(SELECT material_id FROM work_evidence WHERE kind='policy_snapshot')",
    }
    with closing(sqlite3.connect(dispatch_store._repository.path)) as connection:
        connection.execute(statements[damage])
        connection.commit()
    with pytest.raises(ValueError):
        dispatch_store.load_dispatch_run("run-1")


def test_dispatch_refreshes_external_controls_and_keeps_lease_checks(dispatch_store: StateStore) -> None:
    states = dispatch_store
    other = SQLiteStateRepository(states.root)
    queue = [{"ticket_number": 5, "generation": 1, "role": "development", "lease_id": "old-token"}]
    other.save_objects("run-1", {"ticket_work_queue": queue}, fields={"ticket_work_queue"})
    admission = TicketWorkAdmission(states, "run-1")
    with states.current_run_reader("run-1", dispatch_only=True) as read:
        assert read()["ticket_work_queue"] == queue
        admission.check_control("old-token")
        other.save_objects("run-1", {"status": "operator_stopped"}, fields={"status"})
        assert read()["status"] == "operator_stopped"
        with pytest.raises(TaskControlBusyError, match="execution lease"):
            admission.check_control("old-token")
        other.save_objects("run-1", {"status": "active", "ticket_pause_requests": {"5": "requested"}},
                           fields={"status", "ticket_pause_requests"})
        assert read()["ticket_pause_requests"] == {"5": "requested"}
        with pytest.raises(TicketStopRequested):
            admission.check_control("old-token")
        other.save_objects("run-1", {"ticket_pause_requests": {}}, fields={"ticket_pause_requests"})
        admission.check_control("old-token")
        replacement = {"ticket_number": 5, "ticket_branch_generation": 2, "phase": "developing"}
        new_queue = [{**queue[0], "generation": 2, "lease_id": "new-token"}]
        with other.atomic():
            other.save_objects("run-1", {"ticket_jobs": {"5": replacement}}, fields={"ticket_jobs"},
                               keyed_changes={"ticket_jobs": {"5"}})
            other.save_objects("run-1", {"ticket_work_queue": new_queue}, fields={"ticket_work_queue"})
        assert generation(read(), 5) == 2
        with pytest.raises(TaskControlBusyError):
            admission.check_control("old-token")
        admission.check_control("new-token")
        with closing(sqlite3.connect(other.path)) as connection:
            connection.execute("UPDATE run_work_queue SET generation=1")
            connection.commit()
        with pytest.raises(ValueError, match="Queue ownership"):
            admission.check_control("new-token")


@pytest.mark.parametrize("damage", ["protocol", "schema"])
def test_dispatch_polling_rejects_each_unverified_changed_schema(dispatch_store: StateStore, damage: str) -> None:
    with dispatch_store.current_run_reader("run-1", dispatch_only=True) as read:
        assert read() is not None
        with closing(sqlite3.connect(dispatch_store._repository.path)) as connection:
            connection.execute("UPDATE schema_meta SET value='unsupported'" if damage == "protocol"
                               else "CREATE VIEW unexpected AS SELECT 1")
            connection.commit()
        with pytest.raises(ValueError):
            read()
        with pytest.raises(ValueError):
            read()


def test_dispatch_budget_is_shared_and_partial_view_cannot_be_a_save_baseline(dispatch_store: StateStore) -> None:
    repository = dispatch_store._repository
    with repository.atomic():
        current = dispatch_store.load_dispatch_run("run-1")
        assert current is not None
        with pytest.raises(ValueError, match="current transaction read"):
            repository.current_work_baseline("run-1")
    with query_scope(max_bytes=1), pytest.raises(QueryLimitError):
        repository.load_dispatch("run-1")
    repository.max_bytes = 1
    with pytest.raises(QueryLimitError):
        repository.load_dispatch("run-1")
    repository.max_bytes = 32 * 1024 * 1024
    assert repository.load_dispatch("run-1") is not None


def test_dispatch_watcher_is_read_only_idle_and_closed_after_error(
    dispatch_store: StateStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import sqlite_watcher

    watchers: list[SQLiteWatcher] = []

    class TrackingWatcher(SQLiteWatcher):
        def __enter__(self) -> SQLiteWatcher:
            watchers.append(self)
            return super().__enter__()

    monkeypatch.setattr(sqlite_watcher, "SQLiteWatcher", TrackingWatcher)
    before = len(list(Path("/proc/self/fd").iterdir()))
    with pytest.raises(OSError, match="caller failed"):
        with dispatch_store.current_run_reader("run-1", dispatch_only=True) as read:
            assert read() is not None
            connection = watchers[-1].connection
            assert connection is not None and not connection.in_transaction
            assert connection.execute("PRAGMA query_only").fetchone() == (1,)
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM runs")
            raise OSError("caller failed")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="scope is not active"):
        read()
    assert len(list(Path("/proc/self/fd").iterdir())) <= before


def test_dispatch_missing_storage_is_not_created(tmp_path: Path) -> None:
    states = StateStore(tmp_path / "absent")
    assert states.load_dispatch_run("missing") is None
    with states.current_run_reader("missing", dispatch_only=True) as read:
        assert read() is None
    assert not states.root.exists()


def test_dispatch_preview_never_replaces_full_write_admission(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from support.ticket_parallel import ticket_run

    state, states, _, _ = ticket_run(git_repo, (2,))
    original = states._load_work_for_update
    boundaries: list[bool] = []

    def load_for_update(*args: Any, **kwargs: Any) -> Any:
        boundaries.append(states._repository.in_transaction)
        return original(*args, **kwargs)

    monkeypatch.setattr(states, "_load_work_for_update", load_for_update)
    admission = TicketWorkAdmission(states, state["run_id"])
    assert admission.seed([2], current=states.load_dispatch_run(state["run_id"]))
    token = admission.acquire(2, 1, "development")
    assert token is not None
    admission.release(token)
    assert boundaries == [True, True, True]
