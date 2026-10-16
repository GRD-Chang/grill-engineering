"""A Stop observation reads its exact relational intent in a fresh snapshot.

原通用节点的 JSON/material/children 形状验证已被明确类型和关系所替代；
保留真实重开、跨进程、schema、只读及无关材料损坏的行为覆盖。
"""
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import subprocess
import sys

import pytest

from agent_run.sqlite_change_job_codec import presence
from agent_run.sqlite_run_schema import FIELDS
from agent_run.state import StateStore
from agent_run.ticket_state import TicketState
from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("timestamp", ["2026-10-13T01:02:03+00:00", "retained" * 200])
def test_pause_reopens_latest_intent_and_ignores_unrelated_material(tmp_path: Path, timestamp: str) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    state = {"ticket_pause_requests": {}, "parent_job": {"review_context": {"body": "unrelated" * 200}}}
    repository.save("run-1", state)
    lane = TicketState(states, "run-1", 2)
    assert lane.ticket_pause_requested("run-1", 2) is False
    repository.save_objects("run-1", {"ticket_pause_requests": {"2": timestamp, "3": "sibling"}}, fields={"ticket_pause_requests"})
    assert StateStore(tmp_path).ticket_pause_requested("run-1", 2) is True
    assert lane.ticket_pause_requested("run-1", 2) is True
    repository.save_objects("run-1", {"ticket_pause_requests": {}}, fields={"ticket_pause_requests"}, keyed_changes={"ticket_pause_requests": {"2"}})
    assert lane.ticket_pause_requested("run-1", 2) is False
    assert states.ticket_pause_requested("run-1", 3) is True
    with repository.atomic():
        c = repository._local.connection
        c.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1")
        assert lane.ticket_pause_requested("run-1", 2) is False
        with pytest.raises(ValueError, match="material|evidence"):
            repository.load("run-1")
    with pytest.raises(ValueError, match="another Ticket"):
        lane.ticket_pause_requested("run-1", 3)


@pytest.mark.parametrize("damage", ["missing_run", "missing_root", "oversize", "missing_map",
                                   "map_null", "timestamp_blob", "protocol", "schema"])
def test_pause_rejects_current_relational_damage(tmp_path: Path, damage: str) -> None:
    states = StateStore(tmp_path)
    repository = states._repository
    repository.save("run-1", {"ticket_pause_requests": {"2": "timestamp"}})
    with repository.atomic():
        c = repository._local.connection
        assert states.ticket_pause_requested("run-1", 2)
        if damage == "missing_run":
            c.execute("DELETE FROM runs WHERE run_id='run-1'")
        elif damage == "missing_root":
            c.execute("DELETE FROM run_root")
        elif damage == "oversize":
            c.execute("UPDATE run_ticket_pause_requests SET requested_at=?", ("X" * (300 * 1024),))
        elif damage == "missing_map":
            c.execute("UPDATE run_root SET presence=?", (presence((), FIELDS),))
        elif damage == "map_null":
            c.execute("UPDATE run_root SET nulls=?", (presence({"ticket_pause_requests"}, FIELDS),))
        elif damage == "timestamp_blob":
            c.execute("UPDATE run_ticket_pause_requests SET requested_at=CAST(requested_at AS BLOB)")
        elif damage == "protocol":
            c.execute("UPDATE schema_meta SET value='unsupported'")
        else:
            c.execute("CREATE VIEW unexpected AS SELECT 1")
        with pytest.raises(ValueError):
            states.ticket_pause_requested("run-1", 2)


def test_pause_missing_storage_does_not_create_or_repair_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(ValueError, match="disappeared"):
        StateStore(root).ticket_pause_requested("run-1", 2)
    assert not root.exists()


def test_pause_sessions_observe_another_process_commit(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"ticket_pause_requests": {}})
    source_root = str(Path(sys.modules[SQLiteStateRepository.__module__].__file__).parents[1])
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((source_root, os.environ.get("PYTHONPATH", "")))}
    with repository.work_session():
        assert repository.ticket_pause_requested("run-1", 2) is False
        result = subprocess.run([
            sys.executable, "-c",
            "from pathlib import Path; import sys; from agent_run.sqlite_state import SQLiteStateRepository; "
            "SQLiteStateRepository(Path(sys.argv[1])).save_objects('run-1',{'ticket_pause_requests':{'2':'child'}},fields={'ticket_pause_requests'})",
            str(tmp_path),
        ], env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert repository.ticket_pause_requested("run-1", 2) is True
    assert SQLiteStateRepository(tmp_path).ticket_pause_requested("run-1", 2) is True


def test_pause_rejects_invalid_timestamp_without_changing_siblings(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    state = {"ticket_pause_requests": {"2": "own", "3": "sibling"}}
    repository.save("run-1", state)
    with pytest.raises(ValueError, match="timestamp"):
        repository.save_objects("run-1", {"ticket_pause_requests": {"2": False}}, fields={"ticket_pause_requests"})
    assert repository.load("run-1") == state
