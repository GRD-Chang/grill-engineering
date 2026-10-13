"""Empty supervision prechecks preserve full validation for actual work."""
from pathlib import Path

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.state import MAX_RUN_STATE_BYTES, StateStore


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
