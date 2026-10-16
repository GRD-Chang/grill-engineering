"""明确领域记录的事务、一致读取、材料与首次创建边界。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.object_state import save_owned
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_work_evidence import MAX_EVIDENCE_BYTES
from agent_run.state import StateStore
from agent_run.state_errors import IncompatibleRunStateError


def _repository(path: Path) -> SQLiteStateRepository:
    return SQLiteStateRepository(path)


def test_transaction_reads_are_isolated_and_follow_writes_and_rollback(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = {"status": "active", "parent_job": {"phase": "developing", "modification_attempts": 1}}
    replacement = {"status": "completed", "parent_job": {"phase": "completed", "modification_attempts": 2}}
    repository.save("run-1", original)
    with pytest.raises(OSError, match="rollback"):
        with repository.atomic():
            first = repository.load("run-1")
            first["parent_job"]["phase"] = "caller mutation"
            assert repository.load("run-1") == original
            repository.save_objects("run-1", replacement, fields=set(replacement))
            assert repository.load("run-1") == replacement
            raise OSError("rollback")
    assert repository.load("run-1") == original
    _repository(tmp_path).save_objects("run-1", replacement, fields=set(replacement))
    with repository.atomic():
        assert repository.load("run-1") == replacement


def test_caught_nested_failure_rolls_back_the_entire_transaction(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"status": "created"})
    with pytest.raises(RuntimeError, match="failed nested operation"):
        with repository.atomic():
            assert repository.load("run-1") == {"status": "created"}
            try:
                with repository.atomic():
                    repository.save_objects("run-1", {"status": "active"}, fields={"status"})
                    assert repository.load("run-1") == {"status": "active"}
                    raise OSError("inner failure")
            except OSError:
                pass
    assert repository.load("run-1") == {"status": "created"}


def test_material_limit_is_independent_of_expanded_query_aliases(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    context = {"body": "原始证据" * 30000}
    job = {"ticket_number": 2, "review_context": context}
    original = {"active_ticket_job": job, "ticket_jobs": {"2": job}}
    repository.save("run-1", original)
    assert _repository(tmp_path).load("run-1") == original
    expected_bytes = len(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    assert expected_bytes > 256 * 1024
    with closing(sqlite3.connect(repository.path)) as connection:
        assert connection.execute("SELECT byte_length FROM work_evidence_materials").fetchall() == [(expected_bytes,)]
    oversized = {"ticket_jobs": {"2": {"ticket_number": 2, "review_context": {"body": "x" * MAX_EVIDENCE_BYTES}}}}
    with pytest.raises(ValueError, match="Work evidence exceeds"):
        repository.save_objects("run-1", oversized, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repository.load("run-1") == original


def test_material_replacement_preserves_other_run_references(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    material = "same exact evidence " * 100
    for run_id in ("run-1", "run-2"):
        repository.save(run_id, {"parent": {"body": material}})
    repository.save_objects("run-1", {"parent": {"body": "replaced"}}, fields={"parent"})
    assert repository.load("run-2") == {"parent": {"body": material}}
    repository.save_objects("run-2", {"parent": {"body": "also replaced"}}, fields={"parent"})
    assert _repository(tmp_path).load("run-1") == {"parent": {"body": "replaced"}}
    assert repository.load("run-2") == {"parent": {"body": "also replaced"}}


def test_relational_projection_can_be_read_and_updated_as_keyed_objects(tmp_path: Path) -> None:
    state = {"ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing", "review_context": {
        "body": "exact evidence " * 100, "revision": "original",
    }}}}
    repository = _repository(tmp_path)
    repository.save("run-1", state)
    state["ticket_jobs"]["2"]["phase"] = "candidate"
    _repository(tmp_path).save_objects("run-1", state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repository.load("run-1") == state


@pytest.mark.parametrize("scope", ["create", "root", "job"])
def test_unsupported_core_fields_fail_without_replacing_existing_work(tmp_path: Path, scope: str) -> None:
    repository = _repository(tmp_path)
    original = {"status": "active", "parent_job": {"phase": "developing"}}
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="Unsupported"):
        if scope == "create":
            repository.save("run-2", {"unregistered_fact": {"value": 1}})
        elif scope == "root":
            repository.save_objects("run-1", {"status": "completed", "unregistered_fact": 1},
                                    fields={"status", "unregistered_fact"})
        else:
            repository.save_objects("run-1", {"parent_job": {"phase": "completed", "unregistered_fact": 1}},
                                    fields={"parent_job"})
    assert not repository.has("run-2")
    assert repository.load("run-1") == original


def test_previous_sqlite_protocol_is_rejected_without_rewriting(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"phase": "developing"})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute("UPDATE schema_meta SET value='sqlite-task-run-v3' WHERE key='protocol'")
        before = tuple(connection.iterdump())
    with pytest.raises(IncompatibleRunStateError):
        _repository(tmp_path).load("run-1")
    with closing(sqlite3.connect(repository.path)) as connection:
        assert tuple(connection.iterdump()) == before


def test_observer_receives_isolated_durable_facts(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = {"run_id": "run-1", "parent_job": {"review_context": {"checks": ["original"]}},
             "blocked_reason": "token=private-value"}
    observations = []
    def observe(saved):
        observations.append(saved["blocked_reason"])
        saved["parent_job"]["review_context"]["checks"].append("observer mutation")
        saved["timeline"].clear()
    store.run_saved_observer = observe
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert observations == ["token=[REDACTED]"]
    assert persisted["parent_job"] == state["parent_job"] == {"review_context": {"checks": ["original"]}}
    assert persisted["timeline"] == state["timeline"] and persisted["timeline"]


def test_retained_owner_keeps_materials_when_sibling_changes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    context = {"body": "[original exact evidence] " * 100, "literal": {"tags": ["snapshot", "not-a-reference"]}}
    state = {"ticket_jobs": {str(number): {"ticket_number": number, "phase": "reviewing", "review_context": deepcopy(context)}
                             for number in (2, 3)}}
    repository.save("run-1", state)
    with repository.atomic():
        updated = repository.load("run-1")
        updated["ticket_jobs"]["3"]["review_context"] = {"body": "replacement " * 200}
        repository.save_objects("run-1", updated, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"3"}})
        assert repository.load("run-1") == updated
    saved = _repository(tmp_path).load("run-1")
    assert saved == updated
    assert saved["ticket_jobs"]["2"]["review_context"] == context


@pytest.mark.parametrize("old,new", [(1, True), (True, 1), (0, False), (False, 0),
                                     (1, 1.0), (1.0, 1), (0.0, -0.0)])
def test_owned_material_preserves_exact_json_types_and_values(tmp_path: Path, old, new) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"ticket_jobs": {"2": {"ticket_number": 2, "review_context": {"value": old}}}})
    with repository.atomic():
        state = repository.load("run-1")
        state["ticket_jobs"]["2"]["review_context"]["value"] = new
        repository.save_objects("run-1", state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
        assert json.dumps(repository.load("run-1")) == json.dumps(state)
    value = _repository(tmp_path).load("run-1")["ticket_jobs"]["2"]["review_context"]["value"]
    assert type(value) is type(new)
    assert json.dumps(value) == json.dumps(new)


def test_observer_and_reopened_store_agree_on_changed_material_type(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = {"parent_job": {"review_context": {"value": 1}}}
    store.save_run("run-1", state)
    observed = []
    store.run_saved_observer = observed.append
    state["parent_job"]["review_context"]["value"] = True
    save_owned(store, "run-1", state, fields={"parent_job"})
    assert StateStore(tmp_path).load_run("run-1")["parent_job"]["review_context"]["value"] is True
    assert observed[0]["parent_job"]["review_context"]["value"] is True


@pytest.mark.parametrize("loaded", [False, True])
def test_complete_save_rejects_existing_run_before_processing_input(tmp_path: Path, loaded: bool) -> None:
    repository = _repository(tmp_path)
    original = {"phase": "created", "parent": {"body": "original evidence " * 100}}
    repository.save("run-1", original)
    with pytest.raises(ValueError, match="already exists"):
        with repository.atomic():
            if loaded:
                assert repository.load("run-1") == original
            repository.save("run-1", {"parent_job": {"review_context": {"not-json"}}})
    assert not repository.in_transaction
    assert _repository(tmp_path).load("run-1") == original


def test_competing_complete_creations_preserve_one_durable_run(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2, timeout=5)
    candidates = [{"status": status} for status in ("created", "active")]
    def create(state):
        repository = _repository(tmp_path)
        barrier.wait()
        try:
            repository.save("run-1", state)
            return state
        except ValueError as error:
            assert "already exists" in str(error)
            return None
        finally:
            assert not repository.in_transaction
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(create, candidate) for candidate in candidates]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert _repository(tmp_path).load("run-1") == winners[0]
