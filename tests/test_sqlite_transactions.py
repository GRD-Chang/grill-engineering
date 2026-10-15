"""Transaction view isolation and the expanded logical persistence limit."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run import sqlite_state
from agent_run.state import StateStore
from agent_run.sqlite_rows import read_row_source, read_run_rows, read_schema
from agent_run.object_state import save_owned


def test_transaction_reads_are_isolated_and_follow_writes_and_rollback(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"job": {"phase": "developing"}, "history": ["started"]}
    replacement = {"job": {"phase": "candidate"}, "history": ["started", "returned"]}
    repository.save("run-1", original)

    with pytest.raises(OSError, match="before commit"):
        with repository.atomic():
            view = repository.load("run-1")
            assert view is not None
            view["job"]["phase"] = "caller mutation"
            view["history"].append("not saved")
            assert repository.load("run-1") == original
            repository.save_objects("run-1", replacement, fields={"job", "history"})
            assert repository.load("run-1") == replacement
            raise OSError("before commit")

    with repository.atomic():
        assert repository.load("run-1") == original
    other = SQLiteStateRepository(tmp_path, max_bytes=4096)
    other.save_objects("run-1", replacement, fields={"job", "history"})
    with repository.atomic():
        assert repository.load("run-1") == replacement


def test_caught_nested_failure_rolls_back_the_entire_transaction(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"version": 1})
    with pytest.raises(RuntimeError, match="failed nested operation"):
        with repository.atomic():
            assert repository.load("run-1") == {"version": 1}
            try:
                with repository.atomic():
                    repository.save_objects("run-1", {"version": 2}, fields={"version"})
                    assert repository.load("run-1") == {"version": 2}
                    raise OSError("inner failure")
            except OSError:
                pass
    assert repository.load("run-1") == {"version": 1}


@pytest.mark.parametrize("state", [
    {},
    {"转义\n\"": [True, None, -1, 1.25, "中文\t\\\"\n"], "tuple": (1, 2)},
    {"ticket_jobs": {str(number): {"review_context": {"body": "中文\n" * 400}}
                     for number in range(4)}},
])
def test_size_limit_counts_exact_utf8_json_including_expanded_materials(
    tmp_path: Path, state: dict,
) -> None:
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    size = len(encoded.encode("utf-8")) + 1
    repository = SQLiteStateRepository(tmp_path, max_bytes=size)
    repository.save("run-1", state)
    assert repository.load("run-1") == json.loads(encoded)
    limited = SQLiteStateRepository(tmp_path, max_bytes=size - 1)
    with pytest.raises(ValueError, match="persistence limit"):
        limited.load("run-1")
    with pytest.raises(ValueError, match="persistence limit"):
        limited.save("oversized-new-run", state)
    assert repository.load("run-1") == json.loads(encoded)


def test_material_reclamation_preserves_other_run_references(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    material = "same exact evidence " * 100
    for run_id in ("run-1", "run-2"):
        repository.save(run_id, {"evidence": material})
    repository.save_objects("run-1", {"evidence": "replaced"}, fields={"evidence"})
    assert repository.load("run-2") == {"evidence": material}
    repository.save_objects("run-2", {"evidence": "also replaced"}, fields={"evidence"})
    assert SQLiteStateRepository(tmp_path, max_bytes=8192).load("run-1") == {"evidence": "replaced"}
    assert repository.load("run-2") == {"evidence": "also replaced"}


def test_existing_node_projection_can_be_read_and_updated_as_keyed_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"ticket_jobs": {"2": {"phase": "developing", "review_context": {
        "body": "exact evidence " * 100, "revision": "original",
    }}}, "agent_invocation_history": [{"thread_id": "original-thread", "status": "running"}]}
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    # Exercise the earlier node layout under the same SQLite protocol. The
    # existing reader must work before any subsequent business transition.
    with monkeypatch.context() as earlier:
        earlier.setattr(sqlite_state, "_OBJECT_COLLECTIONS", set())
        earlier.setattr(sqlite_state, "_CURRENT_OBJECTS", set())
        repository.save("run-1", state)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=8192)
    assert reopened.load("run-1") == state
    state["ticket_jobs"]["2"]["phase"] = "candidate"
    reopened.save_objects("run-1", state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repository.load("run-1") == state


def test_observer_receives_isolated_durable_facts(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = {"run_id": "run-1", "extension": {"values": [1]},
             "error": "token=private-value", "agent_invocation_history": []}
    observations = []

    def observe(saved):
        observations.append(saved["error"])
        saved["extension"]["values"].append(2)
        saved["timeline"].clear()
        saved["agent_invocation_history"].append({"observer": "mutation"})

    store.run_saved_observer = observe
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert persisted is not None
    assert observations == ["token=[REDACTED]"]
    assert persisted["extension"] == state["extension"] == {"values": [1]}
    assert persisted["timeline"] == state["timeline"] and persisted["timeline"]
    assert persisted["agent_invocation_history"] == state["agent_invocation_history"] == []


@pytest.mark.parametrize("aggregate", [True, False])
def test_retained_object_keeps_transitive_materials_when_sibling_changes(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository._aggregate_rows = aggregate
    context = {"body": "[original exact evidence] " * 100,
               "literal": {"tags": ["snapshot", "not-a-reference"]}}
    state = {"ticket_jobs": {
        "2": {"phase": "reviewing", "review_context": context},
        "3": {"phase": "reviewing", "review_context": context},
    }}
    repository.save("run-1", state)
    with repository.atomic():
        updated = repository.load("run-1")
        assert updated is not None
        updated["ticket_jobs"]["3"]["review_context"] = {"body": "replacement " * 200}
        repository.save_objects("run-1", updated, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"3"}})
        assert repository.load("run-1") == updated
    saved = SQLiteStateRepository(tmp_path, max_bytes=65536).load("run-1")
    assert saved == updated
    assert saved["ticket_jobs"]["2"]["review_context"] == context


@pytest.mark.parametrize("damage", ["material_content", "material_length", "object_kind", "protocol",
                                   "columns", "foreign_keys", "trigger"])
def test_reused_projection_rechecks_actual_database_contents(tmp_path: Path, damage: str) -> None:
    from agent_run.state_errors import IncompatibleRunStateError

    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    material = "original evidence " * 100
    repository.save("run-1", {"evidence": material})
    assert repository.load("run-1") == {"evidence": material}
    assert repository.load("run-1") == {"evidence": material}
    # Damage disk facts after a successful repeated read, without changing
    # the Run ID, material ID or any timestamp that a cache might trust.
    with sqlite3.connect(repository.path) as connection:
        if damage == "material_content":
            identity = hashlib.sha256(material.encode()).hexdigest()
            connection.execute("UPDATE run_materials SET value = ? WHERE material_id = ?",
                               ("X" + material[1:], identity))
        elif damage == "material_length":
            connection.execute("UPDATE run_materials SET byte_length = byte_length + 1")
        elif damage == "object_kind":
            connection.execute("UPDATE run_objects SET kind = 'scalar' WHERE object_path = '[]'")
        elif damage == "protocol":
            connection.execute("UPDATE schema_meta SET value = 'incompatible'")
        elif damage == "columns":
            connection.execute("ALTER TABLE run_objects ADD COLUMN unexpected TEXT")
        elif damage == "foreign_keys":
            connection.execute("ALTER TABLE run_objects RENAME TO old_objects")
            connection.execute("CREATE TABLE run_objects (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
                               "kind TEXT NOT NULL, value_json TEXT NOT NULL, PRIMARY KEY (run_id, object_path))")
            connection.execute("INSERT INTO run_objects SELECT * FROM old_objects")
            connection.execute("DROP TABLE old_objects")
        else:
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON runs BEGIN SELECT 1; END")
    exception = (IncompatibleRunStateError if damage in {"protocol", "columns", "foreign_keys", "trigger"}
                 else ValueError)
    with pytest.raises(exception):
        repository.load("run-1")


@pytest.mark.parametrize("old,new", [(1, True), (True, 1), (0, False), (False, 0),
                                     (1, 1.0), (1.0, 1), (0.0, -0.0)])
def test_object_reuse_preserves_exact_json_types_and_values(tmp_path: Path, old, new) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    repository.save("run-1", {"ticket_jobs": {"2": {"nested": {"value": old}}}})
    with repository.atomic():
        state = repository.load("run-1")
        state["ticket_jobs"]["2"]["nested"]["value"] = new
        repository.save_objects("run-1", state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
        assert json.dumps(repository.load("run-1")) == json.dumps(state)
    saved = SQLiteStateRepository(tmp_path, max_bytes=8192).load("run-1")
    value = saved["ticket_jobs"]["2"]["nested"]["value"]
    assert type(value) is type(new)
    assert json.dumps(value) == json.dumps(new)


def test_observer_and_reopened_store_agree_on_changed_object_type(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = {"ticket_jobs": {"2": {"extension": {"value": 1}}}}
    store.save_run("run-1", state)
    observed = []
    store.run_saved_observer = observed.append
    state["ticket_jobs"]["2"]["extension"]["value"] = True
    save_owned(store, "run-1", state, fields={"ticket_jobs"})
    saved = StateStore(tmp_path).load_run("run-1")
    assert saved["ticket_jobs"]["2"]["extension"]["value"] is True
    assert observed[0]["ticket_jobs"]["2"]["extension"]["value"] is True


def test_native_and_missing_json_row_sources_preserve_actual_sql_values(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    body = '["snapshot","literal"]\n\x00中文' * 100
    repository.save("run-1", {"ticket_jobs": {"2": {"review_context": {"body": body}}}})
    with sqlite3.connect(repository.path) as connection:
        rows = read_row_source(connection, "run-1", aggregate=False)
        native = read_row_source(connection, "run-1", aggregate=True)
        assert native.material_rows() == rows.material_rows()
        assert native.object_rows() == rows.object_rows()

        class WithoutJson:
            def execute(self, sql, args=()):
                if "json_group_array" in sql:
                    raise sqlite3.OperationalError("no such function: json_group_array")
                return connection.execute(sql, args)

        fallback = read_row_source(WithoutJson(), "run-1", aggregate=True)
        assert fallback == rows
        guarded, _, _, supported = read_run_rows(
            WithoutJson(), "run-1", max_bytes=65536, aggregate_bytes=65536, aggregate=True,
        )
        assert guarded == rows and supported is False
        definitions, metadata = read_schema(WithoutJson())
        assert definitions == tuple(connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ))
        assert metadata is None
    assert repository.load("run-1")["ticket_jobs"]["2"]["review_context"]["body"] == body


def test_sibling_change_preserves_independent_snapshot_values(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    state = {"ticket_jobs": {
        "2": {"value": 1, "literal": {"tags": ["snapshot", "literal"]},
              "review_context": {"body": "original evidence " * 100}},
        "3": {"value": 0.0},
    }}
    repository.save("run-1", state)
    assert repository.load("run-1") == state
    other = SQLiteStateRepository(tmp_path, max_bytes=65536)
    state["ticket_jobs"]["3"]["value"] = -0.0
    other.save_objects("run-1", state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"3"}})
    changed = repository.load("run-1")
    assert json.dumps(changed, sort_keys=True) == json.dumps(state, sort_keys=True)
    changed["ticket_jobs"]["2"]["literal"]["tags"].clear()
    assert repository.load("run-1") == state
    with repository.atomic():
        changed = repository.load("run-1")
        changed["ticket_jobs"]["2"]["value"] = True
        repository.save_objects("run-1", changed, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert other.load("run-1")["ticket_jobs"]["2"]["value"] is True


@pytest.mark.parametrize("damage", ["body", "length", "reference"])
def test_unchanged_snapshot_rechecks_transitive_materials(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    body = "original evidence " * 100
    state = {"ticket_jobs": {"2": {"review_context": {"body": body}}}}
    repository.save("run-1", state)
    assert repository.load("run-1") == state
    identity = hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(repository.path) as connection:
        if damage == "body":
            connection.execute("UPDATE run_materials SET value = ? WHERE material_id = ?",
                               ("X" + body[1:], identity))
        elif damage == "length":
            connection.execute("UPDATE run_materials SET byte_length = byte_length + 1 WHERE material_id = ?",
                               (identity,))
        else:
            connection.execute("DELETE FROM run_material_refs WHERE material_id = ?", (identity,))
    with pytest.raises(ValueError, match="material"):
        repository.load("run-1")


def test_row_read_checks_bounds_before_aggregation(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    repository.save("run-1", {"body": "evidence " * 200})
    with sqlite3.connect(repository.path) as connection:
        def unexpected_aggregate(*args):
            raise AssertionError("oversized source reached aggregation")

        connection.create_function("json_array", -1, unexpected_aggregate)
        source, _, _, supported = read_run_rows(
            connection, "run-1", max_bytes=8192, aggregate_bytes=1, aggregate=True,
        )
        assert source == read_row_source(connection, "run-1", aggregate=False)
        assert supported is True
        with pytest.raises(ValueError, match="persistence limit"):
            read_run_rows(connection, "run-1", max_bytes=1, aggregate_bytes=65536, aggregate=True)
        assert read_run_rows(connection, "missing", max_bytes=1, aggregate_bytes=65536, aggregate=True)[0] is None


@pytest.mark.parametrize("loaded", [False, True])
def test_complete_save_rejects_existing_run_before_processing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loaded: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"phase": "created", "evidence": "original evidence " * 100}
    repository.save("run-1", original)

    def unexpected_encode(*args, **kwargs):
        raise AssertionError("existing Run entered complete encoding")

    with pytest.raises(ValueError, match="already exists"):
        with repository.atomic():
            if loaded:
                assert repository.load("run-1") == original
            monkeypatch.setattr(repository, "_objects", unexpected_encode)
            repository.save("run-1", {"phase": "replacement"})
    assert not repository.in_transaction
    assert SQLiteStateRepository(tmp_path, max_bytes=4096).load("run-1") == original


def test_competing_complete_creations_preserve_one_durable_run(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2, timeout=5)
    candidates = [{"winner": number} for number in (1, 2)]

    def create(state):
        repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
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
    assert SQLiteStateRepository(tmp_path, max_bytes=4096).load("run-1") == winners[0]
