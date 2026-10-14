"""A Stop observation reads only its own intent and validates that snapshot."""
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import subprocess
import sys

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.ticket_state import TicketState
from agent_run.sqlite_state import SQLiteStateRepository, _WORK_SCHEMA_PARAMETERS
from agent_run.sqlite_ticket_pause_read import read_ticket_pause


@pytest.mark.parametrize("timestamp", ["2026-10-13T01:02:03+00:00", "retained" * 200])
def test_ticket_pause_query_reopens_reads_latest_intent_and_ignores_unrelated_material(
    tmp_path: Path, timestamp: str,
) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    state = {"ticket_pause_requests": {}, "review_context": {"body": "unrelated" * 200}}
    repository.save("run-1", state)
    lane = TicketState(states, "run-1", 2)
    assert lane.ticket_pause_requested("run-1", 2) is False
    state["ticket_pause_requests"] = {"2": timestamp, "3": "sibling"}
    repository.save("run-1", state)
    assert StateStore(tmp_path).ticket_pause_requested("run-1", 2) is True
    assert lane.ticket_pause_requested("run-1", 2) is True
    state["ticket_pause_requests"] = {"3": "sibling"}
    repository.save("run-1", state)
    assert lane.ticket_pause_requested("run-1", 2) is False
    with repository.atomic():
        connection = repository._local.connection
        # Actual unrelated corruption proves the Stop query never decodes it.
        connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        assert lane.ticket_pause_requested("run-1", 2) is False
        with pytest.raises(ValueError, match="material"):
            repository.load("run-1")
    with pytest.raises(ValueError, match="another Ticket"):
        lane.ticket_pause_requested("run-1", 3)


@pytest.mark.parametrize("damage", [
    "missing_run", "missing_size", "oversize", "missing_map", "map_kind", "map_value",
    "timestamp_type", "timestamp_kind", "child", "protocol", "schema",
    "material_length", "material_identity", "material_reference",
])
def test_ticket_pause_query_rejects_damaged_current_metadata_and_target(
    tmp_path: Path, damage: str,
) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    timestamp = "retained" * 200 if damage.startswith("material_") else "timestamp"
    repository.save("run-1", {"ticket_pause_requests": {"2": timestamp}})
    root = encode_json(("ticket_pause_requests",))
    target = encode_json(("ticket_pause_requests", "2"))
    with repository.atomic():
        connection = repository._local.connection
        assert states.ticket_pause_requested("run-1", 2)
        if damage == "missing_run":
            connection.execute("DELETE FROM runs WHERE run_id='run-1'")
        elif damage == "missing_size":
            connection.execute("DELETE FROM run_sizes WHERE run_id='run-1'")
        elif damage == "oversize":
            connection.execute("UPDATE run_sizes SET logical_bytes=?", (MAX_RUN_STATE_BYTES + 1,))
        elif damage == "missing_map":
            connection.execute("DELETE FROM run_objects WHERE object_path=?", (root,))
        elif damage in {"map_kind", "map_value"}:
            kind, value = ("scalar", '"bad"') if damage == "map_kind" else ("map", "false")
            connection.execute("UPDATE run_objects SET kind=?,value_json=? WHERE object_path=?", (kind, value, root))
        elif damage in {"timestamp_type", "timestamp_kind"}:
            kind, value = ("scalar", "42") if damage == "timestamp_type" else ("list", "0")
            connection.execute("UPDATE run_objects SET kind=?,value_json=? WHERE object_path=?", (kind, value, target))
        elif damage == "child":
            child = encode_json(("ticket_pause_requests", "2", "child"))
            connection.execute("INSERT INTO run_objects VALUES ('run-1',?,'scalar','null')", (child,))
        elif damage == "protocol":
            connection.execute("UPDATE schema_meta SET value='unsupported'")
        elif damage == "schema":
            connection.execute("CREATE VIEW unexpected AS SELECT 1")
        elif damage == "material_length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "material_identity":
            connection.execute("UPDATE run_materials SET value=replace(value,'retained','replaced')")
        else:
            connection.execute("DELETE FROM run_object_material_refs WHERE object_path=?", (target,))
        with pytest.raises(ValueError):
            states.ticket_pause_requested("run-1", 2)


def test_ticket_pause_query_missing_storage_does_not_create_or_repair_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(ValueError, match="disappeared"):
        StateStore(root).ticket_pause_requested("run-1", 2)
    assert not root.exists()


@pytest.mark.parametrize("requested", [False, True])
def test_scalar_pause_native_is_one_fresh_query_and_matches_original(tmp_path: Path, requested: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", {"ticket_pause_requests": {"2": "timestamp"} if requested else {"3": "sibling"}})
    with closing(sqlite3.connect(repository.path)) as connection:
        queries = []
        connection.set_trace_callback(queries.append)
        assert read_ticket_pause(connection, "run-1", 2, max_bytes=65536,
                                 schema_parameters=_WORK_SCHEMA_PARAMETERS) is requested
        assert len(queries) == 1
        connection.set_trace_callback(None)
    repository._aggregate_rows = False
    assert repository.ticket_pause_requested("run-1", 2) is requested
    repository._aggregate_rows = True
    assert repository.ticket_pause_requested("run-1", 2) is requested


def test_pause_native_sessions_read_other_process_commit(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", {"ticket_pause_requests": {}})
    source_root = str(Path(sys.modules[SQLiteStateRepository.__module__].__file__).parents[1])
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((source_root, os.environ.get("PYTHONPATH", "")))}
    with repository.work_session():
        assert repository.ticket_pause_requested("run-1", 2) is False
        result = subprocess.run([
            sys.executable, "-c",
            "from pathlib import Path; import sys; from agent_run.sqlite_state import SQLiteStateRepository; "
            "SQLiteStateRepository(Path(sys.argv[1]),max_bytes=65536).save('run-1',{'ticket_pause_requests':{'2':'child'}})",
            str(tmp_path),
        ], env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert repository.ticket_pause_requested("run-1", 2) is True
    assert SQLiteStateRepository(tmp_path, max_bytes=65536).ticket_pause_requested("run-1", 2) is True


def test_pause_writer_keeps_actual_schema_validation_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", {"ticket_pause_requests": {"2": "timestamp"}})
    validate = repository._check_schema
    observed = []

    def check(connection, **kwargs):
        observed.append(connection)
        return validate(connection, **kwargs)

    with repository.atomic():
        monkeypatch.setattr(repository, "_check_schema", check)
        assert repository.ticket_pause_requested("run-1", 2)
        assert observed == [repository._local.connection]


@pytest.mark.parametrize("damage", ["root", "value_blob", "invalid_json", "json_map", "map_extra_child",
                                   "size_blob", "size_real", "schema", "protocol"])
def test_pause_native_read_rejects_damage_like_original(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", {"ticket_pause_requests": {"2": "timestamp"}})
    target = encode_json(("ticket_pause_requests", "2"))
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "root":
            connection.execute("UPDATE run_objects SET value_json='false' WHERE object_path='[]'")
        elif damage == "value_blob":
            connection.execute("UPDATE run_objects SET value_json=CAST(value_json AS BLOB) WHERE object_path=?", (target,))
        elif damage in {"invalid_json", "json_map"}:
            connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", ("bad" if damage == "invalid_json" else '{}', target))
        elif damage == "map_extra_child":
            connection.execute("INSERT INTO run_objects VALUES ('run-1',?,'scalar','null')", (encode_json(("ticket_pause_requests", "2", "child")),))
        elif damage == "size_blob":
            connection.execute("UPDATE run_sizes SET logical_bytes=CAST(logical_bytes AS BLOB)")
        elif damage == "size_real":
            connection.execute("UPDATE run_sizes SET logical_bytes=logical_bytes+0.5")
        elif damage == "schema":
            connection.execute("CREATE VIEW unexpected AS SELECT 1")
        else:
            connection.execute("UPDATE schema_meta SET value='unsupported'")
        connection.commit()
    # BLOB object JSON is rejected by the existing aggregated fallback; retain
    # that original path rather than silently coercing SQL bytes to TEXT.
    with pytest.raises(ValueError):
        repository.ticket_pause_requested("run-1", 2)


@pytest.mark.parametrize("fallback", ["material", "missing_json", "noncanonical", "large"])
def test_pause_native_fallback_retains_original_reader(tmp_path: Path, fallback: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    timestamp = "材料" * 600 if fallback == "material" else "timestamp"
    repository.save("run-1", {"ticket_pause_requests": {"2": timestamp}})

    class MissingJson(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if "schema_definitions" in sql:
                raise sqlite3.OperationalError("no such function: json_group_array")
            return super().execute(sql, parameters)

    factory = MissingJson if fallback == "missing_json" else sqlite3.Connection
    with closing(sqlite3.connect(repository.path, factory=factory)) as connection:
        if fallback == "noncanonical":
            # A compatible alternative table DDL remains subject to the
            # original structural validator after canonical proof declines it.
            ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
            index_ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name='runs_parent'").fetchone()[0]
            run_rows = list(connection.execute("SELECT * FROM runs"))
            connection.execute("DROP TABLE runs")
            connection.execute(ddl.replace("CREATE TABLE runs (", "CREATE TABLE runs ( "))
            connection.executemany("INSERT INTO runs VALUES (" + ",".join("?" for _ in run_rows[0]) + ")", run_rows)
            connection.execute(index_ddl)
        elif fallback == "large":
            target = encode_json(("ticket_pause_requests", "2"))
            connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", (encode_json("L" * (512 * 1024)), target))
        connection.commit()
        assert read_ticket_pause(connection, "run-1", 2, max_bytes=repository.max_bytes,
                                 schema_parameters=_WORK_SCHEMA_PARAMETERS) is None
        with repository.use_reader(connection):
            assert repository.ticket_pause_requested("run-1", 2) is True
    assert repository.ticket_pause_requested("run-1", 2) is True


@pytest.mark.parametrize("node", ["map", "target"])
def test_pause_native_bounds_materialized_nodes_before_copying_oversized_value(tmp_path: Path, node: str) -> None:
    from agent_run.sqlite_ticket_pause_read import _PAUSE

    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    repository.save("run-1", {"ticket_pause_requests": {"2": "timestamp"}})
    root = encode_json(("ticket_pause_requests",))
    target = encode_json(("ticket_pause_requests", "2"))
    with closing(sqlite3.connect(repository.path)) as connection:
        path = target if node == "target" else root
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", (encode_json("X" * 250000), path))
        connection.commit()
        parameters = {**_WORK_SCHEMA_PARAMETERS, "run_id": "run-1", "map_path": root,
                      "target_path": target, "target_low": target[:-1] + ",", "target_high": target[:-1] + "-",
                      "aggregate_bytes": 8 * repository.max_bytes}
        # Force the real planner to materialize the shared nodes relation. Its
        # original numeric length remains, while the oversized body is absent.
        row = connection.execute(_PAUSE.replace("WITH nodes AS (", "WITH nodes AS MATERIALIZED ("), parameters).fetchone()
        assert row[3] > 8 * repository.max_bytes
        assert row[7 if node == "map" else 10] is None
        with pytest.raises(ValueError, match="persistence limit"):
            read_ticket_pause(connection, "run-1", 2, max_bytes=repository.max_bytes,
                              schema_parameters=_WORK_SCHEMA_PARAMETERS)
    with pytest.raises(ValueError, match="persistence limit"):
        repository.ticket_pause_requested("run-1", 2)
