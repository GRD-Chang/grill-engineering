"""工作查询的同源结构证明仍拒绝当前损坏，兼容结构保持完整核验。"""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_errors import IncompatibleRunStateError


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["protocol_blob", "material_blob", "extra_table", "missing_index", "missing_table", "trigger"])
def test_work_query_rejects_current_storage_after_successful_read(
    tmp_path: Path, aggregate: bool, damage: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    original = {"phase": "pending", "parent": {"body": "read evidence"}}
    repository.save("run-1", original)
    repository._aggregate_rows = aggregate
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "protocol_blob":
            connection.execute("UPDATE schema_meta SET value=CAST(value AS BLOB)")
        elif damage == "material_blob":
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        elif damage == "extra_table":
            # The underscore is a literal prefix character, not a LIKE wildcard.
            connection.execute("CREATE TABLE sqlitex_extra(value TEXT)")
        elif damage == "missing_index":
            connection.execute("DROP INDEX current_work_objects")
        elif damage == "missing_table":
            connection.execute("DROP TABLE run_history")
        else:
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON runs BEGIN SELECT 1; END")
        connection.commit()
    expected = ValueError if damage in {"protocol_blob", "material_blob"} else IncompatibleRunStateError
    with pytest.raises(expected, match="protocol|schema|indexes|BLOB|material"):
        repository.load_work("run-1", 2)


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_query_validates_compatible_noncanonical_ddl(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    original = {"parent": {"body": "read evidence"}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=sql||' ' WHERE type='table' AND name='run_objects'")
        connection.commit()
    repository._aggregate_rows = aggregate
    assert repository.load_work("run-1", 2) == original


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_query_returns_none_for_empty_database(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    with closing(sqlite3.connect(repository.path)):
        pass
    repository._aggregate_rows = aggregate
    assert repository.load_work("run-1", 2) is None
