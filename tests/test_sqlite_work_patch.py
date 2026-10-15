"""批次工作写入保留准确字节、跨 Run 引用与失败时的原子回滚。"""
from copy import deepcopy
from pathlib import Path
from contextlib import closing
import hashlib
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("body", ["new evidence", "\x00中文🙂" * 300, "large evidence" * 6000])
def test_work_patch_batches_keep_other_run_materials_and_exact_reopen(
    tmp_path: Path, aggregate: bool, body: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    original = {"ticket_jobs": {str(number): {"ticket_number": number, "phase": "developing"}
                               for number in range(2, 272)}}
    repository.save("run-1", original)
    repository.save("other-run", original)
    repository._aggregate_rows = aggregate
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        updated = deepcopy(baseline)
        for job in updated["ticket_jobs"].values():
            job["phase"] = "candidate"
        updated["ticket_jobs"]["2"]["body"] = body
        updated["ticket_jobs"].pop("271")
        repository.save_work("run-1", updated, baseline)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == updated
    assert reopened.load("other-run") == original
    with reopened.atomic():
        connection = reopened._local.connection
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        materials = {row[0] for row in connection.execute("SELECT material_id FROM run_materials")}
        references = {row[0] for row in connection.execute("SELECT material_id FROM run_object_material_refs")}
        assert materials == references


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["same_length", "blob", "length"])
def test_work_patch_rejects_existing_unreferenced_material_and_rolls_back(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    original = {"parent": {"body": "old evidence"}}
    proposed = {"parent": {"body": "new evidence"}}
    repository.save("run-1", original)
    _, generated = repository._objects(proposed)
    identity, value = next(iter(generated.items()))
    assert hashlib.sha256(value.encode()).hexdigest() == identity
    with closing(sqlite3.connect(repository.path)) as connection:
        bad_value = value.replace("new", "bad") if damage == "same_length" else value.encode() if damage == "blob" else value
        bad_length = len(value.encode()) + int(damage == "length")
        connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, bad_value, bad_length))
        connection.commit()
    repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="material"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            repository.save_work("run-1", proposed, baseline)
    assert repository.load("run-1") == original


@pytest.mark.parametrize("aggregate", [True, False])
def test_secondary_reference_batch_failure_rolls_back_both_projection_indexes(
    tmp_path: Path, aggregate: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = {"parent": {"body": "old material " * 200}}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    with pytest.raises(OSError, match="injected reference failure"):
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            updated = deepcopy(baseline)
            updated["parent"]["body"] = "new material " * 200
            # The failure occurs after the first projection's rows and material
            # references have changed, at the second index's actual SQL write.
            repository._local.connection.execute(
                "CREATE TRIGGER reject_second_index BEFORE INSERT ON run_object_materials "
                "BEGIN SELECT RAISE(ABORT,'injected reference failure'); END",
            )
            repository.save_work("run-1", updated, baseline)
    reopened = SQLiteStateRepository(tmp_path, max_bytes=65536)
    assert reopened.load("run-1") == original
    with reopened.atomic():
        connection = reopened._local.connection
        first = connection.execute(
            "SELECT run_id,object_path,material_id FROM run_object_material_refs ORDER BY 1,2,3",
        ).fetchall()
        second = connection.execute(
            "SELECT run_id,object_path,material_id FROM run_object_materials ORDER BY 1,2,3",
        ).fetchall()
        assert first == second
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall() == []
