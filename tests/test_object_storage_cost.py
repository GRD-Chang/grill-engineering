"""对象存储成本优化保留索引证明、历史内容和一致读取边界。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_integrity import field_content_digest
from agent_run.sqlite_objects import _rows_for_paths
from agent_run.sqlite_state import SQLiteStateRepository


def _history(count: int) -> list[dict[str, object]]:
    return [dict(work_subject="ticket:2", started_at=f"invocation-{index}",
                 semantic_attempt={"attempt_id": f"attempt-{index}"},
                 review_context={"body": "证据\x00\n" * 300}) for index in range(count)]


def test_object_commit_updates_only_changed_field_indexes(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    state = {"phase": "pending", "fixed": {"body": "fixed evidence " * 300},
             "agent_invocation_history": _history(12)}
    repository.save("run-1", state)
    writes: list[str] = []
    with repository.atomic():
        changed = repository.load("run-1")
        assert changed is not None
        changed["phase"] = "completed"
        repository._local.connection.set_trace_callback(writes.append)
        repository.save_objects("run-1", changed, fields={"phase"})
    indexes = [sql for sql in writes if sql.startswith("INSERT INTO run_object_index ")]
    assert len(indexes) == 1 and '["phase"]' in indexes[0]
    assert repository.load("run-1") == {**state, "phase": "completed"}


@pytest.mark.parametrize("damage", ["proof", "missing"])
def test_object_commit_rejects_damaged_unchanged_field_index(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    state = {"phase": "pending", "fixed": {"body": "evidence"}}
    repository.save("run-1", state)
    with sqlite3.connect(repository.path) as connection:
        if damage == "proof":
            connection.execute("UPDATE run_object_index SET size_proof='broken' WHERE object_path=?",
                               (encode_json(("fixed",)),))
        else:
            connection.execute("DELETE FROM run_object_index WHERE object_path=?", (encode_json(("fixed",)),))
    with pytest.raises(ValueError, match="field size proof"):
        repository.save_objects("run-1", {"phase": "completed"}, fields={"phase"})
    assert repository.load("run-1") == state


def test_unrelated_commit_preserves_damaged_history_for_explicit_validation(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    state = {"phase": "pending", "agent_invocation_history": _history(2),
             "active_agent_invocation": _history(2)[0]}
    repository.save("run-1", state)
    path = encode_json(("agent_invocation_history", 0))
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE run_object_index SET invocation_id='wrong',size_proof='broken' WHERE object_path=?", (path,))
    repository.save_objects("run-1", {"phase": "completed"}, fields={"phase"})
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute("SELECT invocation_id,size_proof FROM run_object_index WHERE object_path=?", (path,)).fetchone()
    assert row == ("wrong", "broken")
    assert repository.load("run-1") == {**state, "phase": "completed"}
    with pytest.raises(ValueError, match="history index claim"):
        repository.save_objects("run-1", {"agent_invocation_history": [state["agent_invocation_history"][0]]},
                                fields={"agent_invocation_history"}, history_positions={
                                    encode_json(("ticket:2", "invocation-0")): 0,
                                })


@pytest.mark.parametrize("change", ["append", "edit", "from_empty", "sparse"])
def test_selected_history_delta_preserves_full_content_and_size(tmp_path: Path, change: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    state = {"phase": "pending", "agent_invocation_history": [] if change == "from_empty" else _history(4)}
    repository.save("run-1", state)
    history = deepcopy(state["agent_invocation_history"])
    if change in {"append", "from_empty"}:
        selected = _history(6)[4:]
        history.extend(selected)
    elif change == "edit":
        history[2]["review_context"]["body"] += "new evidence"
        selected = [history[2]]
    else:
        # An omitted history record is preserved, including its exact position.
        selected = [history[2]]
    with repository.atomic():
        repository.save_objects("run-1", {"agent_invocation_history": selected},
                                fields={"agent_invocation_history"}, history_positions={
                                    encode_json((item["work_subject"], item["started_at"])): position
                                    for position, item in enumerate(state["agent_invocation_history"])
                                })
        summary = repository._local.connection.execute(
            "SELECT logical_bytes,content_digest,child_count FROM run_object_index WHERE run_id=? AND object_path=?",
            ("run-1", encode_json(("agent_invocation_history",))),
        ).fetchone()
        assert summary == (len(encode_json("agent_invocation_history").encode()) + len(encode_json(history).encode()) + 2,
                           field_content_digest("agent_invocation_history", history), len(history))
    assert repository.load("run-1") == {**state, "agent_invocation_history": history}
    assert repository.load_objects("run-1")[0]["phase"] == "pending"


def test_batched_subtree_reads_do_not_scan_unrelated_history(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    state = {f"field-{index}": {"value": index} for index in range(30)}
    paths = [encode_json((key,)) for key in state]
    repository.save("run-1", {**state, "agent_invocation_history": []})
    costs = []
    for count in (0, 256):
        history = _history(count)
        for item in history:
            item["review_context"] = {"body": "small archived evidence"}
        repository.save_objects("run-1", {"agent_invocation_history": history}, fields={"agent_invocation_history"})
        with sqlite3.connect(repository.path) as connection:
            steps = []
            connection.set_progress_handler(lambda: steps.append(1) or 0, 10)
            rows = _rows_for_paths(connection, "run-1", paths)
            connection.set_progress_handler(None, 0)
            assert len(rows) == 60
            costs.append(len(steps))
    assert abs(costs[1] - costs[0]) <= 5, costs


@pytest.mark.parametrize("reader", ["full", "objects", "paths"])
def test_shared_material_read_is_independent_and_revalidated(tmp_path: Path, reader: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    job = {"ticket_number": 2, "review_context": {"body": "verified evidence " * 200}}
    state = {"phase": "pending", "active_ticket_job": job, "ticket_jobs": {"2": job, "3": deepcopy(job)}}
    repository.save("run-1", state)
    assert repository.load("run-1") == state
    repository.save_objects("run-1", {"phase": "active"}, fields={"phase"})

    def read():
        if reader == "full":
            return repository.load("run-1")
        if reader == "objects":
            return repository.load_objects("run-1", related_tickets=True, ticket_numbers={3})[0]
        return repository.load_paths("run-1", {("ticket_jobs", "2"), ("ticket_jobs", "3")})

    current = read()
    if reader != "paths":
        assert current["phase"] == "active"
    current["ticket_jobs"]["2"]["review_context"]["body"] = "caller mutation"
    assert current["ticket_jobs"]["3"] == job
    assert read()["ticket_jobs"]["2"] == job
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE run_materials SET value='damaged' WHERE value=?", (job["review_context"]["body"],))
    with pytest.raises(ValueError, match="material identity"):
        read()


def test_transaction_object_commit_checks_new_logical_size_and_rolls_back(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=2048)
    state = {"phase": "pending", "fixed": "original"}
    repository.save("run-1", state)
    with pytest.raises(ValueError, match="persistence limit"):
        with repository.atomic():
            changed = repository.load("run-1")
            changed["many_references"] = ["deduplicated evidence " * 50] * 8
            repository.save_objects("run-1", changed, fields={"many_references"})
    assert repository.load("run-1") == state
    assert repository.load_objects("run-1")[0] == {**state, "agent_invocation_history": []}
