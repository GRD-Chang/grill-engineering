"""Scoped scheduling polls observe real commits and release their SQLite reader."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_watcher import SQLiteWatcher
from agent_run.state import StateStore
from agent_run.object_state import save_owned
from support.ticket_parallel import ticket_run


def test_watcher_observes_other_process_commit_and_not_rollback(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=8192)
    repository.save("run-1", {"version": 1})
    with SQLiteWatcher(repository.path) as watcher:
        first = watcher.version()
        connection = watcher.connection
        assert connection is not None and not connection.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM runs")
        with pytest.raises(OSError):
            with repository.atomic():
                repository.save_objects("run-1", {"version": 2}, fields={"version"})
                raise OSError("before commit")
        assert watcher.version() == first
        script = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE run_objects SET value_json='2' WHERE object_path='[\\\"version\\\"]'\"); c.commit(); c.close()"
        result = subprocess.run([sys.executable, "-c", script, str(repository.path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert watcher.version() != first
        with repository.use_reader(connection):
            assert repository.load("run-1") == {"version": 2}
        assert not connection.in_transaction
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_watcher_reopens_after_database_replacement_and_closes_on_error(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "original", max_bytes=8192)
    replacement = SQLiteStateRepository(tmp_path / "replacement", max_bytes=8192)
    repository.save("run-1", {"version": 1})
    replacement.save("run-1", {"version": 2})
    with pytest.raises(OSError, match="scope failed"):
        with SQLiteWatcher(repository.path) as watcher:
            first = watcher.version()
            old = watcher.connection
            replacement.path.replace(repository.path)
            assert watcher.version() != first
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                old.execute("SELECT 1")
            new = watcher.connection
            with repository.use_reader(new):
                assert repository.load("run-1") == {"version": 2}
            raise OSError("scope failed")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        new.execute("SELECT 1")


def test_polling_sees_stop_and_preserves_independent_public_views(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    with states.current_run_reader(run_id) as read:
        first = read()
        assert first is not None and read() == first
        independent = states.load_current_run(run_id)
        independent["parent"]["body"] = "caller mutation"
        assert read()["parent"]["body"] == first["parent"]["body"]
        other = StateStore(states.root)
        stopped = other.load_current_run(run_id)
        stopped["status"] = "operator_stopped"
        save_owned(other, run_id, stopped, fields={"status"})
        assert read()["status"] == "operator_stopped"
        assert read() == other.load_current_run(run_id)


@pytest.mark.parametrize("damage", ["protocol", "columns", "material"])
def test_polling_rejects_changed_schema_and_materials(git_repo: Path, damage: str) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    with states.current_run_reader(state["run_id"]) as read:
        assert read() is not None
        assert read() is not None
        with sqlite3.connect(states._repository.path) as connection:
            if damage == "protocol":
                connection.execute("UPDATE schema_meta SET value='unsupported'")
            elif damage == "columns":
                connection.execute("ALTER TABLE run_objects ADD COLUMN unsupported TEXT")
            else:
                connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        with pytest.raises(ValueError):
            read()
        # A failed refresh must not mark this version as verified.
        with pytest.raises(ValueError):
            read()


def test_polling_refresh_does_not_mark_concurrent_commit_as_observed(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    other = StateStore(states.root)
    changed = other.load_current_run(run_id)
    changed["status"] = "operator_stopped"
    original = SQLiteStateRepository._check_schema
    advanced = False

    def check(connection, *, allow_empty=False):
        nonlocal advanced
        result = original(connection, allow_empty=allow_empty)
        if not advanced and connection.execute("PRAGMA query_only").fetchone()[0]:
            advanced = True
            save_owned(other, run_id, changed, fields={"status"})
        return result

    monkeypatch.setattr(SQLiteStateRepository, "_check_schema", staticmethod(check))
    with states.current_run_reader(run_id) as read:
        assert read()["status"] == state["status"]
        assert advanced
        assert read()["status"] == "operator_stopped"


def test_polling_callback_is_closed_after_scope_exit(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    with states.current_run_reader(state["run_id"]) as read:
        assert read() is not None
    with pytest.raises(RuntimeError, match="scope is not active"):
        read()


def test_polling_never_substitutes_old_facts_inside_a_write_transaction(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    with states.current_run_reader(run_id) as read:
        assert read()["status"] == state["status"]
        with pytest.raises(OSError, match="before commit"):
            with states._repository.atomic():
                changed = states.load_current_run(run_id)
                changed["status"] = "operator_stopped"
                states._repository.save_objects(run_id, changed, fields={"status"})
                assert read()["status"] == "operator_stopped"
                raise OSError("before commit")
        assert read()["status"] == state["status"]


def test_work_polling_reuses_its_current_read_size_without_encoding_twice(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    sizes = []
    check = states._repository._check_size

    def measure(*args, **kwargs):
        result = check(*args, **kwargs)
        sizes.append(result)
        return result

    monkeypatch.setattr(states._repository, "_check_size", measure)
    with states.current_run_reader(state["run_id"], work_only=True) as read:
        assert read() is not None
        assert len(sizes) == 1
        assert read() is not None
        assert len(sizes) == 1


def test_custom_work_polling_keeps_actual_returned_size_limit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    original = states.load_work_run
    reads = []

    def custom(*args, **kwargs):
        current = original(*args, **kwargs)
        current["custom"] = "x" * (513 * 1024)
        reads.append(True)
        return current

    monkeypatch.setattr(states, "load_work_run", custom)
    with states.current_run_reader(state["run_id"], work_only=True) as read:
        assert len(read()["custom"]) == 513 * 1024
        assert len(read()["custom"]) == 513 * 1024
        assert len(reads) == 2
