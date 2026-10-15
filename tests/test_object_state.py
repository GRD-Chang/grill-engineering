"""Real SQLite projection, identity, atomicity and bounded-history regressions."""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.object_state import save_owned
from agent_run.sqlite_state import SQLiteStateRepository
from support.state import persist_run_fixture
from support.work_records import invocation as stored_invocation


def _invocation(position: int, **extra: Any) -> dict[str, Any]:
    subject = extra.pop("work_subject", "parent-only:run-1")
    context = extra.pop("review_context", {"position": position})
    if "evidence" in extra:
        context = {**context, "evidence": extra.pop("evidence")}
    role, phase = ("fresh_acceptance", "run_acceptance") if subject.startswith("run-acceptance:") else (
        ("final_publication", "final_publication") if subject.startswith("run-publication:")
        else ("development", "developing"))
    value = stored_invocation(subject, extra.pop("started_at", f"invocation-{position}"), context=context,
                              status=extra.pop("status", "completed"), role=role, phase=phase)
    return {**value, **extra}


def test_current_projection_reads_only_exact_invocation_and_resume_links(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    history = [_invocation(number, evidence="x" * 2048) for number in range(500)]
    history[12]["resume_id"] = "resume-1"
    state = {"phase": "developing", "active_agent_invocation": history[-1],
             "resume_audit": {"history": [{"resume_id": "resume-1"}]},
             "agent_invocation_history": history}
    repository.save("run-1", state)
    projection, positions = repository.load_objects("run-1")  # type: ignore[misc]
    assert projection["agent_invocation_history"] == [history[12], history[-1]]
    assert len(positions) == 2
    projection["phase"] = "candidate"
    repository.save_objects("run-1", projection, fields={"phase"}, history_positions=positions)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == {**state, "phase": "candidate"}


def test_history_update_and_duplicate_append_keep_unrelated_archives(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    previous = _invocation(1, status="running")
    original = {"active_agent_invocation": previous,
                "agent_invocation_history": [_invocation(0), previous]}
    repository.save("run-1", original)
    state, positions = repository.load_objects("run-1")  # type: ignore[misc]
    state["agent_invocation_history"][0]["status"] = "completed"
    state["active_agent_invocation"]["status"] = "completed"
    state["agent_invocation_history"].append(_invocation(2))
    repository.save_objects("run-1", state, fields={"agent_invocation_history"}, history_positions=positions)
    repository.save_objects("run-1", state, fields={"agent_invocation_history"}, history_positions=positions)
    saved = repository.load("run-1")
    assert saved["agent_invocation_history"] == [  # type: ignore[index]
        _invocation(0), _invocation(1), _invocation(2)]
    assert saved["active_agent_invocation"] == _invocation(1)  # type: ignore[index]


def test_scoped_size_limit_and_failure_restore_prior_objects_and_indexes(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=512)
    original = {"active_agent_invocation": _invocation(0), "agent_invocation_history": [_invocation(0)], "phase": "developing"}
    repository.save("run-1", original)
    state, positions = repository.load_objects("run-1")  # type: ignore[misc]
    state["phase"] = "x" * 512
    with pytest.raises(ValueError, match="persistence limit"):
        repository.save_objects("run-1", state, fields={"phase"}, history_positions=positions)
    assert SQLiteStateRepository(tmp_path, max_bytes=512).load("run-1") == original
    assert repository.load_objects("run-1")[0] == original  # type: ignore[index]


def test_unrelated_archived_material_is_not_decoded_by_current_commit(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    state = {"phase": "developing", "active_agent_invocation": _invocation(1),
             "agent_invocation_history": [_invocation(0, evidence="x" * 1024), _invocation(1)]}
    repository.save("run-1", state)
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute("UPDATE work_evidence_materials SET body='broken' WHERE instr(body,?)>0", ("x" * 1024,))
        assert cursor.rowcount >= 1
    projection, positions = repository.load_objects("run-1")  # type: ignore[misc]
    projection["phase"] = "candidate"
    repository.save_objects("run-1", projection, fields={"phase"}, history_positions=positions)
    assert repository.load_objects("run-1")[0]["phase"] == "candidate"  # type: ignore[index]
    with pytest.raises(ValueError, match="work evidence byte length"):
        repository.load("run-1")


def test_damaged_list_length_is_rejected_before_allocating(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"current_list": ["existing"], "agent_invocation_history": []})
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE run_objects SET value_json='999999999999' WHERE kind='list' AND object_path=?",
                           ('["current_list"]',))
    with pytest.raises(ValueError, match="field size proof"):
        repository.load_objects("run-1")


def test_scoped_material_replacement_preserves_shared_archive_and_collects_unused(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=16 * 1024)
    shared = "shared" * 256
    state = {"current": {"evidence": shared}, "other": {"evidence": shared},
             "agent_invocation_history": [_invocation(0, evidence=shared)]}
    repository.save("run-1", state)
    repository.save("run-2", {"current": {"evidence": shared},
                              "agent_invocation_history": [_invocation(0, evidence=shared)]})
    repository.save_objects("run-1", {"current": {"evidence": "changed" * 256}}, fields={"current"})
    reopened = repository.load("run-1")
    assert reopened == {**state, "current": {"evidence": "changed" * 256}}
    repository.save_objects("run-1", {"current": {"evidence": "small"}}, fields={"current"})
    with sqlite3.connect(repository.path) as connection:
        assert not connection.execute("SELECT 1 FROM run_materials WHERE value=?", ("changed" * 256,)).fetchall()
        assert connection.execute("SELECT 1 FROM run_materials WHERE value=?", (shared,)).fetchall()
        assert connection.execute("SELECT 1 FROM work_evidence_materials WHERE instr(body,?)>0", (shared,)).fetchall()
    assert repository.load("run-2") == {"current": {"evidence": shared},
                                         "agent_invocation_history": [_invocation(0, evidence=shared)]}


def test_owned_commit_checks_action_receipt_and_notifies_after_durability(tmp_path: Path) -> None:
    import os

    from agent_run.executor_host import _process_start_token
    from agent_run.object_state import save_owned
    from agent_run.run_lifecycle import prepare_action_application_receipt
    from agent_run.state import ConcurrentRunStateError, StateStore
    from agent_run.task_control import TaskControlStore, TaskKey

    store = StateStore(tmp_path)
    control = TaskControlStore(store.root)
    task = TaskKey(tmp_path / "repo", "example/repo", 1)
    waiting = control.claim_action(task, kind="run", payload={"parent": 1})
    assert waiting.action is not None and waiting.action_id is not None
    reservation = control.begin_executor(task, action_id=waiting.action_id, run_id="run-1")
    control.bind_run(task, waiting.action_id, "run-1", state_dir=store.root)
    control.mark_handshake(
        task, action_id=waiting.action_id, generation=reservation.generation,
        pid=os.getpid(), process_start_token=_process_start_token(os.getpid()),
    )
    original = {"run_id": "run-1", "repository": "example/repo", "parent": {"number": 1},
                "status": "waiting_external", "supervision_window": {"retry_count": 0}}
    prepare_action_application_receipt(original, waiting.action)
    store.save_run("run-1", original)
    original = store.load_run("run-1")
    assert original is not None
    control.complete_action(
        task, action_id=waiting.action_id, generation=reservation.generation,
        result_status="waiting_external",
    )
    stop = control.claim_control_action(
        task, kind="stop", payload={"run_id": "run-1"},
        run_id="run-1", state_dir=store.root,
    )
    assert stop is not None
    control.begin_executor(task, action_id=stop.action_id, run_id="run-1")
    stopped = dict(original)
    prepare_action_application_receipt(stopped, stop.action)
    save_owned(store, "run-1", stopped, fields={'action_application_receipt'})
    requested = {**original, "supervision_window": {"retry_count": 1}}
    with pytest.raises(ConcurrentRunStateError, match="action_application_receipt"):
        save_owned(store, "run-1", requested, fields={"supervision_window"}, expected=original)
    assert store.load_run("run-1")["supervision_window"] == {"retry_count": 0}  # type: ignore[index]
    observations = []
    def observe(state: dict[str, object]) -> None:
        assert not store._repository.in_transaction
        assert store._lock_depth == 0
        assert store.load_run("run-1")["delivery_cleanup"] == state["delivery_cleanup"]  # type: ignore[index]
        observations.append(state)
    store.run_saved_observer = observe
    save_owned(store, "run-1", {"delivery_cleanup": {"completed": True}}, fields={"delivery_cleanup"})
    assert observations[0]["run_id"] == "run-1"
    assert observations[0]["status"] == "waiting_external"
    assert "agent_invocation_history" not in observations[0]


def test_old_sqlite_protocol_is_rejected_without_migration(tmp_path: Path) -> None:
    from agent_run.state_errors import IncompatibleRunStateError
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"phase": "developing"})
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE schema_meta SET value='sqlite-run-objects-v1' WHERE key='protocol'")
    with pytest.raises(IncompatibleRunStateError, match="protocol"):
        repository.load_objects("run-1")
    with pytest.raises(IncompatibleRunStateError, match="protocol"):
        repository.save_objects("run-1", {"phase": "candidate"}, fields={"phase"})
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == "sqlite-run-objects-v1"
        assert connection.execute("SELECT value_json FROM run_objects WHERE object_path=?", ('["phase"]',)).fetchone()[0] == '"developing"'


@pytest.mark.parametrize("claim", ["current_reference", "attempt_binding"])
def test_corrupted_history_claim_is_rejected_without_overwriting_archives(tmp_path: Path, claim: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    first = _invocation(0, resume_id="resume-first")
    second = _invocation(1, resume_id="resume-second")
    original = {"phase": "developing", "active_agent_invocation": first,
                "resume_audit": {"history": [{"resume_id": "resume-first"}]},
                "agent_invocation_history": [first, second]}
    repository.save("run-1", original)
    table = "work_invocation_current" if claim == "current_reference" else "work_invocations"
    column = "started_at" if claim == "current_reference" else "attempt_version_id"
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute(f"UPDATE {table} SET {column}='foreign identity' WHERE run_id=? AND started_at=?",
                                    ("run-1", first["started_at"]))
        assert cursor.rowcount == 1
        before = list(connection.execute(f"SELECT * FROM {table} ORDER BY started_at"))
        archived = list(connection.execute("SELECT * FROM work_invocation_history ORDER BY position"))
    with pytest.raises(ValueError, match="Missing Agent Invocation|Missing Semantic Attempt"):
        repository.load_objects("run-1")
    updated = {**first, "error": "observed after return"}
    with pytest.raises(ValueError, match="Missing Agent Invocation|Missing Semantic Attempt"):
        repository.save_objects("run-1", {"phase": "candidate", "agent_invocation_history": [updated]},
                                fields={"phase", "agent_invocation_history"})
    assert repository.load_fields("run-1", {"phase"}) == {"phase": "developing"}
    with sqlite3.connect(repository.path) as connection:
        assert list(connection.execute(f"SELECT * FROM {table} ORDER BY started_at")) == before
        assert list(connection.execute("SELECT * FROM work_invocation_history ORDER BY position")) == archived


@pytest.mark.parametrize("damage", ["wrong_size", "missing_proof"])
def test_damaged_unmodified_field_size_cannot_bypass_run_limit(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=512)
    original = {"unchanged": "x" * 400, "current": "a"}
    repository.save("run-1", original)
    with sqlite3.connect(repository.path) as connection:
        if damage == "wrong_size":
            connection.execute("UPDATE run_object_index SET logical_bytes=1 WHERE object_path=?", ('["unchanged"]',))
        else:
            connection.execute("DELETE FROM run_object_index WHERE object_path=?", ('["unchanged"]',))
    with pytest.raises(ValueError, match="field size proof"):
        repository.save_objects("run-1", {"current": "y" * 400}, fields={"current"})
    assert SQLiteStateRepository(tmp_path, max_bytes=512).load("run-1") == original


@pytest.mark.parametrize("damaged_owner", ['["current"]', '["run_archive",0]'])
def test_damaged_dependency_index_cannot_collect_material_used_by_archive(tmp_path: Path, damaged_owner: str) -> None:
    import hashlib

    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    shared = "shared" * 256
    original = {"current": shared, "run_archive": [shared],
                "agent_invocation_history": [_invocation(0, evidence=shared)]}
    repository.save("run-1", original)
    identity = hashlib.sha256(shared.encode()).hexdigest()
    with sqlite3.connect(repository.path) as connection:
        connection.execute("DELETE FROM run_object_materials WHERE object_path=? AND material_id=?", (damaged_owner, identity))
    with pytest.raises(ValueError, match="material dependency count"):
        repository.save_objects("run-1", {"current": "replacement"}, fields={"current"})
    assert SQLiteStateRepository(tmp_path, max_bytes=4096).load("run-1") == original
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT value FROM run_materials WHERE material_id=?", (identity,)).fetchone()[0] == shared
        assert connection.execute("SELECT 1 FROM work_evidence_materials WHERE instr(body,?)>0", (shared,)).fetchone()


def test_related_ticket_projection_and_patch_never_decode_sibling(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=16 * 1024)
    sibling_evidence = "sibling" * 256
    job = {"ticket_number": 2, "phase": "developing"}
    original = {"active_ticket_job": job, "ticket_jobs": {
        "2": job, "3": {"ticket_number": 3, "phase": "completed", "review_context": {"body": sibling_evidence}}},
        "ticket_agent_invocations": {}, "retired_ticket_generations": {}, "pending_ticket_retirements": {},
        "agent_invocation_history": []}
    repository.save("run-1", original)
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute("UPDATE work_evidence_materials SET body='broken' WHERE instr(body,?)>0", (sibling_evidence,))
        assert cursor.rowcount == 1
    projection, _ = repository.load_objects("run-1", related_tickets=True)  # type: ignore[misc]
    assert projection["ticket_jobs"] == {"2": job}
    updated = {**job, "phase": "candidate"}
    repository.save_objects("run-1", {"ticket_jobs": {"2": updated}}, fields={"ticket_jobs"},
                            keyed_changes={"ticket_jobs": {"2"}})
    assert repository.load_paths("run-1", {("ticket_jobs", "2")}) == {"ticket_jobs": {"2": updated}}
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE work_evidence_materials SET body=? WHERE body='broken'",
                           ('{"body":"' + sibling_evidence + '"}',))
    assert repository.load("run-1") == {**original, "active_ticket_job": updated,
                                         "ticket_jobs": {**original["ticket_jobs"], "2": updated}}


def test_append_archive_reads_only_new_records_and_rolls_back_on_size_limit(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=16 * 1024)
    old_evidence = "old" * 512
    original = {"retired_job_generations": [{"generation": 1, "review_context": {"body": old_evidence}}], "current": "kept"}
    repository.save("run-1", original)
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute("UPDATE work_evidence_materials SET body='broken' WHERE instr(body,?)>0", (old_evidence,))
        assert cursor.rowcount == 1
    repository.append_array("run-1", "retired_job_generations", [{"generation": 2}])
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE work_evidence_materials SET body=? WHERE body='broken'",
                           ('{"body":"' + old_evidence + '"}',))
    expected = {**original, "retired_job_generations": [*original["retired_job_generations"], {"generation": 2}]}
    assert repository.load("run-1") == expected
    with pytest.raises(ValueError, match="Work evidence exceeds .* byte limit"):
        repository.append_array("run-1", "retired_job_generations",
                                [{"generation": 3, "review_context": {"body": "x" * (4 * 1024 * 1024)}}])
    assert SQLiteStateRepository(tmp_path, max_bytes=16 * 1024).load("run-1") == expected


@pytest.mark.parametrize("changes", [{"3"}, {"2", "3"}])
def test_keyed_children_delete_add_and_size_match_complete_view(tmp_path: Path, changes: set[str]) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"},
                                "3": {"ticket_number": 3, "phase": "completed"}}}
    repository.save("run-1", original)
    repository.save_objects("run-1", {"ticket_jobs": {}}, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": changes})
    remaining = {key: value for key, value in original["ticket_jobs"].items() if key not in changes}
    assert repository.load("run-1") == {"ticket_jobs": remaining}
    new_job = {"ticket_number": 4, "phase": "developing"}
    repository.save_objects("run-1", {"ticket_jobs": {"4": new_job}}, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"4"}})
    expected = {"ticket_jobs": {**remaining, "4": new_job}}
    assert repository.load("run-1") == expected
    assert repository.load_objects("run-1")[0]["ticket_jobs"] == expected["ticket_jobs"]  # type: ignore[index]


def test_related_projection_includes_int_holder_without_work_queue_and_scheduler_generations(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    job = {"ticket_number": 2, "phase": "blocked"}
    original = {"active_ticket_job": job,
                "ticket_integration_holder": 3, "ticket_work_queue": [], "ticket_integration_queue": [],
                "ticket_jobs": {"2": job, "3": {"ticket_number": 3, "phase": "accepted"}},
                "ticket_agent_invocations": {}, "retired_ticket_generations": {"2": 1, "3": 2, "4": 3},
                "pending_ticket_retirements": {}, "agent_invocation_history": []}
    repository.save("run-1", original)
    related, _ = repository.load_objects("run-1", related_tickets=True)  # type: ignore[misc]
    assert related["ticket_jobs"] == original["ticket_jobs"]
    assert related["retired_ticket_generations"] == {"2": 1, "3": 2}
    scheduler, _ = repository.load_objects("run-1", scheduler_tickets=True)  # type: ignore[misc]
    assert scheduler["retired_ticket_generations"] == original["retired_ticket_generations"]


def _detailed_invocation(
    position: int, *, subject: str = "parent-only:run-1", context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta
    start = datetime(2026, 10, 13, tzinfo=UTC) + timedelta(seconds=position * 3)
    end = start + timedelta(seconds=1)
    return _invocation(position, work_subject=subject, review_context=context or {"accepted": True, "position": position},
                        status="completed", started_at=start.isoformat(), reported_thread_id=f"thread-{position}",
                        execution_segments=[{"started_at": start.isoformat(), "ended_at": end.isoformat()}],
                        recovery_wait_intervals=[{"started_at": end.isoformat(), "ended_at": (end + timedelta(seconds=1)).isoformat()}])


def test_incremental_retention_keeps_recent_256_and_compacts_released_canonical(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    history = [_detailed_invocation(position) for position in range(256)]
    repository.save("run-1", {"agent_invocation_history": history, "active_agent_invocation": history[0]})
    repository.append_array("run-1", "agent_invocation_history", [_detailed_invocation(256)])
    repository.append_array("run-1", "agent_invocation_history", [_detailed_invocation(257)])
    saved = repository.load("run-1")
    assert saved is not None
    assert saved["agent_invocation_history"][0]["execution_segments"]
    assert saved["agent_invocation_history"][1]["execution_segments"] == []
    assert saved["agent_invocation_history"][1]["execution_segments_summary"]["count"] == 1
    assert saved["agent_invocation_history"][1]["execution_timing_summary"]["execution_seconds"] == 1
    assert all(item["execution_segments"] for item in saved["agent_invocation_history"][-256:])
    repository.save_objects("run-1", {"active_agent_invocation": _detailed_invocation(257)}, fields={"active_agent_invocation"})
    reopened = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024).load("run-1")
    assert reopened is not None
    assert reopened["agent_invocation_history"][0]["execution_segments"] == []
    assert reopened["agent_invocation_history"][0]["semantic_attempt"]["review_context"] == {"accepted": True, "position": 0}
    assert all(item["execution_segments"] for item in reopened["agent_invocation_history"][-256:])


@pytest.mark.parametrize("subject,owner_path", [
    ("parent-only:run-1", ("parent_job",)),
    ("run-acceptance:run-1", ("run_acceptance",)),
    ("run-integration:run-1", ("run_acceptance",)),
    ("run-repair:run-1", ("run_acceptance", "repair_job")),
    ("run-publication:run-1", ("run_publication",)),
    ("ticket:2", ("ticket_jobs", "2")),
])
def test_incremental_retention_protects_pending_predecessors_until_attempt_consumed(
    tmp_path: Path, subject: str, owner_path: tuple[str, ...],
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    history = [_detailed_invocation(position, subject=subject, context={"evidence": "original"})
               if position < 2 else _detailed_invocation(position) for position in range(256)]
    history[0]["status"] = "failed"
    pending: dict[str, Any] = {"pending_semantic_attempt": deepcopy(history[0]["semantic_attempt"])}
    if owner_path[0] == "ticket_jobs":
        pending["ticket_number"] = 2
    owner = pending if len(owner_path) == 1 else {owner_path[1]: pending}
    repository.save("run-1", {"agent_invocation_history": history, "active_agent_invocation": history[0],
                              owner_path[0]: owner})
    repository.append_array("run-1", "agent_invocation_history", [_detailed_invocation(256), _detailed_invocation(257)])
    repository.save_objects("run-1", {"active_agent_invocation": _detailed_invocation(257)}, fields={"active_agent_invocation"})
    assert all(item["execution_segments"] for item in repository.load("run-1")["agent_invocation_history"][:2])  # type: ignore[index]
    released = {} if len(owner_path) == 1 else {owner_path[1]: {"ticket_number": 2} if owner_path[0] == "ticket_jobs" else {}}
    keyed_changes = {"ticket_jobs": {"2"}} if owner_path[0] == "ticket_jobs" else {}
    repository.save_objects("run-1", {owner_path[0]: released}, fields={owner_path[0]}, keyed_changes=keyed_changes)
    saved = repository.load("run-1")
    assert saved is not None
    assert all(item["execution_segments"] == [] for item in saved["agent_invocation_history"][:2])
    assert saved["agent_invocation_history"][0]["semantic_attempt"]["review_context"] == {"evidence": "original"}
    assert all(item["execution_segments"] for item in saved["agent_invocation_history"][-256:])


def test_object_commit_distinguishes_missing_fields_from_explicit_null(git_repo: Path) -> None:
    from agent_run.object_state import object_store
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.state import StateStore
    from support.run_agents import ScriptedRunAgents
    from support.run_scenarios import _completed_run

    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    RunAcceptanceEngine(git=git, states=states, agents=ScriptedRunAgents())._run_state(state)
    state["scope_change"] = None
    persist_run_fixture(states, run_id, state)
    adapter = object_store(states)
    staged = adapter.load_current_run(run_id)
    assert staged is not None
    assert staged.pop("scope_change") is None
    assert staged["run_acceptance"].pop("development_thread_id") is None
    adapter.save_run(run_id, staged)
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened is not None
    assert "scope_change" not in reopened
    assert "development_thread_id" not in reopened["run_acceptance"]
    staged = adapter.load_current_run(run_id)
    assert staged is not None
    staged["scope_change"] = None
    staged["run_acceptance"]["development_thread_id"] = None
    adapter.save_run(run_id, staged)
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened is not None
    assert "scope_change" in reopened and reopened["scope_change"] is None
    assert "development_thread_id" in reopened["run_acceptance"]
    assert reopened["run_acceptance"]["development_thread_id"] is None


@pytest.mark.parametrize("nested,initially_present", [(False, False), (False, True), (True, False), (True, True)])
def test_null_field_change_preserves_concurrent_owner_fact(
    git_repo: Path, nested: bool, initially_present: bool,
) -> None:
    from agent_run.object_state import object_store
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.state import ConcurrentRunStateError, StateStore
    from support.run_agents import ScriptedRunAgents
    from support.run_scenarios import _completed_run

    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    RunAcceptanceEngine(git=git, states=states, agents=ScriptedRunAgents())._run_state(state)
    field = "development_thread_id" if nested else "scope_change"
    owner = state["run_acceptance"] if nested else state
    owner.pop(field, None)
    if initially_present:
        owner[field] = None
    persist_run_fixture(states, run_id, state)
    adapter = object_store(states)
    staged = adapter.load_current_run(run_id)
    assert staged is not None
    owner = staged["run_acceptance"] if nested else staged
    if initially_present:
        owner.pop(field)
    else:
        owner[field] = None
    concurrent = StateStore(states.root).load_current_run(run_id)
    assert concurrent is not None
    owner = concurrent["run_acceptance"] if nested else concurrent
    owner[field] = "concurrent-thread" if nested else {"reason": "concurrent"}
    save_owned(states, run_id, concurrent, fields={'run_acceptance', 'scope_change'})
    with pytest.raises(ConcurrentRunStateError, match="changed before commit"):
        adapter.save_run(run_id, staged)
    assert StateStore(states.root).load_current_run(run_id) == concurrent


def test_selected_history_batch_preserves_positions_and_independent_evidence(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    history = [_invocation(number, resume_id=f"resume-{number}",
                           review_context={"body": "shared evidence " * 100, "feedback": ["keep"]})
               for number in range(260)]
    original = {"phase": "active", "agent_invocation_history": history,
                "resume_audit": {"history": [{"resume_id": entry["resume_id"]} for entry in history]}}
    repository.save("run-1", original)
    loaded, positions = repository.load_objects("run-1")
    assert loaded == original
    assert set(positions.values()) == set(range(260))
    loaded["agent_invocation_history"][0]["semantic_attempt"]["review_context"]["feedback"].append("caller change")
    assert loaded["agent_invocation_history"][1]["semantic_attempt"]["review_context"]["feedback"] == ["keep"]
    assert SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024).load_objects("run-1")[0] == original


@pytest.mark.parametrize("column", ["current_reference", "attempt_identity", "attempt_version"])
def test_batched_selected_history_rejects_each_actual_identity_claim(tmp_path: Path, column: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    history = [_invocation(number, work_subject=f"ticket:{number + 1}", resume_id=f"resume-{number}") for number in range(3)]
    original = {"phase": "active", "agent_invocation_history": history,
                "ticket_agent_invocations": {str(number + 1): item for number, item in enumerate(history)},
                "resume_audit": {"history": [{"resume_id": entry["resume_id"]} for entry in history]}}
    repository.save("run-1", original)
    assert repository.load_objects("run-1")[0] == original
    table, field, key_column, key = {
        "current_reference": ("work_invocation_current", "started_at", "reference_key", "2"),
        "attempt_identity": ("work_attempts", "role", "attempt_id", history[1]["semantic_attempt"]["attempt_id"]),
        "attempt_version": ("work_invocations", "attempt_version_id", "started_at", history[1]["started_at"]),
    }[column]
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute(f"UPDATE {table} SET {field}=? WHERE run_id='run-1' AND {key_column}=?", ("foreign identity", key))
        assert cursor.rowcount == 1
    with pytest.raises(ValueError, match="Missing Agent Invocation|Missing Semantic Attempt|Invalid Semantic Attempt size|identity"):
        repository.load_objects("run-1")
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(f"SELECT {field} FROM {table} WHERE run_id='run-1' AND {key_column}=?", (key,)).fetchone() == ("foreign identity",)
