"""A Stop observation reads only its own intent and validates that snapshot."""
from pathlib import Path

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.ticket_state import TicketState


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
