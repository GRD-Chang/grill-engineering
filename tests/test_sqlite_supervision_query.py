"""Empty supervision prechecks preserve full validation for actual work."""
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.sqlite_state import _WORK_SCHEMA_PARAMETERS
from agent_run.sqlite_supervision_read import read_supervision_work


def _facts() -> dict:
    return {"status": "active", "ticket_integration_holder": None,
            "ticket_integration_queue": [], "review_context": {"body": "unrelated" * 200}}


@pytest.mark.parametrize("status", ["active", "ticket_delivery_pending", "operator_stopped", "unknown", "unknown" * 200])
def test_supervision_precheck_reopens_and_reads_latest_shared_facts(tmp_path: Path, status: str) -> None:
    states = StateStore(tmp_path)
    facts = _facts() | {"status": status}
    states._repository.save("run-1", facts)
    empty = status in {"active", "ticket_delivery_pending"}
    with states.work_session():
        assert states.has_ticket_supervision_work("run-1") is not empty
        updated = dict(facts, ticket_integration_holder=2)
        StateStore(tmp_path)._repository.save("run-1", updated)
        assert states.has_ticket_supervision_work("run-1")
        updated.update(ticket_integration_holder=None, ticket_integration_queue=[{"ticket_number": 2}])
        StateStore(tmp_path)._repository.save("run-1", updated)
        assert states.has_ticket_supervision_work("run-1")
        StateStore(tmp_path)._repository.save("run-1", facts)
        assert states.has_ticket_supervision_work("run-1") is not empty
    assert not states._repository.in_transaction


def test_empty_supervision_does_not_read_unrelated_material_or_repair_missing_run(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    states = StateStore(root)
    assert states.has_ticket_supervision_work("run-1") is False
    assert not root.exists()
    states._repository.save("run-1", _facts())
    assert states.has_ticket_supervision_work("absent") is False
    with states._repository.atomic():
        connection = states._repository._local.connection
        connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        connection.execute("UPDATE runs SET status='completed'")
        assert states.has_ticket_supervision_work("run-1") is False
        with pytest.raises(ValueError, match="material"):
            states._repository.load("run-1")


def test_empty_supervision_query_cost_does_not_scan_unrelated_object_paths(tmp_path: Path) -> None:
    states = StateStore(tmp_path)
    costs = []
    for count in (1, 1000):
        states._repository.save("run-1", _facts() | {"unrelated_history": [{"fact": n} for n in range(count)]})
        steps = [0]

        def progress() -> int:
            steps[0] += 1
            return 0

        with states._repository.atomic():
            connection = states._repository._local.connection
            connection.set_progress_handler(progress, 1)
            try:
                assert states.has_ticket_supervision_work("run-1") is False
            finally:
                connection.set_progress_handler(None, 0)
        costs.append(steps[0])
    assert costs[1] <= costs[0] + 64, costs


@pytest.mark.parametrize("damage", ["root", "missing_status", "status_kind", "status_type", "holder_kind",
    "holder_type", "queue_kind", "queue_length", "empty_child", "holder_child", "status_child",
    "missing_size", "oversize", "protocol", "schema"])
def test_supervision_precheck_rejects_invalid_shape_and_current_metadata(tmp_path: Path, damage: str) -> None:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    with states._repository.atomic():
        connection = states._repository._local.connection
        assert states.has_ticket_supervision_work("run-1") is False
        fields = {
            "root": ("[]", "scalar", "null"),
            "status_kind": (encode_json(("status",)), "map", "null"),
            "status_type": (encode_json(("status",)), "scalar", "42"),
            "holder_kind": (encode_json(("ticket_integration_holder",)), "map", "null"),
            "holder_type": (encode_json(("ticket_integration_holder",)), "scalar", "true"),
            "queue_kind": (encode_json(("ticket_integration_queue",)), "scalar", "0"),
            "queue_length": (encode_json(("ticket_integration_queue",)), "list", "-1"),
        }
        if damage in fields:
            path, kind, value = fields[damage]
            connection.execute("UPDATE run_objects SET kind=?,value_json=? WHERE object_path=?", (kind, value, path))
        elif damage == "missing_status":
            connection.execute("DELETE FROM run_objects WHERE object_path=?", (encode_json(("status",)),))
        elif damage in {"empty_child", "holder_child", "status_child"}:
            path = encode_json({"empty_child": ("ticket_integration_queue", 0),
                                "holder_child": ("ticket_integration_holder", "child"),
                                "status_child": ("status", "child")}[damage])
            connection.execute("INSERT INTO run_objects VALUES ('run-1',?,'map','null')", (path,))
        elif damage == "missing_size":
            connection.execute("DELETE FROM run_sizes")
        elif damage == "oversize":
            connection.execute("UPDATE run_sizes SET logical_bytes=?", (MAX_RUN_STATE_BYTES + 1,))
        elif damage == "protocol":
            connection.execute("UPDATE schema_meta SET value='unsupported'")
        else:
            connection.execute("CREATE VIEW unexpected AS SELECT 1")
        with pytest.raises(ValueError):
            states.has_ticket_supervision_work("run-1")


@pytest.mark.parametrize("override", [{}, {"status": "ticket_delivery_pending"}, {"status": "future"},
                                      {"status": "material" * 200}, {"ticket_integration_holder": 2},
                                      {"ticket_integration_queue": [{"ticket_number": 2}]}])
def test_supervision_native_one_query_matches_original_precheck(tmp_path: Path, override: dict) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    facts = _facts() | override
    repository.save("run-1", facts)
    repository._aggregate_rows = False
    expected = states.has_ticket_supervision_work("run-1")
    repository._aggregate_rows = True
    with closing(sqlite3.connect(repository.path)) as connection:
        queries = []
        connection.set_trace_callback(queries.append)
        assert read_supervision_work(connection, "run-1", max_bytes=repository.max_bytes,
                                     schema_parameters=_WORK_SCHEMA_PARAMETERS) is expected
        assert len(queries) == 1
    assert states.has_ticket_supervision_work("run-1") is expected


@pytest.mark.parametrize("field,value", [("ticket_integration_holder", "true"),
                                         ("ticket_integration_holder", "2.0"),
                                         ("ticket_integration_queue", "false"),
                                         ("ticket_integration_queue", "0.0")])
def test_supervision_native_numeric_types_cannot_be_coerced_to_int(tmp_path: Path, field: str, value: str) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    repository.save("run-1", _facts())
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", (value, encode_json((field,))))
        connection.commit()
    for native in (True, False):
        repository._aggregate_rows = native
        with pytest.raises(ValueError, match="status or queue"):
            states.has_ticket_supervision_work("run-1")


def test_supervision_native_guards_materialized_oversized_source(tmp_path: Path) -> None:
    from agent_run.sqlite_supervision_read import _SUPERVISION

    states = StateStore(tmp_path)
    repository = states._repository
    repository.save("run-1", _facts())
    repository.max_bytes = 8192
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?", (encode_json("X" * 250000), encode_json(("status",))))
        connection.commit()
        parameters = {**_WORK_SCHEMA_PARAMETERS, "run_id": "run-1", "aggregate_bytes": 8 * repository.max_bytes}
        for name, field in (("status", "status"), ("holder", "ticket_integration_holder"), ("queue", "ticket_integration_queue")):
            path = encode_json((field,))
            parameters.update({name: path, name + "_low": path[:-1] + ",", name + "_high": path[:-1] + "-"})
        row = connection.execute(_SUPERVISION.replace("WITH nodes AS (", "WITH nodes AS MATERIALIZED ("), parameters).fetchone()
        assert row[3] > 8 * repository.max_bytes and row[7] is None
        with pytest.raises(ValueError, match="persistence limit"):
            read_supervision_work(connection, "run-1", max_bytes=repository.max_bytes,
                                  schema_parameters=_WORK_SCHEMA_PARAMETERS)
    with pytest.raises(ValueError, match="persistence limit"):
        states.has_ticket_supervision_work("run-1")


def test_supervision_native_missing_json_uses_original_reader(tmp_path: Path) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    repository.save("run-1", _facts())

    class MissingJson(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if "schema_definitions" in sql:
                raise sqlite3.OperationalError("no such function: json_group_array")
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(repository.path, factory=MissingJson)) as connection:
        assert read_supervision_work(connection, "run-1", max_bytes=repository.max_bytes,
                                     schema_parameters=_WORK_SCHEMA_PARAMETERS) is None
        with repository.use_reader(connection):
            assert states.has_ticket_supervision_work("run-1") is False
