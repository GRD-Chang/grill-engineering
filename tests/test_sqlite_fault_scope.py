"""Private test capability validation; these cases never activate a fault."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from support.core_operations import native_agent_fixture
from support.sqlite_faults import FaultScope, assert_save_rollback, logical_state, sqlite_fault


@pytest.fixture
def rollback_snapshot_database(tmp_path):
    """A snapshot-helper leaf, not a Core/FIFO execution or fault substitute."""
    database = tmp_path / 'snapshot.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('''
            CREATE TABLE business(body TEXT);
            CREATE TABLE budgets(used INTEGER);
            CREATE TABLE save_admissions(position INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE other_sequence(id INTEGER PRIMARY KEY AUTOINCREMENT);
            INSERT INTO business VALUES ('Original evidence');
            INSERT INTO budgets VALUES (3);
        ''')
        connection.commit()
    return database


@pytest.mark.parametrize('registration_committed', [False, True])
def test_rollback_snapshot_counts_only_committed_registration(rollback_snapshot_database, registration_committed):
    database = rollback_snapshot_database
    before = logical_state(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('INSERT INTO save_admissions DEFAULT VALUES')
        if registration_committed:
            connection.commit()
        else:
            connection.rollback()
        connection.execute("INSERT INTO business VALUES ('Uncommitted evidence')")
        connection.execute('UPDATE budgets SET used=4')
        connection.rollback()
        connection.execute('DELETE FROM save_admissions')
        connection.commit()
    assert_save_rollback(database, before, registrations=int(registration_committed))


@pytest.mark.parametrize('corruption', ['business', 'budget', 'queue', 'other-sequence', 'registration-count'])
def test_rollback_snapshot_rejects_any_other_change(rollback_snapshot_database, corruption):
    database = rollback_snapshot_database
    before = logical_state(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('INSERT INTO save_admissions DEFAULT VALUES')
        connection.commit()
        if corruption != 'queue':
            connection.execute('DELETE FROM save_admissions')
        if corruption == 'business':
            connection.execute("UPDATE business SET body='Unexpected replacement'")
        elif corruption == 'budget':
            connection.execute('UPDATE budgets SET used=4')
        elif corruption == 'other-sequence':
            connection.execute('INSERT INTO other_sequence DEFAULT VALUES')
            connection.execute('DELETE FROM other_sequence')
        connection.commit()
    with pytest.raises(AssertionError):
        assert_save_rollback(database, before, registrations=0 if corruption == 'registration-count' else 1)


def _created_kernel(directory: Path):
    base = Commit('base', 'tree')
    job = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'original', base,
                   'refs/heads/source', 'refs/heads/main', checkout=str(directory))
    return native_agent_fixture(directory / 'kernel.sqlite3',
        RunSpec('scope-run', 'example/project', 1, 'original', 'graph', (job,)))[0]


@pytest.mark.parametrize('mismatch', ['database', 'run', 'action', 'inode', 'digest'])
def test_fault_scope_rejects_foreign_identity_without_activation(tmp_path, mismatch):
    kernel = _created_kernel(tmp_path)
    database = kernel.storage.path
    scope, digest = FaultScope.create(tmp_path / 'fault', database, 'scope-run', 'original')
    selected, run, action = database, 'scope-run', 'original'
    if mismatch == 'database':
        selected = tmp_path / 'unrelated.sqlite3'
        sqlite3.connect(selected).close()
    elif mismatch == 'run':
        run = 'other-run'
    elif mismatch == 'action':
        action = 'other-action'
    elif mismatch == 'inode':
        values = scope.values | {'inode': scope.values['inode'] + 1}
        payload = json.dumps(values, sort_keys=True).encode()
        scope.manifest.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
    else:
        digest = '0' * 64
    before = database.read_bytes()
    state = scope.manifest.with_suffix('.state.json').read_bytes()
    with pytest.raises(ValueError):
        FaultScope.load(scope.manifest, digest, selected, run, action)
    assert database.read_bytes() == before
    assert scope.manifest.with_suffix('.state.json').read_bytes() == state
    assert not scope.manifest.with_suffix('.events.jsonl').exists()


def test_inactive_fault_factory_preserves_target_and_foreign_transactions(tmp_path, monkeypatch):
    kernel = _created_kernel(tmp_path)
    database = kernel.storage.path
    scope, digest = FaultScope.create(tmp_path / 'fault', database, 'scope-run', 'original')
    scope = FaultScope.load(scope.manifest, digest, database, 'scope-run', 'original')
    foreign = tmp_path / 'unrelated.sqlite3'
    with sqlite_fault(monkeypatch, database, 'commit-interrupt', scope=scope) as failures:
        assert kernel.control('stop', 'normal-stop', subject='parent:1') == 'applied'
        connection = sqlite3.connect(foreign)
        try:
            assert type(connection) is sqlite3.Connection
            connection.execute('CREATE TABLE physical_probe(value TEXT)')
            connection.execute('INSERT INTO physical_probe VALUES (?)', ('unrelated',))
            connection.commit()
            assert connection.execute('SELECT value FROM physical_probe').fetchall() == [('unrelated',)]
        finally:
            connection.close()
    assert failures == [] and not scope.active()
    assert not scope.manifest.with_suffix('.events.jsonl').exists()


def test_only_original_test_owner_can_change_activation_receipt(tmp_path):
    kernel = _created_kernel(tmp_path)
    scope, _ = FaultScope.create(tmp_path / 'fault', kernel.storage.path, 'scope-run', 'original')
    state = scope.manifest.with_suffix('.state.json')
    before = state.read_bytes()
    other = replace(scope, values=scope.values | {'owner': os.getpid() + 1})
    with pytest.raises(ValueError, match='original test owner'):
        other.set_active(False)
    assert state.read_bytes() == before and not scope.active()


def test_fault_factory_checks_actual_run_before_using_the_connection(tmp_path, monkeypatch):
    kernel = _created_kernel(tmp_path)
    scope, digest = FaultScope.create(tmp_path / 'fault', kernel.storage.path, 'other-run', 'original')
    scope = FaultScope.load(scope.manifest, digest, kernel.storage.path, 'other-run', 'original')
    before = kernel.storage.path.read_bytes()
    with sqlite_fault(monkeypatch, kernel.storage.path, 'commit-interrupt', scope=scope) as failures:
        with pytest.raises(ValueError, match='another Run'):
            sqlite3.connect(kernel.storage.path)
    assert failures == [] and not scope.active()
    assert kernel.storage.path.read_bytes() == before


@pytest.mark.parametrize('reader_present', [False, True])
def test_fault_audit_fifo_is_rejected_without_blocking(tmp_path, reader_present):
    kernel = _created_kernel(tmp_path)
    scope, digest = FaultScope.create(tmp_path / 'fault', kernel.storage.path, 'scope-run', 'original')
    fifo = scope.manifest.with_suffix('.events.jsonl')
    os.mkfifo(fifo, 0o600)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK) if reader_present else None
    program = '''import errno, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from support.sqlite_faults import FaultScope
scope = FaultScope.load(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), 'scope-run', 'original')
try:
    scope.record('rejected_fifo')
except OSError as error:
    assert error.errno == errno.ENXIO
except ValueError as error:
    assert str(error) == 'Fault audit is unavailable or full'
else:
    raise AssertionError('FIFO accepted as a regular audit file')
'''
    try:
        # A broken blocking open is bounded and reaped by subprocess.run,
        # rather than hanging pytest or starting a FIFO writer thread.
        probe = subprocess.run([sys.executable, '-I', '-c', program,
            str(Path(__file__).parent), str(scope.manifest), digest, str(kernel.storage.path)],
            capture_output=True, text=True, timeout=3)
        assert probe.returncode == 0, (probe.stdout, probe.stderr)
    finally:
        if reader is not None:
            os.close(reader)
    assert not scope.active()


def test_fault_audit_failure_preserves_sqlite_error_and_rollback(tmp_path, monkeypatch):
    kernel = _created_kernel(tmp_path)
    scope, _ = FaultScope.create(tmp_path / 'fault', kernel.storage.path, 'scope-run', 'original')
    os.mkfifo(scope.manifest.with_suffix('.events.jsonl'), 0o600)
    with sqlite_fault(monkeypatch, kernel.storage.path, 'commit-interrupt', scope=scope) as failures:
        connection = sqlite3.connect(kernel.storage.path)
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            # Only TEMP probe data fails COMMIT: no business facts are edited,
            # and the persistent native interruption switch stays off.
            connection.execute('CREATE TEMP TABLE audit_probe(id INTEGER PRIMARY KEY, parent INTEGER '
                'REFERENCES audit_probe(id) DEFERRABLE INITIALLY DEFERRED)')
            connection.execute('BEGIN')
            connection.execute('INSERT INTO temp.audit_probe VALUES (1, 999)')
            with pytest.raises(sqlite3.IntegrityError) as failed:
                connection.commit()
            assert failed.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
            assert failures == [failed.value]
            assert failed.value.__notes__ == ['Fault audit unavailable: OSError']
            assert connection.in_transaction
            connection.rollback()
            assert not connection.in_transaction
            assert connection.execute('SELECT * FROM temp.audit_probe').fetchall() == []
        finally:
            connection.close()
    assert not scope.active()
