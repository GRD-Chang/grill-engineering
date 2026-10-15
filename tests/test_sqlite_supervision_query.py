"""Supervision reads authoritative Run columns and the integration relation.

原节点形状、原生 JSON 聚合与回退专属测试已随实现移除；当前损坏、只读、
跨连接新快照、数值类型及历史无关成本在关系边界继续验证。
"""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.sqlite_change_job_codec import presence
from agent_run.sqlite_run_schema import FIELDS


def _facts() -> dict:
    return {"status": "active", "ticket_integration_holder": None,
            "ticket_integration_queue": [], "review_context": {"body": "unrelated" * 200}}


@pytest.mark.parametrize("status", ["active", "ticket_delivery_pending", "operator_stopped", "unknown" * 200])
def test_supervision_precheck_reopens_and_reads_latest_shared_facts(tmp_path: Path, status: str) -> None:
    states = StateStore(tmp_path)
    facts = _facts() | {"status": status}
    states._repository.save("run-1", facts)
    empty = status in {"active", "ticket_delivery_pending"}
    with states.work_session():
        assert states.has_ticket_supervision_work("run-1") is not empty
        updated = dict(facts, ticket_integration_holder=2)
        StateStore(tmp_path)._repository.save_objects("run-1", updated, fields={"ticket_integration_holder"})
        assert states.has_ticket_supervision_work("run-1")
        updated.update(ticket_integration_holder=None, ticket_integration_queue=[{"ticket_number": 2}])
        StateStore(tmp_path)._repository.save_objects("run-1", updated, fields={"ticket_integration_holder", "ticket_integration_queue"})
        assert states.has_ticket_supervision_work("run-1")
        StateStore(tmp_path)._repository.save_objects("run-1", facts, fields={"ticket_integration_holder", "ticket_integration_queue"})
        assert states.has_ticket_supervision_work("run-1") is not empty
    assert not states._repository.in_transaction


def test_supervision_ignores_unrelated_material_and_never_repairs_missing_run(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    states = StateStore(root)
    assert states.has_ticket_supervision_work("run-1") is False
    assert not root.exists()
    states._repository.save("run-1", _facts())
    assert states.has_ticket_supervision_work("absent") is False
    with states._repository.atomic():
        c = states._repository._local.connection
        c.execute("UPDATE run_materials SET byte_length=byte_length+1")
        c.execute("UPDATE runs SET status='completed'")
        # The indexed status column is the single source, with no stale node.
        assert states.has_ticket_supervision_work("run-1") is True
        with pytest.raises(ValueError, match="material"):
            states._repository.load("run-1")


def test_supervision_query_cost_does_not_scan_unrelated_history(tmp_path: Path) -> None:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    costs = []
    for count in (1, 1000):
        states._repository.save_objects("run-1", {"unrelated_history": [{"fact": n} for n in range(count)]}, fields={"unrelated_history"})
        steps = [0]
        def progress() -> int:
            steps[0] += 1
            return 0
        with states._repository.atomic():
            c = states._repository._local.connection
            c.set_progress_handler(progress, 1)
            try:
                assert states.has_ticket_supervision_work("run-1") is False
            finally:
                c.set_progress_handler(None, 0)
        costs.append(steps[0])
    assert costs[1] <= costs[0] + 64, costs


@pytest.mark.parametrize("damage", ["root", "missing_status", "status_blob", "holder_text", "holder_real",
                                   "queue_null", "missing_size", "oversize", "protocol", "schema"])
def test_supervision_rejects_current_relational_damage(tmp_path: Path, damage: str) -> None:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    with states._repository.atomic():
        c = states._repository._local.connection
        assert not states.has_ticket_supervision_work("run-1")
        if damage == "root":
            c.execute("DELETE FROM run_root")
        elif damage == "missing_status":
            c.execute("UPDATE run_root SET presence=?", (presence({"ticket_integration_holder", "ticket_integration_queue"}, FIELDS),))
        elif damage == "status_blob":
            c.execute("UPDATE runs SET status=CAST(status AS BLOB)")
        elif damage == "holder_text":
            c.execute("UPDATE run_root SET value_ticket_integration_holder='true'")
        elif damage == "holder_real":
            c.execute("UPDATE run_root SET value_ticket_integration_holder=2.5")
        elif damage == "queue_null":
            c.execute("UPDATE run_root SET nulls=?", (presence({"ticket_integration_holder", "ticket_integration_queue"}, FIELDS),))
        elif damage == "missing_size":
            c.execute("DELETE FROM run_sizes")
        elif damage == "oversize":
            c.execute("UPDATE run_sizes SET logical_bytes=?", (MAX_RUN_STATE_BYTES + 1,))
        elif damage == "protocol":
            c.execute("UPDATE schema_meta SET value='unsupported'")
        else:
            c.execute("CREATE VIEW unexpected AS SELECT 1")
        with pytest.raises(ValueError):
            states.has_ticket_supervision_work("run-1")


@pytest.mark.parametrize("field,value", [("ticket_integration_holder", True), ("ticket_integration_holder", 2.5),
                                         ("ticket_integration_queue", False)])
def test_supervision_rejects_invalid_types_without_persisting(tmp_path: Path, field: str, value: object) -> None:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    with pytest.raises(ValueError):
        states._repository.save_objects("run-1", {field: value}, fields={field})
    assert states.has_ticket_supervision_work("run-1") is False


def test_supervision_rejects_oversized_current_status_before_copy(tmp_path: Path) -> None:
    states = StateStore(tmp_path)
    states._repository.save("run-1", _facts())
    with closing(sqlite3.connect(states._repository.path)) as c:
        c.execute("UPDATE runs SET status=?", ("X" * (300 * 1024),))
        c.commit()
    with pytest.raises(ValueError, match="current record"):
        states.has_ticket_supervision_work("run-1")
