"""Read supervision consumes timing first and the exact kind only when needed."""
from contextlib import closing, contextmanager
import json
import sqlite3

import pytest

from agent_run.core.kernel import _SupervisionWriteRequired
from agent_run.errors import KernelError, ObservationSuperseded
from agent_run.model import Execution, ParentIssue, ReadWait, Requirements, wire_json
from support.repository_scope import add_subject, insert
from support.run_scoped_kernel import ScopedKernel, effect_runs


@contextmanager
def observed_run(monkeypatch, encoding='UTF-8'):
    with effect_runs(monkeypatch) as (original, kernels), closing(sqlite3.connect(':memory:', isolation_level=None)) as copied:
        if encoding == 'UTF-8':
            db, kernel = original, kernels['A']
        else:
            copied.row_factory = sqlite3.Row
            copied.execute("PRAGMA encoding='UTF-16le'")
            copied.executescript('\n'.join(original.iterdump()))
            db, kernel = copied, ScopedKernel(copied, 'A')
        execution = Execution(run_id='A', owner='same-owner', epoch=1)
        demand = kernel.begin_observation('parent:1', execution=execution)
        yield db, kernel, execution, demand.fence, json.loads(demand.fence.scope)


def seed_wait(db, kernel, scope, *, kind='github_convergence', retry=None, purpose='supervision_scope', body=None):
    with kernel.storage.transaction() as tx:
        material = tx.put('parent:1', purpose, wire_json(scope) if body is None else body)
        db.execute('INSERT INTO waits(run_id,subject,kind,scope_id,started_at,deadline,next_retry_at) VALUES(?,?,?,?,?,?,?)',
                   ('A', scope['subject'], kind, material, 100., 200., retry))


@pytest.mark.parametrize('encoding', ['UTF-8', 'UTF-16le'])
@pytest.mark.parametrize('after_read', [False, True])
def test_healthy_read_does_not_adopt_large_unconsumed_kind(monkeypatch, encoding, after_read):
    with observed_run(monkeypatch, encoding) as (db, kernel, _execution, _fence, scope):
        kind = 'k' * (24 * 1024 * 1024 // (2 if encoding == 'UTF-16le' else 1))
        seed_wait(db, kernel, scope, kind=kind)
        assert db.execute('SELECT length(CAST(kind AS BLOB)) FROM waits').fetchone()[0] == 24 * 1024 * 1024
        before = db.total_changes
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            assert kernel._operation_read_wait(tx, run, scope, after_read=after_read, write_timeout=False) is None
        assert tx.bytes_read < 8192
        assert db.total_changes == before


@pytest.mark.parametrize('encoding', ['UTF-8', 'UTF-16le'])
def test_backoff_returns_the_complete_kind_from_the_selected_wait(monkeypatch, encoding):
    with observed_run(monkeypatch, encoding) as (db, kernel, _execution, _fence, scope):
        kind = 'k' * (24 * 1024 * 1024 // (2 if encoding == 'UTF-16le' else 1))
        seed_wait(db, kernel, scope, kind=kind, retry=150.)
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            step = kernel._operation_read_wait(tx, run, scope, after_read=False, write_timeout=False)
        assert (step.kind, step.subject, step.reason, step.retry_at) == ('waiting', 'parent:1', kind, 150.)
        assert 24 * 1024 * 1024 < tx.bytes_read < 25 * 1024 * 1024


@pytest.mark.parametrize('after_read', [False, True])
def test_expired_read_requires_writer_before_consuming_kind_or_timeout_details(monkeypatch, after_read):
    with observed_run(monkeypatch) as (db, kernel, _execution, _fence, scope):
        seed_wait(db, kernel, scope, kind='k' * (24 * 1024 * 1024), retry=150.)
        kernel.clock = lambda: 200.
        before = db.total_changes
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            with pytest.raises(_SupervisionWriteRequired):
                kernel._operation_read_wait(tx, run, scope, after_read=after_read, write_timeout=False)
        assert tx.bytes_read < 8192 and db.total_changes == before


def test_after_read_ignores_backoff_without_decoding_an_unconsumed_kind(monkeypatch):
    with observed_run(monkeypatch) as (db, kernel, _execution, _fence, scope):
        seed_wait(db, kernel, scope, retry=150.)
        db.execute("UPDATE waits SET kind=CAST(X'80' AS TEXT)")
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            assert kernel._operation_read_wait(tx, run, scope, after_read=True, write_timeout=False) is None
        with pytest.raises(sqlite3.OperationalError):
            with kernel.storage.transaction(write=False) as tx:
                run = tx.one("SELECT * FROM runs WHERE id='A'")
                kernel._operation_read_wait(tx, run, scope, after_read=False, write_timeout=False)
        assert not db.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            db.execute('SELECT kind FROM waits').fetchone()


@pytest.mark.parametrize('mismatch', ['run', 'purpose', 'body', 'subject'])
def test_unrelated_material_never_selects_a_supervision_wait(monkeypatch, mismatch):
    with observed_run(monkeypatch) as (db, kernel, _execution, _fence, scope):
        seed_wait(db, kernel, scope, retry=150., purpose='other' if mismatch == 'purpose' else 'supervision_scope',
                  body=b'{}' if mismatch == 'body' else None)
        if mismatch == 'run':
            with ScopedKernel(db, 'B').storage.transaction() as tx:
                other = tx.put('parent:1', 'supervision_scope', wire_json(scope))
            db.execute("UPDATE waits SET run_id='B',scope_id=?", (other,))
        elif mismatch == 'subject':
            # SQL joins must still require the original material owner.
            add_subject(db, 'A', 'another-subject')
            with kernel.storage.transaction() as tx:
                other = tx.put('another-subject', 'supervision_scope', wire_json(scope))
            db.execute('UPDATE waits SET scope_id=?', (other,))
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            assert kernel._operation_read_wait(tx, run, scope, after_read=False, write_timeout=False) is None


def test_necessary_timing_read_denial_still_poisons_owner(monkeypatch):
    with observed_run(monkeypatch) as (db, kernel, _execution, _fence, scope):
        seed_wait(db, kernel, scope)
        def deny(operation, table, column, _database, _source):
            return sqlite3.SQLITE_DENY if (operation, table, column) == (sqlite3.SQLITE_READ, 'waits', 'deadline') else sqlite3.SQLITE_OK
        db.set_authorizer(deny)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                with kernel.storage.transaction(write=False) as tx:
                    run = tx.one("SELECT * FROM runs WHERE id='A'")
                    kernel._operation_read_wait(tx, run, scope, after_read=False, write_timeout=False)
            assert kernel.owners[-1].failed and not db.in_transaction
        finally:
            db.set_authorizer(None)


@pytest.mark.parametrize('change', ['stop', 'epoch'])
def test_timeout_writer_rechecks_changes_after_reader_closed(monkeypatch, change):
    with observed_run(monkeypatch) as (db, kernel, execution, fence, scope):
        seed_wait(db, kernel, scope)
        kernel.clock = lambda: 200.
        transaction = kernel.storage.transaction
        order = []
        @contextmanager
        def changed_before_writer(**options):
            if options.get('write', True):
                assert not db.in_transaction and order == ['reader_closed']
                db.execute("UPDATE runs SET " + ('stopped=1' if change == 'stop' else 'epoch=2') + " WHERE id='A'")
                order.append('writer')
            with transaction(**options) as tx:
                yield tx
            if not options.get('write', True):
                order.append('reader_closed')
        monkeypatch.setattr(kernel.storage, 'transaction', changed_before_writer)
        with pytest.raises(KernelError, match='automatic_step_paused|executor_fenced'):
            kernel._guard_automatic_step(fence, execution)
        assert order == ['reader_closed', 'writer']
        assert not db.execute("SELECT 1 FROM gates WHERE code='supervision_timeout'").fetchall()


@pytest.mark.parametrize('failure', [OSError('read close'), InterruptedError('read cancelled')])
def test_expired_reader_cleanup_failure_prevents_writer(monkeypatch, failure):
    with observed_run(monkeypatch) as (db, kernel, execution, fence, scope):
        seed_wait(db, kernel, scope)
        kernel.clock = lambda: 200.
        transaction = kernel.storage.transaction
        writes = []
        @contextmanager
        def failed_close(**options):
            if options.get('write', True):
                writes.append(True)
            with transaction(**options) as tx:
                yield tx
            raise failure
        monkeypatch.setattr(kernel.storage, 'transaction', failed_close)
        with pytest.raises(type(failure)) as caught:
            kernel._guard_automatic_step(fence, execution)
        assert caught.value is failure and writes == []
        assert not db.in_transaction


@pytest.mark.parametrize('large_kind', [False, True])
def test_backoff_keeps_original_guard_boundary_after_demand_reader(monkeypatch, large_kind):
    with observed_run(monkeypatch) as (db, kernel, execution, _fence, scope):
        kind = 'k' * (24 * 1024 * 1024) if large_kind else 'github_convergence'
        seed_wait(db, kernel, scope, kind=kind, retry=150.)
        count = len(kernel.owners)
        with pytest.raises(ReadWait) as caught:
            kernel.begin_observation('parent:1', execution=execution)
        assert (caught.value.step.retry_at, caught.value.step.reason) == (150., kind)
        assert len(kernel.owners) == count + 2
        assert not db.in_transaction


def test_deadline_crossed_during_timing_read_uses_original_writer(monkeypatch):
    from agent_run.storage.sqlite import Transaction
    with observed_run(monkeypatch) as (db, kernel, execution, _fence, scope):
        seed_wait(db, kernel, scope)
        now = [199.]
        kernel.clock = lambda: now[0]
        rows = Transaction.rows
        def cross_during_sql(owner, sql, parameters=(), **options):
            result = rows(owner, sql, parameters, **options)
            if sql.startswith('SELECT w.rowid AS wait_rowid'):
                now[0] = 200.
            return result
        monkeypatch.setattr(Transaction, 'rows', cross_during_sql)
        with pytest.raises(ReadWait) as caught:
            kernel.begin_observation('parent:1', execution=execution)
        assert caught.value.step.reason == 'supervision_timeout'
        assert db.execute("SELECT started_at,deadline FROM waits WHERE run_id='A'").fetchone()[:] == (100., 200.)
        assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'supervision_timeout'


def test_abandon_ownership_material_keeps_original_guard_boundary(monkeypatch):
    with observed_run(monkeypatch) as (db, kernel, execution, _fence, _scope):
        with kernel.storage.transaction() as tx:
            receipt = tx.put('parent:1', 'abandonment', wire_json({'abandonment': {
                'execution': {'run_id': 'A', 'owner': 'same-owner', 'epoch': 1}, 'unsettled': []}}))
            insert(db, 'actions', {'id': 'abandon', 'run_id': 'A', 'kind': 'abandon',
                'subject': 'parent:1', 'status': 'pending', 'receipt_id': receipt})
            db.execute("UPDATE runs SET epoch=2 WHERE id='A'")
        count = len(kernel.owners)
        request = kernel.begin_observation('parent:1', execution=execution)
        assert json.loads(request.fence.scope)['abandon_action'] == 'abandon'
        assert len(kernel.owners) == count + 2


def test_final_ancestry_collection_keeps_original_guard_boundary(monkeypatch):
    with observed_run(monkeypatch) as (db, kernel, execution, _fence, _scope):
        insert(db, 'final_target', {'id': 'final:1', 'run_id': 'A', 'source_ref': 'refs/heads/run',
            'target_ref': 'refs/heads/main', 'checkout': '/fixture/run', 'owned_root': '/fixture', 'common_git_dir': '/fixture/git'})
        count = len(kernel.owners)
        request = kernel.begin_observation(None, execution=execution)
        assert request.fence.subject is None and request.query.ancestry_pairs == ()
        assert len(kernel.owners) == count + 2


def test_abandon_after_short_observation_still_invalidates_source_acceptance(monkeypatch):
    with observed_run(monkeypatch) as (db, kernel, execution, fence, _scope):
        with kernel.storage.transaction() as tx:
            receipt = tx.put('parent:1', 'abandonment', wire_json({'abandonment': {
                'execution': {'run_id': 'A', 'owner': 'same-owner', 'epoch': 1}, 'unsettled': []}}))
            insert(db, 'actions', {'id': 'abandon', 'run_id': 'A', 'kind': 'abandon',
                'subject': 'parent:1', 'status': 'pending', 'receipt_id': receipt})
            db.execute("UPDATE runs SET epoch=2 WHERE id='A'")
        from agent_run.core.operation_contract import RequirementsRead
        request = RequirementsRead(fence, 1)
        before = db.total_changes
        with pytest.raises(ObservationSuperseded):
            kernel.accept_requirements(request, Requirements(ParentIssue(1, 'Parent', 'Requirement'), ()))
        assert db.total_changes == before


@pytest.mark.parametrize('failure', [TimeoutError('watchdog'), InterruptedError('cancel')])
def test_short_observation_clock_failure_preserves_original_exception(monkeypatch, failure):
    with observed_run(monkeypatch) as (db, kernel, execution, _fence, scope):
        seed_wait(db, kernel, scope)
        def clock():
            raise failure
        kernel.clock = clock
        before = db.total_changes
        with pytest.raises(type(failure)) as caught:
            kernel.begin_observation('parent:1', execution=execution)
        assert caught.value is failure
        assert db.total_changes == before and not db.in_transaction
