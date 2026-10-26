"""Inactive Task/Run atomic seam: real SQLite, no Host or child processes.

These tests are supplied for independent execution. The transaction injection
keeps the original Transaction owner and real BEGIN/COMMIT/rollback; it is not a
new production store and does not activate discovery or the repository protocol.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.core.task_creation import (
    create_repository_task, ensure_idle_task, reconcile_repository_task,
)
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.storage.repository_schema import REPOSITORY_TASK_SCHEMA
from agent_run.storage.run_scope import RunTransaction, transaction_scope
from agent_run.storage.save_order import remaining
from agent_run.storage.sqlite import Transaction
from agent_run.storage.sqlite import Storage
from support.offline import fixture_inputs
from support.repository_scope import connection
from support.repository_scope import insert


class Writer:
    """Existing owner seam; never executes product preflight or a new protocol."""
    def __init__(self, path):
        self.path = path
        self.deadlines = []
        self.owners = []
        self.commit_reply_lost = False
        self.before_commit_failure = False
        self.readback_failure = False
        self.after_begin = None

    @contextmanager
    def transaction(self, *, ordered=True, deadline=None):
        assert not ordered
        self.deadlines.append(deadline)
        allowance = 1. if deadline is None else remaining(deadline)
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=allowance)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            owner = Transaction(db)
            self.owners.append(owner)
            try:
                if self.readback_failure and len(self.deadlines) > 1:
                    raise sqlite3.OperationalError('Controlled readback unavailable')
                if self.after_begin is not None:
                    self.after_begin()
                if deadline is not None:
                    remaining(deadline)
                yield owner
                if owner.failed:
                    raise KernelError('transaction_aborted')
                if self.before_commit_failure:
                    self.before_commit_failure = False
                    raise sqlite3.OperationalError('Controlled pre-COMMIT failure')
                db.commit()
                if self.commit_reply_lost:
                    self.commit_reply_lost = False
                    raise OSError('Controlled loss after a real successful COMMIT')
            except BaseException:
                db.rollback()
                raise


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / 'repository.sqlite3'
    with closing(connection(path)) as db:
        db.executescript(REPOSITORY_TASK_SCHEMA)
        writer = Writer(path)
        kernel = Kernel.__new__(Kernel)
        kernel.storage = SimpleNamespace(transaction=writer.transaction, path=path)
        kernel.clock = lambda: 100.
        task = TaskKey(tmp_path / 'workspace', 'example/project', 1)
        yield db, kernel, writer, task


def proposal(task, run='A', *, language='zh'):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery('parent:' + str(task.parent_number), DeliveryKind.PARENT_ONLY,
        task.parent_number, 'unobserved', base, 'refs/heads/work', 'refs/heads/main')
    return RunSpec(run, task.repository, task.parent_number, 'p', 'g', (delivery,),
        language=language, repository_root=str(task.workspace), inputs=fixture_inputs())


def source(task):
    return Requirements(ParentIssue(task.parent_number, 'Parent', 'Required behavior'), ())


def create(kernel, task, run='A', **kwargs):
    return create_repository_task(kernel, task, proposal(task, run, **kwargs), source(task))


def snapshot(db):
    return tuple(db.iterdump())


def test_idle_task_is_independent_and_has_no_run_or_execution_facts(scenario):
    db, _, writer, task = scenario
    with writer.transaction(ordered=False) as owner:
        ensure_idle_task(owner, task)
        ensure_idle_task(owner, task)
        assert owner.one('SELECT current_run FROM tasks')[0] is None
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    for table in ('runs', 'subjects', 'actions', 'invocations', 'invocation_outputs', 'budgets', 'hosts'):
        assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_metadata_owner_does_not_infer_a_run_or_relax_global_business_scope(scenario):
    db, _, writer, task = scenario
    with writer.transaction(ordered=False) as owner:
        owner.rows_read, owner.bytes_read = 7, 19
        with pytest.raises(KernelError, match='run_scope_required'):
            transaction_scope(owner)
        ensure_idle_task(owner, task)
        assert owner.rows_read >= 7 and owner.bytes_read >= 19 and not owner.failed
        assert owner.one('SELECT count(*) FROM runs')[0] == 0
        before = owner.rows_read, owner.bytes_read
        with pytest.raises(KernelError, match='run_scope_required'):
            transaction_scope(owner)
        assert (owner.rows_read, owner.bytes_read) == before
    assert db.execute('SELECT current_run FROM tasks').fetchone()[0] is None


@pytest.mark.parametrize('invalid', ['outside_transaction', 'run_view', 'poison', 'query_only', 'foreign_keys_off'])
def test_metadata_seam_rejects_invalid_owner_without_task_writes(scenario, invalid):
    db, kernel, _, task = scenario
    if invalid == 'run_view':
        create(kernel, task)
    before = snapshot(db)
    if invalid == 'foreign_keys_off':
        db.execute('PRAGMA foreign_keys=OFF')
    if invalid != 'outside_transaction':
        db.execute('BEGIN IMMEDIATE')
    owner = Transaction(db)
    if invalid == 'run_view':
        owner = RunTransaction(owner, 'A')
    elif invalid == 'poison':
        owner.failed = True
    elif invalid == 'query_only':
        db.execute('PRAGMA query_only=ON')
    try:
        code = 'transaction_aborted' if invalid == 'poison' else 'task_creation_transaction_required'
        with pytest.raises(KernelError, match=code):
            ensure_idle_task(owner, task)
        if invalid == 'poison':
            assert owner.failed
    finally:
        db.rollback()
        db.execute('PRAGMA query_only=OFF')
        db.execute('PRAGMA foreign_keys=ON')
    assert snapshot(db) == before


def test_creation_freezes_and_binds_in_one_owner_with_no_locator_or_allocation(scenario):
    db, kernel, writer, task = scenario
    result = create(kernel, task)
    assert result.created and result.run_id == result.current_run == 'A'
    assert len(writer.owners) == 1 and not writer.owners[0].failed
    assert db.execute('SELECT current_run FROM tasks').fetchone()[0] == 'A'
    assert db.execute('SELECT task_id,run_id FROM task_runs').fetchone()[:] == (task.fingerprint, 'A')
    assert db.execute('SELECT language FROM runs').fetchone()[0] == 'zh'
    assert db.execute('SELECT count(*) FROM run_inputs').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM profile_revisions').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM source_observations').fetchone()[0] == 1
    for table in ('attempts', 'threads', 'invocations', 'invocation_outputs', 'hosts', 'leases', 'waits'):
        assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    assert db.execute('SELECT sum(developments_used+reviews_used) FROM budgets').fetchone()[0] == 0
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    assert not list(task.workspace.parent.rglob('*.json'))


def test_retry_and_losing_proposal_reuse_original_frozen_values_without_orphans(scenario):
    db, kernel, _, task = scenario
    create(kernel, task)
    before = snapshot(db)
    assert not create(kernel, task, 'loser', language='en').created
    assert not create(kernel, task, 'A', language='en').created
    assert snapshot(db) == before
    assert db.execute('SELECT id,language FROM runs').fetchall()[0][:] == ('A', 'zh')


@pytest.mark.parametrize('table,operation', [
    ('runs', 'INSERT'), ('subjects', 'INSERT'), ('budgets', 'INSERT'),
    ('resync_allowances', 'INSERT'), ('material', 'INSERT'), ('profile_revisions', 'INSERT'),
    ('run_inputs', 'INSERT'), ('source_observations', 'INSERT'), ('requirement_versions', 'INSERT'),
    ('task_runs', 'INSERT'), ('tasks', 'UPDATE'),
])
def test_failure_at_each_creation_write_rolls_back_entire_run_and_keeps_sibling(scenario, table, operation):
    db, kernel, writer, task = scenario
    sibling = TaskKey(task.workspace, task.repository, 2)
    create(kernel, sibling, 'sibling')
    with writer.transaction(ordered=False) as owner:
        ensure_idle_task(owner, task)
    db.execute(f"CREATE TRIGGER controlled_failure BEFORE {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'Controlled creation failure'); END")
    before = snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match='Controlled creation failure'):
        create(kernel, task)
    assert snapshot(db) == before
    assert db.execute("SELECT count(*) FROM runs WHERE id='A'").fetchone()[0] == 0
    assert db.execute('SELECT current_run FROM tasks WHERE id=?', (task.fingerprint,)).fetchone()[0] is None
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_real_commit_with_lost_reply_is_reconciled_once_on_same_deadline_and_reopened_handle(scenario):
    db, kernel, writer, task = scenario
    writer.commit_reply_lost = True
    result = create(kernel, task)
    assert result.created and result.run_id == 'A'
    assert len(writer.deadlines) == 2 and writer.deadlines[0] == writer.deadlines[1]
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 1
    before = snapshot(db)
    assert reconcile_repository_task(kernel, task, 'A').run_id == 'A'
    assert snapshot(db) == before


def test_uncommitted_or_unreadable_commit_outcome_never_recreates(scenario):
    db, kernel, writer, task = scenario
    writer.before_commit_failure = True
    with pytest.raises(KernelError, match='task_creation_unknown'):
        create(kernel, task)
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert len(writer.deadlines) == 2 and writer.deadlines[0] == writer.deadlines[1]
    writer.deadlines.clear()
    writer.commit_reply_lost = writer.readback_failure = True
    with pytest.raises(sqlite3.OperationalError, match='Controlled readback unavailable'):
        create(kernel, task)
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    assert db.execute('SELECT current_run FROM tasks').fetchone()[0] == 'A'


def test_expired_original_deadline_cannot_receive_a_new_reconciliation_budget(scenario, monkeypatch):
    db, kernel, writer, task = scenario
    import agent_run.core.task_creation as creation
    import agent_run.storage.save_order as order
    clock = [100.]
    monkeypatch.setattr(creation, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(order, 'monotonic', lambda: clock[0])
    original = writer.transaction
    @contextmanager
    def delayed_commit(**kwargs):
        with original(**kwargs) as owner:
            yield owner
        clock[0] = 102.
        raise OSError('Reply lost after original contention allowance expired')
    kernel.storage.transaction = delayed_commit
    with pytest.raises(KernelError, match='storage_busy'):
        create(kernel, task)
    assert len(writer.deadlines) == 1
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1


def test_foreign_task_current_run_and_same_id_proposal_are_rejected(scenario):
    db, kernel, writer, task = scenario
    create(kernel, task)
    other = TaskKey(task.workspace, task.repository, 2)
    with writer.transaction(ordered=False) as owner:
        ensure_idle_task(owner, other)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_location_identity'):
        create(kernel, other, 'A')
    assert snapshot(db) == before
    with pytest.raises(sqlite3.IntegrityError):
        with writer.transaction(ordered=False) as owner:
            owner.execute('UPDATE tasks SET current_run=? WHERE id=?', ('A', other.fingerprint))
    assert snapshot(db) == before
    with pytest.raises(sqlite3.IntegrityError):
        with writer.transaction(ordered=False) as owner:
            owner.execute('INSERT INTO task_runs VALUES (?,?,?,?,?)',
                (other.fingerprint, 'A', other.repository, str(other.workspace), other.parent_number))
    assert snapshot(db) == before


@pytest.mark.parametrize('registered', [False, True])
def test_known_foreign_proposal_is_not_unknown_for_an_absent_or_idle_task(scenario, registered):
    db, kernel, writer, task = scenario
    create(kernel, task)
    other = TaskKey(task.workspace, task.repository, 2)
    if registered:
        with writer.transaction(ordered=False) as owner:
            ensure_idle_task(owner, other)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_location_identity'):
        reconcile_repository_task(kernel, other, 'A')
    assert snapshot(db) == before
    with pytest.raises(KernelError, match='task_location_identity'):
        create(kernel, other, 'A')
    assert snapshot(db) == before
    # No membership/loser alias is manufactured for the caller by either path.
    assert db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 1
    assert db.execute('SELECT task_id FROM task_runs').fetchone()[0] == task.fingerprint


def test_complete_successor_and_old_proposal_readback_do_not_rebind_current(scenario):
    db, kernel, _, task = scenario
    create(kernel, task)
    db.execute("UPDATE subjects SET phase='complete' WHERE run_id='A'")
    assert create(kernel, task, 'B', language='en').created
    before = snapshot(db)
    prior = reconcile_repository_task(kernel, task, 'A')
    assert prior.run_id == 'A' and prior.current_run == 'B'
    repeated = create(kernel, task, 'A')
    assert repeated.run_id == 'A' and repeated.current_run == 'B' and not repeated.created
    assert snapshot(db) == before


@pytest.mark.parametrize('guard', ['executor', 'retired_executor', 'host_unknown', 'seat', 'effect_unknown', 'cleanup'])
def test_completed_jobs_do_not_bypass_original_replacement_guards(scenario, guard):
    db, kernel, _, task = scenario
    create(kernel, task)
    db.execute("UPDATE subjects SET phase='complete' WHERE run_id='A'")
    # Register precisely the existing relational guard; no SQL state transition
    # is presented as a public Host/process or business lifecycle proof.
    if guard == 'executor':
        db.execute("UPDATE runs SET executor='owner' WHERE id='A'")
    elif guard == 'retired_executor':
        db.execute("INSERT INTO retired_executors VALUES ('A','owner',1,'unknown')")
    elif guard in {'host_unknown', 'seat', 'effect_unknown'}:
        db.execute('BEGIN IMMEDIATE')
        tx = RunTransaction(Transaction(db), 'A')
        material = tx.put('parent:1', 'controlled_guard', b'{}')
        if guard == 'host_unknown':
            insert(db, 'actions', {'run_id': 'A', 'id': 'original-action', 'kind': 'resume',
                'payload_id': material, 'status': 'applied'})
            insert(db, 'hosts', {'run_id': 'A', 'epoch': 1, 'owner': 'owner', 'nonce': 'nonce-A',
                'action_id': 'original-action', 'subject': 'parent:1', 'request_id': material,
                'backend': 'foreground', 'allocated_at': 100., 'status': 'unknown'})
        elif guard == 'seat':
            insert(db, 'waits', {'run_id': 'A', 'subject': 'parent:1', 'kind': 'agent_preparation',
                'scope_id': material, 'started_at': 100., 'deadline': 700.})
        else:
            insert(db, 'effects', {'run_id': 'A', 'id': 'unknown-effect', 'subject': 'parent:1',
                'operation': 'inspect_resources', 'status': 'sent', 'executor': 'owner', 'epoch': 1,
                'parent_revision': 'p', 'graph_revision': 'g', 'revision': 'r', 'generation': 1,
                'source_ref': 'refs/heads/work', 'target_ref': 'refs/heads/main', 'before_sha': 'a' * 40,
                'base_sha': 'a' * 40, 'base_tree': 'b' * 40, 'payload_id': material})
        db.commit()
    else:
        db.execute("INSERT INTO resource_cleanup(run_id,subject,generation,state,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir) "
            "VALUES ('A','parent:1',1,'pending','refs/heads/work',?,'',?,'','')", ('a' * 40, 'a' * 40))
    before = snapshot(db)
    result = create(kernel, task, 'B')
    assert result.run_id == 'A' and not result.created
    assert snapshot(db) == before


def test_two_real_connections_compete_without_overwriting_winner_or_leaving_loser(scenario):
    db, kernel, first_writer, task = scenario
    entered, release, queued = Event(), Event(), Event()
    results, failures = [], []
    def hold_first():
        entered.set()
        if not release.wait(.5):
            raise AssertionError('Bounded writer barrier was not released')
    first_writer.after_begin = hold_first
    second_writer = Writer(first_writer.path)
    second_kernel = Kernel.__new__(Kernel)
    second_kernel.storage = SimpleNamespace(transaction=second_writer.transaction, path=first_writer.path)
    second_kernel.clock = lambda: 100.
    def invoke(target, run, language):
        try:
            if run == 'loser':
                queued.set()
            results.append(create(target, task, run, language=language))
        except BaseException as error:
            failures.append(error)
    first = Thread(target=invoke, args=(kernel, 'winner', 'zh'))
    second = Thread(target=invoke, args=(second_kernel, 'loser', 'en'))
    try:
        first.start()
        assert entered.wait(.5)
        second.start()
        assert queued.wait(.5)
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
    assert not failures and len(results) == 2
    assert {result.run_id for result in results} == {'winner'}
    assert sum(result.created for result in results) == 1
    assert db.execute('SELECT id,language FROM runs').fetchone()[:] == ('winner', 'zh')
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_caught_sql_failure_poison_is_shared_and_prevents_task_publication(scenario, monkeypatch):
    db, kernel, writer, task = scenario
    original = kernel._create_run
    def poisoned(owner, *args, **kwargs):
        original(owner, *args, **kwargs)
        selected = RunTransaction(owner, 'A')
        try:
            selected.execute('INSERT INTO missing_controlled_table VALUES (1)')
        except sqlite3.OperationalError:
            pass
        assert selected.failed and owner.failed
    monkeypatch.setattr(kernel, '_create_run', poisoned)
    with pytest.raises(KernelError, match='transaction_aborted'):
        create(kernel, task)
    assert writer.owners[0].failed
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_creation_views_preserve_original_owner_accumulated_read_budget(scenario, monkeypatch):
    db, kernel, writer, task = scenario
    original = kernel._create_run
    def constrained(owner, *args, **kwargs):
        owner.rows_read = 65536
        original(owner, *args, **kwargs)
    monkeypatch.setattr(kernel, '_create_run', constrained)
    with pytest.raises(KernelError, match='query_limit'):
        create(kernel, task)
    assert writer.owners[0].rows_read == 65536
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_unknown_proposal_or_unbound_header_is_never_adopted(scenario):
    db, kernel, writer, task = scenario
    with writer.transaction(ordered=False) as owner:
        ensure_idle_task(owner, task)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_creation_unknown'):
        reconcile_repository_task(kernel, task, 'not-submitted')
    assert snapshot(db) == before
    create(kernel, task)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_creation_unknown'):
        reconcile_repository_task(kernel, task, 'unallocated-loser')
    assert snapshot(db) == before
    unbound = proposal(task, 'unbound')
    with writer.transaction(ordered=False) as owner:
        kernel._create_run(owner, unbound, kernel._creation_profiles(unbound),
            requirements=source(task), scoped=True)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_creation_unknown'):
        create(kernel, task, 'unbound')
    assert snapshot(db) == before
    other = TaskKey(task.workspace, task.repository, 2)
    with writer.transaction(ordered=False) as owner:
        ensure_idle_task(owner, other)
    before = snapshot(db)
    # This Run has no existing association, so failure proves the full
    # Task/Run identity foreign key, not just UNIQUE(run_id).
    with pytest.raises(sqlite3.IntegrityError):
        with writer.transaction(ordered=False) as owner:
            owner.execute('INSERT INTO task_runs VALUES (?,?,?,?,?)',
                (other.fingerprint, 'unbound', other.repository, str(other.workspace), other.parent_number))
    assert snapshot(db) == before


@pytest.mark.parametrize('missing', ['inputs', 'root'])
def test_incomplete_creation_request_is_rejected_before_any_task_is_written(scenario, missing):
    db, kernel, _, task = scenario
    spec = proposal(task)
    spec = replace(spec, inputs=None) if missing == 'inputs' else replace(spec, repository_root=None)
    before = snapshot(db)
    with pytest.raises(KernelError, match='task_creation_identity'):
        create_repository_task(kernel, task, spec, source(task))
    assert snapshot(db) == before


def test_active_sibling_task_does_not_block_a_qualified_successor(scenario):
    db, kernel, _, task = scenario
    create(kernel, task)
    sibling = TaskKey(task.workspace, task.repository, 2)
    create(kernel, sibling, 'sibling')
    db.execute("UPDATE subjects SET phase='complete' WHERE run_id='A'")
    sibling_before = tuple(db.execute("SELECT * FROM runs WHERE id='sibling'").fetchone())
    assert create(kernel, task, 'B').created
    assert tuple(db.execute("SELECT * FROM runs WHERE id='sibling'").fetchone()) == sibling_before
    assert db.execute('SELECT current_run FROM tasks WHERE id=?', (sibling.fingerprint,)).fetchone()[0] == 'sibling'
    assert db.execute("SELECT phase FROM subjects WHERE run_id='sibling'").fetchone()[0] == 'development'


def test_completed_abandonment_preserves_original_replacement_exemptions(scenario):
    db, kernel, _, task = scenario
    create(kernel, task)
    insert(db, 'actions', {'run_id': 'A', 'id': 'abandon-A', 'kind': 'abandon', 'status': 'complete'})
    # Existing completed Abandon permits a successor despite incomplete work;
    # it does not relax the independent Host/seat/effect guards tested above.
    assert db.execute("SELECT phase FROM subjects WHERE run_id='A'").fetchone()[0] == 'development'
    assert create(kernel, task, 'B').created
    assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 2


@pytest.mark.parametrize('shorten', [False, True])
def test_original_storage_caps_or_shortens_explicit_contention_deadline(tmp_path, monkeypatch, shorten):
    import agent_run.storage.sqlite as storage_module
    storage = Storage.create(tmp_path / 'current-protocol.sqlite3')
    requested = storage_module.monotonic() + (.5 if shorten else 60.)
    observed = []
    original = storage_module.save_turn
    @contextmanager
    def record(*args, **kwargs):
        observed.append(kwargs['deadline'])
        with original(*args, **kwargs) as identity:
            yield identity
    monkeypatch.setattr(storage_module, 'save_turn', record)
    with storage.transaction(ordered=False, deadline=requested):
        pass
    assert len(observed) == 1
    if shorten:
        assert observed[0] == requested
    else:
        assert requested - observed[0] > 50.


@pytest.mark.parametrize('deadline', [True, False, float('nan'), float('inf'), -float('inf')])
def test_storage_rejects_invalid_absolute_deadline_without_creating_a_database(tmp_path, deadline):
    storage = Storage.__new__(Storage)
    storage.path = tmp_path / 'must-not-create.sqlite3'
    with pytest.raises(ValueError, match='finite absolute'):
        with storage.transaction(deadline=deadline):
            pytest.fail('Invalid deadline entered a transaction')
    assert not storage.path.exists()
