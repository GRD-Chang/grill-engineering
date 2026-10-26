"""UNRUN author plan: single-process SQLite/state transitions, never Host/children.

Clock substitutes exercise decisions, not actual kernel clock comparability.
True separate-process/clock/crash/WAL-cost acceptance remains explicitly pending.
"""
from contextlib import closing
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from agent_run.errors import KernelError
from agent_run.storage import save_order, sqlite as storage_module
from agent_run.storage.repository_schema import REPOSITORY_RUN_SCHEMA, REPOSITORY_TASK_SCHEMA, REPOSITORY_ROUTING_SCHEMA
from agent_run.storage.save_order import RepositorySave, SaveClock, SaveNotHead
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.routing import RepositoryRoute
from agent_run.contracts.task import TaskKey
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from support.offline import fixture_inputs


class DecisionClock(SaveClock):
    def __init__(self, domain='decision-clock'):
        self.domain = domain
        self.closed = False

    def require(self):
        if self.closed:
            raise KernelError('storage_clock_domain')

    def close(self):
        self.closed = True


@pytest.fixture
def queue():
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(REPOSITORY_RUN_SCHEMA + REPOSITORY_TASK_SCHEMA + REPOSITORY_ROUTING_SCHEMA)
        db.execute("INSERT INTO repository_binding VALUES (1,'example/project','/fixture/repository')")
        db.execute('BEGIN IMMEDIATE')
        try:
            yield Transaction(db)
        finally:
            db.rollback()


def request(deadline=None, domain='decision-clock'):
    return RepositorySave(DecisionClock(domain), None,
        save_order.monotonic() + 100 if deadline is None else deadline)


def test_committed_registration_order_and_nonhead_guard_are_not_call_arrival(queue):
    first_call, second_call = request(), request()
    # Model B's committed registration preceding A despite A's earlier call.
    second_call.register(queue)
    first_call.register(queue)
    with pytest.raises(SaveNotHead):
        first_call.admit(queue)
    second_call.admit(queue)
    second_call.finish(queue)
    first_call.admit(queue)
    first_call.finish(queue)
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 0


@pytest.mark.parametrize('operation', ['register', 'admit', 'discard', 'finish'])
def test_foreign_clock_is_not_expired_or_removed_even_when_deadline_looks_old(queue, monkeypatch, operation):
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.)
    foreign = request(deadline=101., domain='other-time-namespace')
    foreign.register(queue)
    before = [tuple(row) for row in queue.all('SELECT * FROM save_admissions')]
    monkeypatch.setattr(save_order, 'monotonic', lambda: 102.)
    local = request(deadline=103.)
    with pytest.raises(KernelError) as failure:
        getattr(local, operation)(queue)
    assert failure.value.code == 'storage_clock_domain'
    assert [tuple(row) for row in queue.all('SELECT * FROM save_admissions')] == before


def test_one_database_capacity_includes_expired_and_unknown_residuals(queue, monkeypatch):
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.)
    tickets = [request(deadline=101.) for _ in range(128)]
    for ticket in tickets:
        ticket.register(queue)
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 128
    with pytest.raises(KernelError) as full:
        request(deadline=101.).register(queue)
    assert full.value.code == 'storage_busy'
    monkeypatch.setattr(save_order, 'monotonic', lambda: 102.)
    next_ticket = request(deadline=103.)
    next_ticket.register(queue)
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 1
    assert queue.one('SELECT request FROM save_admissions')[0] == next_ticket.request


def test_schema_capacity_and_immutable_ticket_are_not_python_only(queue):
    for _ in range(128):
        request().register(queue)
    with pytest.raises(sqlite3.IntegrityError):
        queue.execute('INSERT INTO save_admissions(request,clock_domain,deadline) VALUES (?,?,?)',
            ('f' * 32, 'decision-clock', save_order.monotonic() + 100))
    with pytest.raises(sqlite3.IntegrityError):
        queue.execute('UPDATE save_admissions SET deadline=deadline+1')
    assert queue.failed and queue.one('SELECT count(*) FROM save_admissions')[0] == 128


def test_same_uuid_wrong_binding_and_missing_unknown_ticket_never_insert(queue):
    original = request()
    original.register(queue)
    impostor = request(deadline=original.deadline)
    impostor.request = original.request
    impostor.run_id = 'foreign-run'
    with pytest.raises(KernelError) as wrong:
        impostor.reconcile(queue)
    assert wrong.value.code == 'storage_registration_unknown'
    queue.execute('DELETE FROM save_admissions WHERE request=?', (original.request,))
    with pytest.raises(KernelError) as missing:
        original.reconcile(queue)
    assert missing.value.code == 'storage_registration_unknown'
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 0


def test_admitted_body_completion_is_not_rejected_as_a_new_business_timeout(queue, monkeypatch):
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.)
    ticket = request(deadline=101.)
    ticket.register(queue)
    ticket.admit(queue)
    monkeypatch.setattr(save_order, 'monotonic', lambda: 102.)
    ticket.finish(queue)
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 0


def test_expired_registration_cannot_enter_and_is_not_a_worker_exit_proof(queue, monkeypatch):
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.)
    ticket = request(deadline=101.)
    ticket.register(queue)
    monkeypatch.setattr(save_order, 'monotonic', lambda: 102.)
    with pytest.raises(KernelError) as expired:
        ticket.admit(queue)
    assert expired.value.code == 'storage_busy'
    assert queue.one('SELECT count(*) FROM save_admissions')[0] == 1
    assert queue.one('SELECT count(*) FROM invocations')[0] == 0
    assert queue.one('SELECT count(*) FROM leases')[0] == 0


def test_inherited_request_owner_cannot_claim_or_remove_parent_ticket(queue, monkeypatch):
    ticket = request()
    ticket.register(queue)
    before = [tuple(row) for row in queue.all('SELECT * FROM save_admissions')]
    with monkeypatch.context() as patch:
        patch.setattr(save_order.os, 'getpid', lambda: ticket.owner[0] + 1)
        with pytest.raises(KernelError) as foreign:
            ticket.discard(queue)
    assert foreign.value.code == 'storage_save_owner'
    assert [tuple(row) for row in queue.all('SELECT * FROM save_admissions')] == before


@pytest.mark.parametrize('fault', ['provider', 'offset', 'duplicate', 'child-namespace'])
def test_clock_capture_refuses_unproved_current_monotonic_domain(monkeypatch, fault):
    from agent_run.adapters import foreground
    monkeypatch.setattr(foreground, 'require_proc_view', lambda: None)
    monkeypatch.setattr(save_order, 'get_clock_info', lambda name: SimpleNamespace(
        implementation='unknown' if fault == 'provider' else 'clock_gettime(CLOCK_MONOTONIC)',
        monotonic=True, adjustable=False))
    monkeypatch.setattr(save_order.os, 'fstat', lambda fd: SimpleNamespace(st_dev=1, st_ino=2))
    monkeypatch.setattr(save_order.os, 'stat', lambda path: SimpleNamespace(st_dev=1,
        st_ino=3 if fault == 'child-namespace' else 2))
    def metadata(path, limit):
        if path.name == 'boot_id':
            return '12345678-1234-1234-1234-123456789abc'
        return ('monotonic 0 1000000000\nboottime 0 0' if fault == 'offset' else
                'monotonic 0 0\nmonotonic 0 0' if fault == 'duplicate' else
                'monotonic 0 0\nboottime 0 0')
    monkeypatch.setattr(save_order, '_bounded_text', metadata)
    with pytest.raises(ValueError):
        save_order._clock_fields(123)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, 'SaveClock', DecisionClock)
    route = RepositoryRoute(tmp_path / 'state/kernel.sqlite3', tmp_path / 'repository', 'example/project')
    route.initialize()
    for name, parent in [('A', 1), ('B', 2)]:
        base = Commit('a' * 40, 'b' * 40)
        delivery = Delivery('same-subject', DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
            'refs/heads/' + name, 'refs/heads/main')
        spec = RunSpec(name, route.repository, parent, 'r', 'g', (delivery,), language='en',
            repository_root=str(route.repository_root), inputs=fixture_inputs())
        route.create(TaskKey(route.repository_root, route.repository, parent), spec,
            Requirements(ParentIssue(parent, 'Parent', 'Original input'), ()))
    return route


def scalar(route, sql, params=()):
    with route.metadata().transaction(write=False) as tx:
        return tx.one(sql, params)[0]


def test_normal_selected_owner_one_body_two_commits_and_brother_unchanged(repository, monkeypatch):
    original = storage_module.sqlite3.connect
    commits = []
    class Recorded(sqlite3.Connection):
        def commit(self):
            commits.append(self.in_transaction and self.execute('PRAGMA query_only').fetchone()[0] == 0)
            return super().commit()
    monkeypatch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=Recorded))
    bodies = []
    with repository.at_run('A').transaction() as tx:
        bodies.append(tx.run_id)
        tx.execute('UPDATE runs SET stopped=1 WHERE id=?', ('A',))
        assert tx.connection.execute('PRAGMA wal_autocheckpoint').fetchone()[0] > 0
    assert bodies == ['A'] and commits.count(True) == 2
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 1
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('B',)) == 0
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


@pytest.mark.parametrize('error_code,expected', [(sqlite3.SQLITE_FULL, 'storage_limit'),
    (sqlite3.SQLITE_INTERRUPT, 'transaction_aborted')])
def test_noncontention_begin_error_is_not_retried_as_metadata(repository, monkeypatch, error_code, expected):
    original = sqlite3.connect
    attempts = []
    class Refused(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                attempts.append(sql)
                error = sqlite3.OperationalError('controlled original category')
                error.sqlite_errorcode, error.sqlite_errorname = error_code, 'CONTROLLED'
                raise error
            return super().execute(sql, parameters)
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=Refused))
        with pytest.raises(KernelError) as failed:
            with repository.at_run('A').transaction():
                pytest.fail('Business body entered after rejected metadata BEGIN')
    assert failed.value.code == expected and len(attempts) == 1
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_only_ended_begin_contention_retries_before_body(repository, monkeypatch):
    original = sqlite3.connect
    attempts = []
    class Contended(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError('controlled admission contention')
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return super().execute(sql, parameters)
    monkeypatch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=Contended))
    bodies = []
    with repository.at_run('A').transaction() as tx:
        bodies.append(tx.run_id)
    assert bodies == ['A'] and len(attempts) == 3  # busy registration, register, original body
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


@pytest.mark.parametrize('after_commit', [False, True])
def test_registration_commit_unknown_reads_only_original_uuid_and_never_reinserts(repository, monkeypatch, after_commit):
    original = sqlite3.connect
    fired, inserts, bodies = [], [], []
    class LostReply(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO save_admissions'):
                inserts.append(parameters[0])
            return super().execute(sql, parameters)
        def commit(self):
            if not fired and self.in_transaction and self.execute('SELECT count(*) FROM save_admissions').fetchone()[0] == 1:
                fired.append(True)
                if after_commit:
                    super().commit()
                raise OSError('controlled registration COMMIT reply loss')
            return super().commit()
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=LostReply))
        if after_commit:
            with repository.at_run('A').transaction() as tx:
                bodies.append(tx.run_id)
        else:
            with pytest.raises(KernelError) as unknown:
                with repository.at_run('A').transaction():
                    bodies.append('unexpected')
            assert unknown.value.code == 'storage_registration_unknown'
    assert len(inserts) == 1 and bodies == (['A'] if after_commit else [])
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_business_commit_unknown_keeps_actual_write_and_never_replays_body(repository, monkeypatch):
    original = sqlite3.connect
    entered, fired = [], []
    class LostBusinessReply(sqlite3.Connection):
        def commit(self):
            if entered and not fired and self.in_transaction:
                fired.append(True)
                super().commit()
                raise OSError('controlled business COMMIT reply loss')
            return super().commit()
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=LostBusinessReply))
        with pytest.raises(OSError):
            with repository.at_run('A').transaction() as tx:
                entered.append(tx.run_id)
                tx.execute('UPDATE runs SET stopped=1 WHERE id=?', ('A',))
    assert entered == ['A'] and fired == [True]
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 1
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_final_cancel_sample_rejects_body_and_cleans_only_original_registration(repository, monkeypatch):
    cancelled = Event()
    original = RepositorySave.admit
    def cancel_after_head(ticket, tx):
        original(ticket, tx)
        cancelled.set()
    monkeypatch.setattr(RepositorySave, 'admit', cancel_after_head)
    with pytest.raises(InterruptedError):
        with repository.at_run('A').transaction(cancelled=cancelled):
            pytest.fail('Final cancellation did not prevent body entry')
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 0


def test_cancel_cleanup_uses_no_new_allowance_and_preserves_primary_error(repository, monkeypatch):
    cancelled = Event()
    original = RepositorySave.admit
    def cancel_and_expire(ticket, tx):
        original(ticket, tx)
        cancelled.set()
        patch.setattr(save_order, 'monotonic', lambda: ticket.deadline + 1)
    with monkeypatch.context() as patch:
        patch.setattr(RepositorySave, 'admit', cancel_and_expire)
        with pytest.raises(InterruptedError) as primary:
            with repository.at_run('A').transaction(cancelled=cancelled):
                pytest.fail('Expired cancellation entered body')
    assert any('Save registration cleanup unavailable' in note for note in primary.value.__notes__)
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 1
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 0


def test_confirmed_business_commit_does_not_fail_from_expiry_or_cleanup(repository, monkeypatch, capsys):
    clocks = []
    class CloseError(DecisionClock):
        def __init__(self):
            super().__init__()
            clocks.append(self)
        def close(self):
            super().close()
            raise OSError('controlled clock close failure after successful commit')
    monkeypatch.setattr(storage_module, 'SaveClock', CloseError)
    metadata = Storage._save_metadata
    def no_postcommit_discard(self, ticket, identity, cancelled, operation, commit=None):
        assert operation != 'discard', 'Confirmed successful business unexpectedly ran cleanup writer'
        return metadata(self, ticket, identity, cancelled, operation, commit)
    monkeypatch.setattr(Storage, '_save_metadata', no_postcommit_discard)
    with monkeypatch.context() as patch:
        with repository.at_run('A').transaction() as tx:
            tx.execute('UPDATE runs SET stopped=1 WHERE id=?', ('A',))
            patch.setattr(save_order, 'monotonic', lambda: 1.e30)
    assert clocks[0].closed and 'Storage save cleanup unavailable' in capsys.readouterr().err
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 1
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_shared_owner_poison_rolls_back_whole_body_without_reentry(repository):
    bodies = []
    with pytest.raises(KernelError) as poisoned:
        with repository.at_run('A').transaction() as tx:
            bodies.append(tx.run_id)
            tx.execute('UPDATE runs SET stopped=1 WHERE id=?', ('A',))
            with pytest.raises(sqlite3.OperationalError):
                tx.execute('UPDATE nonexistent_table SET value=1')
            assert tx.failed
    assert poisoned.value.code == 'transaction_aborted' and bodies == ['A']
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 0
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_readonly_and_exact_control_bypass_do_not_capture_clock_or_touch_tickets(repository, monkeypatch):
    def forbidden():
        raise AssertionError('Readonly or explicit control entered FIFO clock capture')
    monkeypatch.setattr(storage_module, 'SaveClock', forbidden)
    with repository.at_run('A').transaction(write=False) as tx:
        assert tx.run_id == 'A'
    with repository.at_run('A').transaction(ordered=False) as tx:
        tx.execute('UPDATE runs SET stopped=1 WHERE id=?', ('A',))
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0
    assert scalar(repository, 'SELECT stopped FROM runs WHERE id=?', ('A',)) == 1


def test_nested_same_database_control_is_rejected_without_deadlock_or_body_replay(repository):
    bodies = []
    with repository.at_run('A').transaction() as tx:
        bodies.append(tx.run_id)
        with pytest.raises(KernelError) as nested:
            with repository.at_run('B').transaction(ordered=False):
                pytest.fail('Nested sibling writer entered')
        assert nested.value.code == 'storage_busy'
    assert bodies == ['A'] and scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


def test_afterfork_reset_retains_no_parent_local_wait_owner(monkeypatch):
    condition, gates = save_order._condition, save_order._gates
    with monkeypatch.context() as patch:
        patch.setattr(save_order, '_condition', condition)
        patch.setattr(save_order, '_gates', {(1, 2): object()})
        save_order._after_fork()
        assert save_order._gates == {} and save_order._condition is not condition


@pytest.mark.parametrize('error_code,expected', [(sqlite3.SQLITE_FULL, 'storage_limit'),
    (sqlite3.SQLITE_INTERRUPT, 'transaction_aborted')])
def test_registration_commit_known_failure_keeps_category_and_never_registers_again(repository, monkeypatch, error_code, expected):
    original = sqlite3.connect
    inserts, fired = [], []
    class RefusedCommit(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO save_admissions'):
                inserts.append(parameters[0])
            return super().execute(sql, parameters)
        def commit(self):
            if not fired and self.in_transaction and self.execute('SELECT count(*) FROM save_admissions').fetchone()[0] == 1:
                fired.append(True)
                error = sqlite3.OperationalError('controlled known COMMIT category')
                error.sqlite_errorcode, error.sqlite_errorname = error_code, 'CONTROLLED'
                raise error
            return super().commit()
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=RefusedCommit))
        with pytest.raises(KernelError) as failed:
            with repository.at_run('A').transaction():
                pytest.fail('Known registration failure entered body')
    assert failed.value.code == expected and len(inserts) == 1
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0


@pytest.mark.parametrize('changed', [False, True])
def test_clock_pin_requires_same_domain_and_closes_its_descriptor_once(monkeypatch, changed):
    fields = ('12345678-1234-1234-1234-123456789abc',
        'clock_gettime(CLOCK_MONOTONIC)', 1, 2, 0, 0)
    calls, closed = [], []
    def capture(descriptor):
        calls.append(descriptor)
        return (*fields[:-1], 1) if changed and len(calls) > 1 else fields
    with monkeypatch.context() as patch:
        patch.setattr(save_order.os, 'open', lambda *args: 123)
        patch.setattr(save_order.os, 'close', lambda descriptor: closed.append(descriptor))
        patch.setattr(save_order, '_clock_fields', capture)
        if changed:
            with pytest.raises(KernelError) as refused:
                SaveClock()
            assert refused.value.code == 'storage_clock_domain'
        else:
            clock = SaveClock()
            clock.require()
            clock.close()
    assert closed == [123] and len(calls) >= 2


def test_clock_ownership_is_closed_when_request_construction_fails(repository, monkeypatch):
    clocks = []
    class RecordedClock(DecisionClock):
        def __init__(self):
            super().__init__()
            clocks.append(self)
    def unavailable(*args):
        raise OSError('controlled request construction failure before any registration')
    monkeypatch.setattr(storage_module, 'SaveClock', RecordedClock)
    monkeypatch.setattr(storage_module, 'RepositorySave', unavailable)
    with pytest.raises(OSError):
        with repository.at_run('A').transaction():
            pytest.fail('Business entered without a request')
    assert len(clocks) == 1 and clocks[0].closed
    assert scalar(repository, 'SELECT count(*) FROM save_admissions') == 0
