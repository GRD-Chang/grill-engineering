"""New Run durability, coordinated commits and read-only public queries."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.state import StateStore
from agent_run.state_limits import MAX_QUERY_BYTES
from agent_run.ticket_state import TicketState
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_commits import StateCommitBusyError
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS, compact_invocation_details
from support.cli import run_cli, stdout_json
from support.seeding import seed_run, write_fixture
from support.workspace import managed_repo, managed_state
from support.ticket_parallel import TicketAgents, ticket_engine
from support.state import include_in_state_snapshot, resume_audit_for_successors
from agent_run.git import GitRepository
from agent_run.object_state import save_owned
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.sqlite_work_evidence import MAX_EVIDENCE_BYTES


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
    lane = TicketState(states, run_id, 2)
    current = lane.load_current_run(run_id)
    assert current is not None
    update(current)
    lane.save_run(run_id, current)
    saved = states.load_current_run(run_id)
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
    old = {"parent_job": {"phase": "developing", "modification_attempts": 1}, "status": "active"}
    new = {"parent_job": {"phase": "candidate", "modification_attempts": 2}, "status": "waiting_for_checks"}
    store.save_run("run-1", old)
    script = (
        "import os,sys,json; sys.path.insert(0,sys.argv[3]); from pathlib import Path; "
        "from agent_run.sqlite_state import SQLiteStateRepository; "
        f"repo=SQLiteStateRepository(Path(sys.argv[1]),max_bytes={MAX_QUERY_BYTES}); "
        "context=repo.atomic(); context.__enter__(); repo.save_objects('run-1',json.loads(sys.argv[2]),fields={'parent_job','status'}); "
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
    invocation = {"work_subject": "parent-only:run-1", "started_at": "current", "status": "running"}
    old = {"parent_job": {"phase": "developing", "review_budget": {"development_attempts": 1}},
           "active_agent_invocation": invocation, "status": "active"}
    store.save_run("run-1", old)
    observed = []
    store.run_saved_observer = observed.append

    def transition() -> None:
        save_owned(store, "run-1", {
            "parent_job": {"phase": "candidate", "review_budget": {"development_attempts": 2}},
            "active_agent_invocation": {**invocation, "status": "completed"}, "status": "waiting_for_checks",
        }, fields={"parent_job", "active_agent_invocation", "status"})
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
                             for number in range(2, 18)}}
    store.save_run("run-1", state)
    assert StateStore(tmp_path).load_run("run-1") == state
    assert (tmp_path / "state.sqlite3").stat().st_size < len(json.dumps(state).encode())
    state["ticket_jobs"]["2"]["phase"] = "accepted"
    save_owned(store, "run-1", state, fields={"ticket_jobs"})
    saved = StateStore(tmp_path).load_run("run-1")
    assert saved == state
    assert saved["ticket_jobs"]["3"]["review_context"] == context


def test_consistent_query_keeps_one_commit_while_writer_advances(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path)
    old = {"parent_job": {"phase": "developing", "modification_attempts": 1}, "status": "active"}
    new = {"parent_job": {"phase": "candidate", "modification_attempts": 2}, "status": "waiting_for_checks"}
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
        save_owned(StateStore(tmp_path), "run-1", new, fields=set(new))
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
    store.save_run("run-1", {"retired_parent_generation": 1})
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
            request = pool.submit(save_owned, store, "run-1", {"retired_parent_generation": 2}, fields={"retired_parent_generation"})
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
    assert store.load_run("run-1") == {"retired_parent_generation": 1}


def test_observer_waits_outside_commit_locks_and_cannot_fail_saved_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    observing, release = threading.Event(), threading.Event()
    observations = []

    def observe(state):
        observations.append(state["retired_parent_generation"])
        if state["retired_parent_generation"] == 1:
            observing.set()
            assert release.wait(5)
            raise RuntimeError("notification failed")

    store.run_saved_observer = observe
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.save_run, "run-1", {"retired_parent_generation": 1})
        try:
            assert observing.wait(5)
            second = pool.submit(save_owned, store, "run-1", {"retired_parent_generation": 2}, fields={"retired_parent_generation"})
            second.result(timeout=5)
            assert StateStore(tmp_path).load_run("run-1") == {"retired_parent_generation": 2}
        finally:
            release.set()
        first.result(timeout=5)
    assert observations == [1, 2]


def test_commit_queue_is_fifo_bounded_and_releases_failed_waiters(tmp_path: Path, monkeypatch) -> None:
    from agent_run import state_commits

    monkeypatch.setattr(state_commits, "MAX_PENDING_COMMITS", 3)
    store = StateStore(tmp_path)
    store.save_run("run-1", {"retired_parent_generation": 1})
    waiting = {name: threading.Event() for name in ("second", "third")}
    real_wait = store._coordinator._condition.wait

    def wait(timeout=None):
        waiting[threading.current_thread().name].set()
        return real_wait(timeout)

    monkeypatch.setattr(store._coordinator._condition, "wait", wait)
    order = []

    def commit(version):
        store._commit_run(lambda: (order.append(version), save_owned(
            store, "run-1", {"retired_parent_generation": version}, fields={"retired_parent_generation"})))

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
    assert store.load_run("run-1") == {"retired_parent_generation": 3}
    save_owned(store, "run-1", {"retired_parent_generation": 5}, fields={"retired_parent_generation"})


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


def _stored_attempt(subject: str, context: dict, *, role: str = "development", completed: bool = False) -> dict:
    owner: dict = {}
    attempt = allocate_semantic_attempt(owner, role=role, work_subject=subject, generation=1,
                                       currentness_boundary=context, ordinal=1,
                                       budget_window=None if role == "publication" else 1)
    attempt["review_context"] = deepcopy(context)
    if completed:
        close_semantic_attempt(owner, attempt, outcome="fixture_result")
        return owner["semantic_attempt_history"][0]
    return attempt


def _stored_invocation(subject: str, started: str, context: dict, *, status: str = "completed") -> dict:
    return {"work_subject": subject, "started_at": started, "status": status,
            "generation": 1, "role": "development", "phase": "developing", "mode": "fresh",
            "input_fingerprint": "fixture-input", "currentness_boundary": deepcopy(context),
            "semantic_attempt": _stored_attempt(subject, context)}


def _work_fixture(unrelated: int = 0) -> dict:
    current = _stored_invocation("ticket:2", "current", {"body": "本票准确材料" * 200}, status="running")
    history = [_stored_invocation("ticket:2" if number % 2 else "ticket:3", str(number),
                                  {"body": f"历史-{number}" + "历史材料" * 200})
               for number in range(unrelated)] + [deepcopy(current)]
    return {"ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"}, "3": {"ticket_number": 3, "phase": "blocked"}},
            "ticket_agent_invocations": {"2": current, "3": _stored_invocation(
                "ticket:3", "sibling", {"body": "兄弟材料" * 400})},
            "agent_invocation_history": history, "phase": "delivery"}


def test_work_patch_preserves_unloaded_history_and_sibling_after_reopen(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(20)
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        assert len(baseline["agent_invocation_history"]) == 1
        assert set(baseline["ticket_agent_invocations"]) == {"2", "3"}
        assert baseline["ticket_agent_invocations"]["3"] == original["ticket_agent_invocations"]["3"]
        updated = deepcopy(baseline)
        updated["ticket_jobs"]["2"]["phase"] = "candidate"
        updated["agent_invocation_history"][0]["status"] = "completed"
        updated["ticket_agent_invocations"]["2"]["status"] = "completed"
        updated["agent_invocation_history"].append({"work_subject": "ticket:2", "started_at": "next", "status": "running"})
        repository.save_work("run-1", updated, baseline)
    expected = deepcopy(original)
    expected["ticket_jobs"]["2"]["phase"] = "candidate"
    expected["agent_invocation_history"][-1]["status"] = "completed"
    expected["ticket_agent_invocations"]["2"]["status"] = "completed"
    expected["agent_invocation_history"].append(updated["agent_invocation_history"][-1])
    reopened = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    assert reopened.load("run-1") == expected


@pytest.mark.parametrize("empty_core", [True, False])
def test_work_replacing_thin_active_job_accounts_for_unloaded_archive(
    tmp_path: Path, empty_core: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(4)
    original["ticket_jobs"]["2"]["semantic_attempt_history"] = [_stored_attempt("ticket:2", {"evidence": "target"}, completed=True)]
    original["ticket_jobs"]["3"] = {"ticket_number": 3, **({} if empty_core else {"phase": "completed"}), "semantic_attempt_history": [
        _stored_attempt("ticket:3", {"body": "old sibling evidence" * 1000}, completed=True)], "human_blocker_history": [{"question": "old question"}]}
    original["active_ticket_job"] = deepcopy(original["ticket_jobs"]["3"])
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        assert "semantic_attempt_history" not in baseline["active_ticket_job"]
        updated = deepcopy(baseline)
        updated["active_ticket_job"] = deepcopy(updated["ticket_jobs"]["2"])
        repository.save_work("run-1", updated, baseline)
    expected = deepcopy(original)
    expected["active_ticket_job"] = deepcopy(expected["ticket_jobs"]["2"])
    assert SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES).load("run-1") == expected


def test_work_rejects_replacing_unloaded_sibling_archive(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(4)
    original["ticket_jobs"]["3"]["semantic_attempt_history"] = [_stored_attempt("ticket:3", {"evidence": "retained"}, completed=True)]
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="cannot replace unloaded Job history"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            assert baseline is not None
            updated = deepcopy(baseline)
            updated["ticket_jobs"]["3"]["semantic_attempt_history"] = []
            repository.save_work("run-1", updated, baseline)
    assert repository.load("run-1") == original


@pytest.mark.parametrize("damage", ["logical_bytes", "version", "missing"])
def test_full_and_current_queries_reject_damage_in_their_read_scope(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(4)
    original["ticket_jobs"]["2"]["semantic_attempt_history"] = [_stored_attempt("ticket:2", {"evidence": "retained"}, completed=True)]
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    # Model damaged storage through a separate connection. Normal writes keep
    # foreign keys enabled and cannot manufacture a missing version reference.
    with sqlite3.connect(repository.path) as connection:
        if damage == "missing":
            connection.execute("UPDATE work_job_attempt_history SET version_id='missing' WHERE run_id='run-1'")
        elif damage == "version":
            connection.execute("UPDATE work_attempt_versions SET status='damaged' WHERE run_id='run-1'")
        else:
            connection.execute("UPDATE work_attempt_versions SET logical_bytes=logical_bytes+1 WHERE run_id='run-1'")
    with pytest.raises(ValueError, match="Semantic Attempt"):
        repository.load("run-1")
    if damage == "missing":
        # This archive has not participated in the current query or decision.
        assert "semantic_attempt_history" not in repository.load_work("run-1", 2)["ticket_jobs"]["2"]
    else:
        with pytest.raises(ValueError, match="Semantic Attempt"):
            repository.load_work("run-1", 2)


def test_existing_attempt_update_keeps_archive_metadata_exact_after_reopen(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(4)
    original["ticket_jobs"]["2"]["semantic_attempt_history"] = [_stored_attempt("ticket:2", {}, role="publication", completed=True)]
    original["active_ticket_job"] = {"ticket_number": 2, **deepcopy(original["ticket_jobs"]["2"])}
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load("run-1")
        assert baseline is not None
        updated = deepcopy(baseline)
        for owner in (updated["ticket_jobs"]["2"], updated["active_ticket_job"]):
            owner["semantic_attempt_history"][0]["publication_operation_retry"] = {"attempts": 5, "limit": 5}
        repository.save_objects("run-1", updated, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    expected = deepcopy(original)
    for owner in (expected["ticket_jobs"]["2"], expected["active_ticket_job"]):
        owner["semantic_attempt_history"][0]["publication_operation_retry"] = {"attempts": 5, "limit": 5}
    assert SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES).load("run-1") == expected


@pytest.mark.parametrize("ticket", [None, 2])
def test_work_projection_retains_resume_successor_evidence(tmp_path: Path, ticket: int | None) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(10)
    original["resume_audit"] = resume_audit_for_successors("run-1", [original["agent_invocation_history"][0]])
    resume_id = original["resume_audit"]["history"][0]["resume_id"]
    repository.save("run-1", original)
    projected = repository.load_work("run-1", ticket)
    assert projected is not None
    assert any(entry.get("resume_id") == resume_id for entry in projected["agent_invocation_history"])
    assert {entry["started_at"] for entry in projected["agent_invocation_history"]} == {"0", "current"}


@pytest.mark.parametrize("failure", ["size", "exception"])
def test_work_patch_failure_rolls_back_materials_and_history(tmp_path: Path, failure: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = _work_fixture(1)
    repository.save("run-1", original)
    with pytest.raises((ValueError, OSError), match="limit|injected"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            assert baseline is not None
            updated = deepcopy(baseline)
            updated["agent_invocation_history"].append(_stored_invocation(
                "ticket:2", "next", {"body": "x" * (MAX_EVIDENCE_BYTES + 1 if failure == "size" else 1200)}, status="running"))
            repository.save_work("run-1", updated, baseline)
            assert failure == "exception", "oversized evidence was accepted"
            raise OSError("injected failure")
    assert SQLiteStateRepository(tmp_path).load("run-1") == original


def test_work_global_retention_preserves_sibling_current_and_pending_attempt(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(256)
    for entry in original["agent_invocation_history"][:3]:
        entry["execution_segments"] = [{"started_at": "2026-10-13T00:00:00+00:00", "ended_at": "2026-10-13T00:00:01+00:00"}]
    original["ticket_agent_invocations"]["3"] = deepcopy(original["agent_invocation_history"][0])
    original["ticket_jobs"]["2"]["pending_semantic_attempt"] = deepcopy(original["agent_invocation_history"][1]["semantic_attempt"])
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        updated = deepcopy(baseline)
        updated["agent_invocation_history"].extend([
            {"work_subject": "ticket:2", "started_at": "new-1", "status": "completed"},
            {"work_subject": "ticket:2", "started_at": "new-2", "status": "completed"}])
        repository.save_work("run-1", updated, baseline)
    saved = repository.load("run-1")
    assert saved is not None
    history = saved["agent_invocation_history"]
    assert history[0]["execution_segments"] and history[1]["execution_segments"]
    assert history[2]["execution_segments"] == []
    assert history[2]["execution_segments_summary"]["count"] == 1


@pytest.mark.parametrize("damage", ["history", "refs"])
def test_complete_queries_recheck_damaged_invocation_and_evidence_after_successful_read(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    repository.save("run-1", _work_fixture(3))
    assert repository.load("run-1") is not None
    with sqlite3.connect(repository.path) as connection:
        if damage == "history":
            connection.execute("DELETE FROM work_invocations WHERE run_id='run-1' AND started_at='0'")
        else:
            connection.execute("UPDATE work_evidence SET owner_id='wrong-owner:'||owner_id WHERE run_id='run-1'")
    with pytest.raises(ValueError, match="metadata|index|references|Invocation|evidence"):
        repository.load("run-1")


def test_work_gc_keeps_materials_needed_by_unloaded_history_and_other_run(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(2)
    shared_context = deepcopy(original["agent_invocation_history"][0]["semantic_attempt"]["review_context"])
    original["ticket_agent_invocations"]["2"] = _stored_invocation("ticket:2", "current", shared_context, status="running")
    original["agent_invocation_history"][-1] = deepcopy(original["ticket_agent_invocations"]["2"])
    repository.save("run-1", original)
    other = {"parent_job": {"review_context": deepcopy(shared_context)}}
    repository.save("run-2", other)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        updated = deepcopy(baseline)
        replacement = _stored_invocation("ticket:2", "replacement", {"body": "replacement" * 200}, status="running")
        updated["ticket_agent_invocations"]["2"] = replacement
        updated["agent_invocation_history"].append(deepcopy(replacement))
        repository.save_work("run-1", updated, baseline)
    expected = deepcopy(original)
    expected["ticket_agent_invocations"]["2"] = deepcopy(replacement)
    expected["agent_invocation_history"].append(deepcopy(replacement))
    assert repository.load("run-1") == expected
    assert repository.load("run-2") == other
    # Complete removal exercises released object refs and actual SQL GC.
    repository.save_objects("run-1", {"phase": "removed"}, fields={"phase", "ticket_jobs", "ticket_agent_invocations", "active_ticket_job", "active_agent_invocation"})
    assert repository.load("run-2") == other


def test_work_material_damage_is_bounded_and_old_protocol_is_rejected(tmp_path: Path) -> None:
    from agent_run.state_errors import IncompatibleRunStateError
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    repository.save("run-1", _work_fixture())
    with repository.atomic():
        repository._local.connection.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1")
    with pytest.raises(ValueError, match="byte length"):
        repository.load_work("run-1", 2)
    with repository.atomic():
        repository._local.connection.execute("UPDATE schema_meta SET value='sqlite-run-objects-v1'")
    with pytest.raises(IncompatibleRunStateError, match="protocol"):
        repository.load_work("run-1", 2)


def test_work_shared_material_decode_keeps_returned_objects_independent(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture()
    repository.save("run-1", original)
    with repository.atomic():
        projected = repository.load_work("run-1", 2)
        assert projected is not None
        projected["agent_invocation_history"][0]["semantic_attempt"]["review_context"]["body"] = "changed history"
        assert projected["ticket_agent_invocations"]["2"]["semantic_attempt"]["review_context"] == original["ticket_agent_invocations"]["2"]["semantic_attempt"]["review_context"]
    assert repository.load("run-1") == original


def test_repeated_work_queries_return_independent_views_and_follow_writes(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture()
    repository.save("run-1", original)
    with repository.atomic():
        projected = repository.load_work("run-1", 2)
        assert projected is not None
        projected["ticket_jobs"]["2"]["phase"] = "local mutation"
        repeated = repository.load_work("run-1", 2)
        assert repeated is not None
        assert repeated["ticket_jobs"]["2"]["phase"] == original["ticket_jobs"]["2"]["phase"]
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        repeated["ticket_jobs"]["2"]["phase"] = "candidate"
        repository.save_work("run-1", repeated, baseline)
        fresh = repository.load_work("run-1", 2)
        assert fresh is not None and fresh["ticket_jobs"]["2"]["phase"] == "candidate"
        sibling = repository.load_work("run-1", 3)
        assert sibling is not None
        assert sibling["ticket_agent_invocations"]["3"] == original["ticket_agent_invocations"]["3"]
    other = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    saved = other.load("run-1")
    assert saved is not None
    saved["ticket_jobs"]["2"]["phase"] = "external"
    other.save_objects("run-1", saved, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    refreshed = repository.load_work("run-1", 2)
    assert refreshed is not None and refreshed["ticket_jobs"]["2"]["phase"] == "external"


@pytest.mark.parametrize("damage", ["material", "protocol", "schema"])
def test_work_transaction_view_rechecks_storage_after_direct_sql(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    repository.save("run-1", _work_fixture())
    with repository.atomic():
        assert repository.load_work("run-1", 2) is not None
        connection = repository._local.connection
        if damage == "material":
            connection.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1")
        elif damage == "protocol":
            connection.execute("UPDATE schema_meta SET value='unsupported'")
        else:
            # DDL leaves total_changes unchanged, so the separate schema check
            # must still reject it before returning the transaction's view.
            connection.execute("CREATE VIEW unexpected AS SELECT 1")
        with pytest.raises(ValueError, match="byte length|protocol|schema"):
            repository.load_work("run-1", 2)


def test_work_add_remove_preserves_exact_material_types(tmp_path: Path) -> None:
    import math
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(2)
    original["parent_job"] = {"review_context": {"value": 0.0}}
    repository.save("run-1", original)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        updated = deepcopy(baseline)
        del updated["ticket_agent_invocations"]["2"]
        updated["parent_job"]["review_context"]["value"] = -0.0
        repository.save_work("run-1", updated, baseline)
    expected = deepcopy(original)
    del expected["ticket_agent_invocations"]["2"]
    expected["parent_job"]["review_context"]["value"] = -0.0
    assert repository.load("run-1") == expected
    assert math.copysign(1, repository.load("run-1")["parent_job"]["review_context"]["value"]) == -1
    with repository.atomic():
        baseline = repository.load_work("run-1", 4)
        assert baseline is not None
        updated = deepcopy(baseline)
        updated["ticket_agent_invocations"]["4"] = {"work_subject": "ticket:4", "started_at": "new", "status": "running"}
        updated["parent_job"]["review_context"]["value"] = False
        repository.save_work("run-1", updated, baseline)
    expected["ticket_agent_invocations"]["4"] = updated["ticket_agent_invocations"]["4"]
    expected["parent_job"]["review_context"]["value"] = False
    saved = repository.load("run-1")
    assert saved == expected and type(saved["parent_job"]["review_context"]["value"]) is bool


@pytest.mark.parametrize("index", ["work_invocation_history_subject", "work_jobs_owner", "work_attempt_owner_role", "work_evidence_content"])
def test_work_required_history_index_cannot_be_removed(tmp_path: Path, index: str) -> None:
    from agent_run.state_errors import IncompatibleRunStateError
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    repository.save("run-1", _work_fixture())
    with repository.atomic():
        repository._local.connection.execute(f"DROP INDEX {index}")
    with pytest.raises(IncompatibleRunStateError, match="indexes"):
        repository.load("run-1")
    with pytest.raises(IncompatibleRunStateError, match="indexes"):
        repository.load_work("run-1", 2)


def test_work_patch_can_initialize_history_and_canonical_collection(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"phase": "delivery"})
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        updated = deepcopy(baseline)
        invocation = {"work_subject": "ticket:2", "started_at": "first", "status": "running"}
        updated["ticket_agent_invocations"] = {"2": invocation}
        updated["agent_invocation_history"] = [deepcopy(invocation)]
        repository.save_work("run-1", updated, baseline)
    assert repository.load("run-1") == updated


def test_work_and_owned_commits_share_fresh_independent_transaction_views(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(2)
    repository.save("run-1", original)
    with repository.atomic():
        admission = repository.load_work("run-1", None)
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None and admission is not None
        updated = deepcopy(baseline)
        updated["ticket_jobs"]["2"]["phase"] = "candidate"
        assert admission["ticket_jobs"]["2"]["phase"] == "developing"
        repository.save_work("run-1", updated, baseline)
        assert repository.load_work("run-1", None)["ticket_jobs"]["2"]["phase"] == "candidate"
        replacement = deepcopy(original)
        replacement["ticket_jobs"]["2"]["phase"] = "accepted"
        repository.save_objects("run-1", replacement, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
        assert repository.load_work("run-1", 2)["ticket_jobs"]["2"]["phase"] == "accepted"
    assert repository.load("run-1") == replacement


def test_current_work_materials_preserve_unicode_and_nul(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_QUERY_BYTES)
    original = _work_fixture(2)
    original["ticket_agent_invocations"]["2"] = _stored_invocation("ticket:2", "current", {"body": "原始证据\x00继续" * 300}, status="running")
    original["agent_invocation_history"][-1] = deepcopy(original["ticket_agent_invocations"]["2"])
    repository.save("run-1", original)
    projected = repository.load_work("run-1", 2)
    assert projected is not None
    assert projected["ticket_agent_invocations"]["2"]["semantic_attempt"]["review_context"] == original["ticket_agent_invocations"]["2"]["semantic_attempt"]["review_context"]
    assert projected["agent_invocation_history"][-1] == original["agent_invocation_history"][-1]
