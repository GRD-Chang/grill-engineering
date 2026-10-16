"""工作查询拒绝当前领域结构损坏，兼容结构保持完整核验。"""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_errors import IncompatibleRunStateError


@pytest.mark.parametrize("damage", ["protocol_blob", "material_blob", "extra_table", "missing_index", "missing_table", "trigger"])
def test_work_query_rejects_current_storage_after_successful_read(
    tmp_path: Path, damage: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"phase": "pending", "parent": {"body": "read evidence"}}
    repository.save("run-1", original)
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "protocol_blob":
            connection.execute("UPDATE schema_meta SET value=CAST(value AS BLOB)")
        elif damage == "material_blob":
            damaged = connection.execute(
                "UPDATE work_evidence_materials SET body=CAST(body AS BLOB) WHERE material_id IN "
                "(SELECT material_id FROM work_evidence WHERE run_id='run-1' "
                "AND owner_id='run:run-1' AND kind='parent.body')"
            )
            assert damaged.rowcount == 1
        elif damage == "extra_table":
            # The underscore is a literal prefix character, not a LIKE wildcard.
            connection.execute("CREATE TABLE sqlitex_extra(value TEXT)")
        elif damage == "missing_index":
            connection.execute("DROP INDEX work_invocation_resume")
        elif damage == "missing_table":
            connection.execute("DROP TABLE work_invocation_history")
        else:
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON runs BEGIN SELECT 1; END")
        connection.commit()
    expected = ValueError if damage in {"protocol_blob", "material_blob"} else IncompatibleRunStateError
    with pytest.raises(expected, match="protocol|schema|indexes|BLOB|material|evidence byte length"):
        repository.load_work("run-1", 2)


def test_work_query_validates_compatible_noncanonical_ddl(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"parent": {"body": "read evidence"}}
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=sql||' ' WHERE type='table' AND name='work_jobs'")
        connection.commit()
    assert repository.load_work("run-1", 2) == original


def test_work_query_returns_none_for_empty_database(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    with closing(sqlite3.connect(repository.path)):
        pass
    assert repository.load_work("run-1", 2) is None


def test_schema_check_reads_all_required_facts_without_unrelated_index_definitions(tmp_path: Path) -> None:
    from agent_run.sqlite_state_connection import _CANONICAL_INDEXES, _CANONICAL_TABLES

    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {})
    with closing(sqlite3.connect(repository.path)) as writer:
        writer.execute("CREATE INDEX compatible_extra_index ON runs(updated_at)")
        writer.commit()
        seen_tables, seen_indexes = set(), set()

        def inspect(_cursor, row):
            if len(row) >= 3 and row[0] == "table":
                seen_tables.add(row[1])
            if len(row) >= 3 and row[0] == "index":
                assert row[1] in _CANONICAL_INDEXES, "Unneeded index definition entered Python"
                seen_indexes.add(row[1])
            return row

        with closing(sqlite3.connect(repository.path.resolve().as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None)) as reader:
            reader.row_factory = inspect
            assert repository._check_schema(reader)
            assert seen_tables == set(_CANONICAL_TABLES)
            assert seen_indexes == set(_CANONICAL_INDEXES)
            # The same prepared reader must revalidate current facts after a
            # commit, including a required name with the wrong definition.
            writer.execute("DROP INDEX work_invocation_resume")
            writer.execute("CREATE INDEX work_invocation_resume ON work_invocations(run_id)")
            writer.commit()
            with pytest.raises(IncompatibleRunStateError, match="indexes"):
                repository._check_schema(reader)
