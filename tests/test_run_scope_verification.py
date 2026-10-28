"""Fresh Run identity checks with bounded results and shared read accounting.

These are real SQLite contract tests, not native FIFO/Host performance proof.
"""
from contextlib import closing
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage import sqlite as storage
from agent_run.storage.repository_schema import REPOSITORY_RUN_SCHEMA
from agent_run.storage.run_scope import RunTransaction, run_statement
from support.repository_scope import seed


@pytest.fixture(params=['UTF-8', 'UTF-16le'])
def database(request):
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA encoding='{request.param}'")
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(REPOSITORY_RUN_SCHEMA)
        db.execute('BEGIN')
        seed(db, 'A\0原')
        seed(db, 'B')
        db.execute("UPDATE runs SET repository='EXAMPLE/Project',repository_root='/原/root' WHERE id=?", ('A\0原',))
        db.commit()
        yield db


def original_charge(db, run='A\0原'):
    return db.execute('SELECT length(CAST(id AS BLOB))+length(CAST(repository AS BLOB))+'
                      '16+length(CAST(repository_root AS BLOB)) FROM runs WHERE id=?', (run,)).fetchone()[0]


def test_each_verified_binding_reads_sql_once_and_keeps_original_byte_charge(database):
    owner = storage.Transaction(database)
    tx = RunTransaction(owner, 'A\0原')
    before = owner.rows_read, owner.bytes_read
    charge = original_charge(database)
    statements = []
    database.set_trace_callback(statements.append)
    try:
        for _ in range(5):
            assert run_statement(tx, 'SELECT id FROM subjects WHERE run_id=?') == (
                'SELECT id FROM subjects WHERE run_id=?', ('A\0原',))
    finally:
        database.set_trace_callback(None)
    # Every call performs a current SQLite check; the immutable binding does
    # not authorize a later call. One bounded comparison replaces three reads.
    assert len(statements) == 5
    assert (owner.rows_read, owner.bytes_read) == (before[0] + 5, before[1] + 5 * charge)
    assert tx.scope.repository == 'example/project'


@pytest.mark.parametrize('field,value,code', [
    ('repository', 'different/project', 'run_scope_changed'),
    ('repository', 'example/project\0', 'run_scope_invalid'),
    ('repository', 'éxample/project', 'run_scope_invalid'),
    ('repository', b'example/project', 'run_scope_invalid'),
    ('repository_root', '/different/root', 'run_scope_changed'),
    ('repository_root', 'relative', 'run_scope_invalid'),
    ('repository_root', b'/original', 'run_scope_invalid'),
    ('parent_issue', 2, 'run_scope_changed'),
    ('parent_issue', 1.5, 'run_scope_invalid'),
])
def test_changed_or_invalid_identity_still_uses_original_refusal(database, field, value, code):
    tx = RunTransaction(storage.Transaction(database), 'A\0原')
    assert tx.require_scope() == tx.scope
    database.execute('BEGIN')
    try:
        database.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, tx.run_id))
        with pytest.raises(KernelError) as error:
            run_statement(tx, 'SELECT id FROM subjects WHERE run_id=?')
        assert error.value.code == code
    finally:
        database.rollback()
    assert tx.require_scope() == tx.scope


def test_repository_case_changes_keep_normalized_identity_and_actual_byte_charge(database):
    tx = RunTransaction(storage.Transaction(database), 'A\0原')
    database.execute('UPDATE runs SET repository=? WHERE id=?', ('Example/PROJECT', tx.run_id))
    before = tx.bytes_read
    assert tx.require_scope() == tx.scope
    assert tx.bytes_read - before == original_charge(database)


@pytest.mark.parametrize('budget', ['rows', 'bytes'])
def test_scope_checks_share_original_owner_budget_across_run_views(database, monkeypatch, budget):
    owner = storage.Transaction(database)
    a, b = RunTransaction(owner, 'A\0原'), RunTransaction(owner, 'B')
    charge = original_charge(database)
    if budget == 'rows':
        owner.rows_read = 65535
    else:
        # The common Transaction limit, not a second scope-specific allowance.
        monkeypatch.setattr(storage, 'QUERY_LIMIT', owner.bytes_read + charge)
    a.require_scope()
    after = owner.rows_read, owner.bytes_read
    with pytest.raises(KernelError, match='query_limit'):
        b.require_scope()
    assert (owner.rows_read, owner.bytes_read) == after
    assert not owner.failed


def test_oversized_changed_identity_is_not_transported_before_budget_refusal(database, monkeypatch):
    tx = RunTransaction(storage.Transaction(database), 'A\0原')
    database.execute('UPDATE runs SET repository_root=? WHERE id=?', ('/' + 'x' * 4096, tx.run_id))
    monkeypatch.setattr(storage, 'QUERY_LIMIT', tx.bytes_read + 128)
    observed = []
    def row(connection, values):
        observed.extend(values)
        return sqlite3.Row(connection, values)
    database.row_factory = row
    try:
        with pytest.raises(KernelError, match='query_limit'):
            tx.require_scope()
    finally:
        database.row_factory = sqlite3.Row
    assert all(not isinstance(value, (str, bytes)) or len(value) < 128 for value in observed)


def test_sql_failure_cannot_reuse_a_previous_success(database):
    tx = RunTransaction(storage.Transaction(database), 'A\0原')
    tx.require_scope()
    def deny(action, table, column, database_name, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'runs' else sqlite3.SQLITE_OK
    database.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            tx.require_scope()
        assert tx.failed
    finally:
        database.set_authorizer(None)
