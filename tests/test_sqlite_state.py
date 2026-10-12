"""New Run durability, coordinated commits and read-only public queries."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_commits import StateCommitBusyError
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS, compact_invocation_details
from support.cli import run_cli, stdout_json
from support.seeding import seed_run, write_fixture
from support.workspace import managed_repo, managed_state
from support.ticket_parallel import TicketAgents, ticket_engine
from support.state import include_in_state_snapshot
from agent_run.git import GitRepository


def test_new_run_advances_and_public_queries_reopen_read_only(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": {
        "number": 2, "title": "Ticket 2", "body": "Implement 2.",
        "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": [],
    }})
    initial = stdout_json(seed_run(git_repo, fixture, idle_control=True))
    run_id = initial["run_id"]
    root = managed_state(git_repo)
    states = StateStore(root)
    ticket_engine(
        GitRepository(managed_repo(git_repo)), states, fixture,
        TicketAgents(git_repo, (2,), lambda _: None), development_only=True,
    ).deliver_ticket(run_id, 2)
    saved = StateStore(root).load_current_run(run_id)
    assert saved["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
    assert saved["ticket_jobs"]["2"]["candidate_sha"]
    assert saved["agent_invocation_history"]
    calls = []
    def update(current):
        calls.append(current["ticket_jobs"]["2"]["candidate_sha"])
        current["ticket_jobs"]["2"]["development_summary"] = "object commit"
        if (current.get("active_ticket_job") or {}).get("ticket_number") == 2:
            current["active_ticket_job"] = current["ticket_jobs"]["2"]
    saved = states.update_run(run_id, update)
    assert calls == [saved["ticket_jobs"]["2"]["candidate_sha"]]
    assert saved["ticket_jobs"]["2"]["development_summary"] == "object commit"
    assert not list((root / "runs").glob("*.json"))
    before = {str(path.relative_to(root)): path.read_bytes()
              for path in root.rglob("*") if path.is_file() and include_in_state_snapshot(path)}
    for command in ("runs", "status", "history"):
        selector = () if command == "runs" else (run_id,)
        result = run_cli(git_repo, fixture, command, *selector, "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        if command != "runs":
            assert stdout_json(result)["run_id"] == run_id
    assert StateStore(root).load_current_run(run_id) == saved
    assert {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and include_in_state_snapshot(path)} == before


@pytest.mark.parametrize("commit", [False, True])
def test_process_exit_exposes_only_complete_old_or_new_commit(tmp_path: Path, commit: bool) -> None:
    root = tmp_path / "state"
    store = StateStore(root)
    old = {"jobs": {"2": {"phase": "developing"}}, "budget": 1, "history": ["old"]}
    new = {"jobs": {"2": {"phase": "candidate"}}, "budget": 2, "history": ["old", "new"]}
    store.save_run("run-1", old)
    script = (
        "import os,sys,json; sys.path.insert(0,sys.argv[3]); from pathlib import Path; "
        "from agent_run.sqlite_state import SQLiteStateRepository; "
        f"repo=SQLiteStateRepository(Path(sys.argv[1]),max_bytes={MAX_RUN_STATE_BYTES}); "
        "context=repo.atomic(); context.__enter__(); repo.save('run-1',json.loads(sys.argv[2])); "
        + ("context.__exit__(None,None,None); " if commit else "")
        + "os._exit(23)"
    )
    result = subprocess.run([sys.executable, "-c", script, str(root), json.dumps(new),
                             str(Path(__file__).parents[1] / "src")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 23, result.stderr
    before = {path.name: path.read_bytes() for path in root.glob("state.sqlite3*")
              if path.is_file() and include_in_state_snapshot(path)}
    assert StateStore(root).load_run("run-1") == (new if commit else old)
    assert {path.name: path.read_bytes() for path in root.glob("state.sqlite3*")
            if path.is_file() and include_in_state_snapshot(path)} == before


def test_transition_failure_rolls_back_all_facts_and_never_notifies(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    old = {"budget": 1, "job": {"phase": "developing"}, "invocation": "running"}
    store.save_run("run-1", old)
    observed = []
    store.run_saved_observer = observed.append

    def transition() -> None:
        store._save_run_and_notify_unlocked("run-1", {
            "budget": 2, "job": {"phase": "candidate"}, "invocation": "completed",
        })
        raise OSError("storage failed before commit")

    with pytest.raises(OSError, match="before commit"):
        store._commit_run(transition)
    assert StateStore(tmp_path).load_run("run-1") == old
    assert observed == []


def test_fixed_materials_are_referenced_once_and_survive_reopen(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    context = {"parent": {"revision": "exact-revision", "body": "原始证据\n" * 3000},
               "scope": "ticket:2", "candidate_sha": "original-candidate"}
    state = {"ticket_jobs": {str(number): {"phase": "reviewing", "review_context": context}
                             for number in range(16)}}
    store.save_run("run-1", state)
    assert StateStore(tmp_path).load_run("run-1") == state
    assert (tmp_path / "state.sqlite3").stat().st_size < len(json.dumps(state).encode())
    state["ticket_jobs"]["2"]["phase"] = "accepted"
    store.save_run("run-1", state)
    saved = StateStore(tmp_path).load_run("run-1")
    assert saved == state
    assert saved["ticket_jobs"]["3"]["review_context"] == context


def test_damaged_list_lengths_are_rejected_before_large_allocation(tmp_path: Path, monkeypatch) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = repository._objects

    def damaged_objects(state, **kwargs):
        objects, materials = original(state, **kwargs)
        for path, (kind, value) in list(objects.items()):
            if kind == "list" and value == "0":
                objects[path] = (kind, "4096")
        return objects, materials

    # Corrupt only list-length metadata while using the real SQL write path;
    # no table names or alternate in-memory store form the failure fixture.
    monkeypatch.setattr(repository, "_objects", damaged_objects)
    repository.save("run-1", {"lists": [[] for _ in range(40)]})
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="Missing SQLite Run list item"):
            repository.load("run-1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024


def test_consistent_query_keeps_one_commit_while_writer_advances(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path)
    old = {"job": "developing", "budget": 1, "history": ["started"]}
    new = {"job": "candidate", "budget": 2, "history": ["started", "returned"]}
    store.save_run("run-1", old)
    entered, release = threading.Event(), threading.Event()
    original = SQLiteStateRepository._check_schema

    def check(connection, **kwargs):
        result = original(connection, **kwargs)
        if threading.current_thread().name == "consistent-reader":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(SQLiteStateRepository, "_check_schema", staticmethod(check))
    results = []
    reader = threading.Thread(target=lambda: results.append(store.load_run("run-1")),
                              name="consistent-reader")
    reader.start()
    try:
        assert entered.wait(5)
        StateStore(tmp_path).save_run("run-1", new)
    finally:
        release.set()
        reader.join(5)
    assert not reader.is_alive()
    assert results == [old]
    assert store.load_run("run-1") == new


@pytest.mark.parametrize("revocation", ["stop", "abandon", "generation"])
def test_queued_commit_revalidates_real_executor_authority(tmp_path: Path, monkeypatch, revocation: str) -> None:
    from functools import partial
    from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey

    store = StateStore(tmp_path / "state")
    store.save_run("run-1", {"version": 1})
    control = TaskControlStore(store.root)
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    control.bind_run(task, claim.action_id, "run-1", generation=generation)
    control.mark_handshake(task, action_id=claim.action_id, generation=generation,
                           pid=900, process_start_token="executor")
    control.record_application(task, action_id=claim.action_id, generation=generation,
                               run_id="run-1", payload_digest=claim.action["payload_digest"])
    control.complete_action(task, action_id=claim.action_id, generation=generation,
                            result_status="active")
    store._set_write_guard(None, transaction=partial(
        control._executor_current_transaction, task, action_id=claim.action_id,
        generation=generation, run_id="run-1",
    ))
    waiting = threading.Event()
    original_wait = store._coordinator._condition.wait
    def wait(timeout=None):
        waiting.set()
        return original_wait(timeout)
    monkeypatch.setattr(store._coordinator._condition, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.commit_admission():
            request = pool.submit(store.save_run, "run-1", {"version": 2})
            assert waiting.wait(5)
            if revocation == "generation":
                control.finish_executor(task, action_id=claim.action_id, generation=generation)
                next_action = control.claim_action(task, kind="resume", payload={})
                control.begin_executor(task, action_id=next_action.action_id, run_id="run-1")
            else:
                control.claim_control_action(task, kind=revocation, payload={},
                                             run_id="run-1", state_dir=store.root)
        with pytest.raises(ActionReconciliationError):
            request.result(timeout=5)
    assert store.load_run("run-1") == {"version": 1}


def test_observer_waits_outside_commit_locks_and_cannot_fail_saved_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    observing, release = threading.Event(), threading.Event()
    observations = []

    def observe(state):
        observations.append(state["version"])
        if state["version"] == 1:
            observing.set()
            assert release.wait(5)
            raise RuntimeError("notification failed")

    store.run_saved_observer = observe
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.save_run, "run-1", {"version": 1})
        try:
            assert observing.wait(5)
            second = pool.submit(store.save_run, "run-1", {"version": 2})
            second.result(timeout=5)
            assert StateStore(tmp_path).load_run("run-1") == {"version": 2}
        finally:
            release.set()
        first.result(timeout=5)
    assert observations == [1, 2]


def test_commit_queue_is_fifo_bounded_and_releases_failed_waiters(tmp_path: Path, monkeypatch) -> None:
    from agent_run import state_commits

    monkeypatch.setattr(state_commits, "MAX_PENDING_COMMITS", 3)
    store = StateStore(tmp_path)
    waiting = {name: threading.Event() for name in ("second", "third")}
    real_wait = store._coordinator._condition.wait

    def wait(timeout=None):
        waiting[threading.current_thread().name].set()
        return real_wait(timeout)

    monkeypatch.setattr(store._coordinator._condition, "wait", wait)
    order = []

    def commit(version):
        store._commit_run(lambda: (order.append(version), store._save_run_and_notify_unlocked(
            "run-1", {"version": version})))

    second = threading.Thread(target=commit, args=(2,), name="second")
    third = threading.Thread(target=commit, args=(3,), name="third")
    try:
        with store.commit_admission():
            second.start()
            assert waiting["second"].wait(5)
            third.start()
            assert waiting["third"].wait(5)
            errors = []
            def overflow():
                try:
                    commit(4)
                except StateCommitBusyError as error:
                    errors.append(str(error))
            rejected = threading.Thread(target=overflow)
            rejected.start()
            rejected.join(5)
            assert not rejected.is_alive()
            assert errors == ["local state commit queue is full"]
    finally:
        second.join(5)
        if third.ident is not None:
            third.join(5)
    assert not second.is_alive() and not third.is_alive()
    assert order == [2, 3]
    assert store.load_run("run-1") == {"version": 3}
    store.save_run("run-1", {"version": 5})


def test_invocation_detail_retention_protects_recovery_and_review_facts() -> None:
    history = [{"work_subject": "ticket:2", "started_at": str(number), "status": "completed",
                "semantic_attempt": {"attempt_id": f"attempt-{number}", "review_context": {"evidence": "original"}},
                "execution_segments": [{"started_at": "old", "ended_at": "done"}],
                "recovery_wait_intervals": [{"started_at": "wait", "ended_at": "ready"}],
                "resume_id": "retained-audit"}
               for number in range(MAX_DETAILED_INVOCATIONS + 3)]
    state = {"agent_invocation_history": history,
             "active_agent_invocation": deepcopy(history[0]),
             "ticket_jobs": {"2": {"pending_semantic_attempt": {"attempt_id": "attempt-1"}}}}
    compact_invocation_details(state)
    assert history[0]["execution_segments"] and history[1]["execution_segments"]
    assert history[2]["execution_segments"] == []
    assert history[2]["execution_segments_summary"]["count"] == 1
    assert history[2]["semantic_attempt"]["review_context"] == {"evidence": "original"}
    assert history[2]["resume_id"] == "retained-audit"
    assert history[-1]["execution_segments"]
    before = deepcopy(state)
    compact_invocation_details(state)
    assert state == before
