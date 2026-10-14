"""Ticket scopes retain prepared SQL, never transactional facts or ownership."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_watcher import SQLiteWatcher


def _repository(path: Path) -> SQLiteStateRepository:
    return SQLiteStateRepository(path, max_bytes=4 * 1024 * 1024)


def _capture_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    created = []
    connect = sqlite3.connect
    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        created.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, "connect", capture)
    return created


def _assert_closed(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_work_session_is_lazy_nested_and_closes_idle_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 1})
    created = _capture_connections(monkeypatch)
    with repository.work_session():
        assert created == []
        assert repository.load_work("run-1", 2) == {"value": 1}
        assert len(created) == 1 and not created[0].in_transaction
        assert getattr(created[0], "schema_facts") is None
        assert getattr(created[0], "run_rows") is None
        assert created[0].execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            created[0].execute("UPDATE runs SET status='altered'")
        with repository.work_session():
            assert repository.load_work("run-1", 2) == {"value": 1}
            with repository.atomic():
                baseline = repository.load_work("run-1", 2)
                assert baseline is not None
                repository.save_work("run-1", {"value": 2}, baseline)
        assert len(created) == 2
        assert not repository.in_transaction
        assert all(not connection.in_transaction for connection in created)
        assert repository.load_work("run-1", 2) == {"value": 2}
        assert len(created) == 2
    _assert_closed(created)


def test_work_session_rechecks_external_commits_between_transactions(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 1})
    with repository.work_session():
        assert repository.load("run-1") == {"value": 1}
        with repository.atomic():
            assert repository.load_work("run-1", 2) == {"value": 1}
        _repository(tmp_path).save("run-1", {"value": 2})
        assert repository.load("run-1") == {"value": 2}
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            assert baseline == {"value": 2}
            repository.save_work("run-1", {"value": 3}, baseline)
    assert _repository(tmp_path).load("run-1") == {"value": 3}


def test_work_session_writer_reestablishes_and_enforces_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 1})
    created = _capture_connections(monkeypatch)
    with repository.work_session():
        with repository.atomic():
            assert repository._local.connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        writer = created[0]
        writer.execute("PRAGMA foreign_keys=OFF")
        assert writer.execute("PRAGMA foreign_keys").fetchone() == (0,)
        with pytest.raises(OSError, match="FOREIGN KEY"):
            with repository.atomic():
                assert repository._local.connection is writer
                assert writer.execute("PRAGMA foreign_keys").fetchone() == (1,)
                writer.execute("INSERT INTO run_material_refs(run_id,material_id) VALUES('missing','missing')")
    _assert_closed(created)
    assert repository.load("run-1") == {"value": 1}


def test_work_session_rolls_back_and_closes_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 1})
    created = _capture_connections(monkeypatch)
    with pytest.raises(ValueError, match="injected"):
        with repository.work_session():
            assert repository.load_work("run-1", 2) is not None
            with repository.atomic():
                baseline = repository.load_work("run-1", 2)
                assert baseline is not None
                repository.save_work("run-1", {"value": 2}, baseline)
                raise ValueError("injected")
    _assert_closed(created)
    assert not repository.in_transaction
    assert repository.load("run-1") == {"value": 1}


@pytest.mark.parametrize("damage", ["protocol", "schema", "material"])
def test_work_session_revalidates_persisted_storage(tmp_path: Path, damage: str) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"parent": {"body": "frozen requirement"}})
    with repository.work_session():
        assert repository.load("run-1") is not None
        with closing(sqlite3.connect(repository.path)) as database:
            if damage == "protocol":
                database.execute("UPDATE schema_meta SET value='unsupported'")
            elif damage == "schema":
                database.execute("CREATE VIEW unexpected AS SELECT 1")
            else:
                database.execute("UPDATE run_materials SET byte_length=byte_length+1")
            database.commit()
        with pytest.raises(ValueError, match="protocol|schema|byte length"):
            repository.load_work("run-1", 2)


@pytest.mark.parametrize("remove", [False, True])
def test_work_session_discards_connections_after_database_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove: bool,
) -> None:
    repository = _repository(tmp_path / "original")
    repository.save("run-1", {"value": 1})
    replacement = _repository(tmp_path / "replacement")
    replacement.save("run-1", {"value": 2})
    created = _capture_connections(monkeypatch)
    with repository.work_session():
        assert repository.load_work("run-1", 2) == {"value": 1}
        with repository.atomic():
            assert repository.load_work("run-1", 2) == {"value": 1}
        old = list(created)
        if remove:
            repository.path.unlink()
            assert repository.load_work("run-1", 2) is None
            assert not repository.path.exists()
            _assert_closed(old)
            repository.save("run-1", {"value": 2})
        else:
            replacement.path.replace(repository.path)
        assert repository.load_work("run-1", 2) == {"value": 2}
        _assert_closed(old)
        with repository.atomic():
            baseline = repository.load_work("run-1", 2)
            assert baseline is not None
            repository.save_work("run-1", {"value": 3}, baseline)
    _assert_closed(created)
    assert _repository(repository.root).load("run-1") == {"value": 3}


def test_work_session_uses_watcher_reader_without_owning_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 1})
    with SQLiteWatcher(repository.path) as watcher:
        assert watcher.version() is not None
        assert watcher.connection is not None
        created = _capture_connections(monkeypatch)
        with repository.work_session(), repository.use_reader(watcher.connection):
            assert repository.load_work("run-1", 2) == {"value": 1}
        assert created == []
        assert watcher.connection.execute("SELECT 1").fetchone() == (1,)


def test_work_sessions_keep_threads_and_transactions_independent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save("run-1", {"value": 0})
    barrier = threading.Barrier(2, timeout=5)
    def increment():
        with repository.work_session():
            assert repository.load_work("run-1", 2) is not None
            barrier.wait()
            with repository.atomic():
                baseline = repository.load_work("run-1", 2)
                assert baseline is not None
                updated = deepcopy(baseline)
                updated["value"] += 1
                repository.save_work("run-1", updated, baseline)
            barrier.wait()
            assert repository.load_work("run-1", 2) == {"value": 2}
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(increment) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert repository.load("run-1") == {"value": 2}
