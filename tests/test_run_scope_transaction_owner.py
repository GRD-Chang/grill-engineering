"""Scope views share their caller's real SQLite transaction, never a new budget."""
from contextlib import closing
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage import sqlite as storage
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, seed


@pytest.fixture
def database():
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, "A")
        seed(db, "B")
        seed(db, "C", subject="only-C")
        db.commit()
        yield db


def test_raw_connection_cannot_create_a_scope_with_fresh_accounting(database):
    before = database.total_changes
    with pytest.raises(KernelError, match="run_scope_transaction_required"):
        RunTransaction(database, "A")
    assert database.total_changes == before


def test_scope_views_accumulate_the_original_owners_read_budget(database, monkeypatch):
    database.execute("BEGIN")
    try:
        owner = Transaction(database)
        first, second = RunTransaction(owner, "A"), RunTransaction(owner, "B")
        before_bytes, before_rows = owner.bytes_read, owner.rows_read
        monkeypatch.setattr(storage, "QUERY_LIMIT", before_bytes + 72)
        assert first.one("SELECT ?", ("x" * 40,))[0] == "x" * 40
        assert owner.bytes_read == first.bytes_read == second.bytes_read == before_bytes + 40
        assert owner.rows_read == first.rows_read == second.rows_read == before_rows + 1
        with pytest.raises(KernelError, match="query_limit"):
            second.one("SELECT ?", ("y" * 40,))
        # A new view cannot bypass what its siblings already consumed, even
        # for the scope identity observation needed by its own construction.
        with pytest.raises(KernelError, match="query_limit"):
            RunTransaction(owner, "A")
        assert owner.bytes_read == first.bytes_read == second.bytes_read == before_bytes + 40
    finally:
        database.rollback()


def test_nested_view_observes_live_owner_updates_without_copying_scalar_state(database):
    database.execute("BEGIN")
    try:
        owner = Transaction(database)
        first = RunTransaction(owner, "A")
        second = RunTransaction(first, "B")
        before = owner.bytes_read
        owner.one("SELECT 'original-root-read'")
        assert owner.bytes_read > before and first.bytes_read == second.bytes_read == owner.bytes_read
        second.one("SELECT 'original-child-read'")
        assert owner.bytes_read == first.bytes_read == second.bytes_read
        owner.failed = True
        assert first.failed and second.failed
        second.failed = False
        assert owner.failed and first.failed and second.failed
        later = RunTransaction(owner, "B")
        assert later.failed
    finally:
        database.rollback()


def test_caught_sql_failure_poison_reaches_owner_and_rollback_preserves_sibling(database):
    before_b = list(database.execute("SELECT * FROM material WHERE run_id='B'"))
    database.execute("BEGIN")
    owner = Transaction(database)
    try:
        first, second = RunTransaction(owner, "A"), RunTransaction(owner, "B")
        pending = first.put("parent:1", "pending", b"not committed")
        with pytest.raises(sqlite3.IntegrityError):
            first.execute("INSERT INTO gates(run_id,subject,code) VALUES ('A','only-C','invalid-owner')")
        assert owner.failed and first.failed and second.failed
        # The unchanged Storage context checks this exact owner's flag before
        # committing. This lower-layer test deliberately rolls back its scope.
    finally:
        database.rollback()
    assert list(database.execute("SELECT * FROM material WHERE run_id='B'")) == before_b
    with pytest.raises(KernelError, match="material_corrupt"):
        RunTransaction(Transaction(database), "A").get(pending, "parent:1")
