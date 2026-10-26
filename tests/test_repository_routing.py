"""Actual-file inactive repository routing, no Host/Worker or child process.

The public marker stays unsupported. Tests call explicit internal factories;
metadata, SQL/WAL, original owner and frozen creation are the exercised seams.
"""
from dataclasses import replace
from contextlib import closing
import sqlite3

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.storage import routing
from agent_run.storage.preflight import UnsupportedState, require_supported_state
from agent_run.storage.repository_schema import STAGED_REPOSITORY_PROTOCOL
from agent_run.storage.routing import RepositoryRoute
from agent_run.storage.run_scope import RunTransaction, transaction_scope
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.tasks import read_repository_task
from support.offline import fixture_inputs

SUBJECT = 'shared-subject'


@pytest.fixture
def route(tmp_path):
    result = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    result.initialize()
    return result


def task(route, parent=1):
    return TaskKey(route.repository_root, route.repository, parent)


def proposal(route, name='A', *, parent=1, language='zh'):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
        'refs/heads/work-' + name, 'refs/heads/main')
    spec = RunSpec(name, route.repository, parent, 'r', 'g', (delivery,),
        language=language, repository_root=str(route.repository_root), inputs=fixture_inputs())
    source = Requirements(ParentIssue(parent, 'Parent ' + str(parent), 'Frozen requirement ' + str(parent)), ())
    return spec, source


def create(route, name='A', *, parent=1, language='zh'):
    spec, source = proposal(route, name, parent=parent, language=language)
    return route.create(task(route, parent), spec, source)


def dump(route):
    with closing(sqlite3.connect(route.database)) as db:
        return tuple(db.iterdump())


def test_staged_public_gate_is_closed_and_idle_task_is_not_a_run(route, monkeypatch):
    assert routing.REPOSITORY_PUBLIC_READY is False
    route.idle(task(route))
    assert route.task(task(route)).current_run is None
    with route.metadata().transaction(write=False) as owner:
        assert type(owner) is Transaction
        assert owner.one('SELECT protocol FROM meta')[0] == STAGED_REPOSITORY_PROTOCOL
        for table in ('runs', 'attempts', 'invocations', 'invocation_outputs', 'budgets', 'hosts'):
            assert owner.one('SELECT count(*) FROM ' + table)[0] == 0
        with pytest.raises(KernelError, match='run_scope_required'):
            transaction_scope(owner)
    before = dump(route)
    for opening in (lambda: Storage(route.database), lambda: Kernel(route.database),
                    lambda: routing.open_run(route.database, run_id='A'),
                    lambda: require_supported_state(route.database)):
        with pytest.raises(UnsupportedState, match='unknown_protocol'):
            opening()
    assert dump(route) == before
    monkeypatch.setattr(routing, 'REPOSITORY_PUBLIC_READY', True)
    with pytest.raises(KernelError, match='repository_activation_incomplete'):
        routing.open_run(route.database, run_id='A')
    assert dump(route) == before


def test_real_storage_creation_opening_queries_and_history_keep_sibling_ids_separate(route):
    assert create(route, 'A', parent=1).created
    assert create(route, 'B', parent=2, language='en').created
    a, b = route.kernel('A'), route.kernel('B')
    assert a.storage.run_id == 'A' and b.storage.run_id == 'B'
    assert a.status(SUBJECT)['kind'] == b.status(SUBJECT)['kind'] == 'parent_only'
    assert a.control('stop', 'same-action', subject=SUBJECT) == 'applied'
    assert not b.status(SUBJECT)['gates']
    assert b.control('stop', 'same-action', subject=SUBJECT) == 'applied'
    assert a.control('resume', 'only-A-resume', subject=SUBJECT) == 'applied'
    assert b.status(SUBJECT)['gates'] == ['operator_stopped']
    assert all(row['run_id'] == 'A' for row in a.history(SUBJECT, limit=128))
    first = a.history(SUBJECT, limit=1)
    later = a.history(SUBJECT, after=first[0]['position'], limit=128)
    assert first and all(row['run_id'] == 'A' and row['position'] > first[0]['position'] for row in later)
    for run, number in (('A', 1), ('B', 2)):
        document = route.read(run, 'status')
        assert document.data['run_id'] == run and document.data['parent']['number'] == number
        with route.at_run(run).transaction(write=False) as tx:
            header = route.read(run, 'settings')
            assert header.data['run_id'] == run
            assert isinstance(tx, RunTransaction) and tx.run_id == run
    before = dump(route)
    with pytest.raises(KernelError):
        route.at_run('not-present')
    assert dump(route) == before


def test_metadata_discovery_is_paginated_and_never_loads_large_sibling_body(route, monkeypatch):
    create(route, 'A', parent=1)
    create(route, 'B', parent=2)
    with route.at_run('B').transaction() as tx:
        tx.execute('UPDATE requirement_versions SET body=? WHERE run_id=?', ('B' * 100000, 'B'))
    before = dump(route)
    import agent_run.storage.sqlite as storage_module
    monkeypatch.setattr(storage_module, 'QUERY_LIMIT', 8192)
    page = route.discover(limit=1)
    assert tuple(item.run_id for item in page.items) == ('A',) and page.has_more and page.next_after == 'A'
    tail = route.discover(after=page.next_after, limit=1)
    assert tuple(item.run_id for item in tail.items) == ('B',) and not tail.has_more and tail.next_after is None
    assert page.items[0].task == task(route, 1) and tail.items[0].task == task(route, 2)
    assert page.items[0].current and tail.items[0].current
    assert dump(route) == before


@pytest.mark.parametrize('limit', [True, 0, 129, -1])
def test_discovery_rejects_invalid_page_without_changing_any_state(route, limit):
    before = dump(route)
    with pytest.raises(KernelError, match='query_limit'):
        route.discover(limit=limit)
    assert dump(route) == before


def test_original_frozen_proposal_and_historical_run_are_not_recreated(route):
    create(route)
    before = dump(route)
    assert create(route, 'loser', language='en').run_id == 'A'
    assert create(route, 'A', language='en').run_id == 'A'
    assert dump(route) == before
    with pytest.raises(KernelError, match='task_creation_unknown'):
        route.reconcile(task(route), 'loser')
    with route.at_run('A').transaction() as tx:
        tx.execute("UPDATE subjects SET phase='complete' WHERE run_id='A'")
    assert create(route, 'successor').created
    old = route.reconcile(task(route), 'A')
    assert old.run_id == 'A' and old.current_run == 'successor'
    assert route.task(task(route)).current_run == 'successor'
    assert route.kernel('A').storage.run_id == 'A'
    page = route.discover()
    assert [(item.run_id, item.current) for item in page.items] == [('A', False), ('successor', True)]


def test_namespace_checks_are_in_original_writer_before_commit_and_rollback_all_foreign_metadata(route):
    route.idle(task(route))
    before = dump(route)
    other = TaskKey(route.repository_root, 'foreign/project', 1)
    from agent_run.core.task_creation import ensure_idle_task
    with pytest.raises(sqlite3.IntegrityError, match='repository namespace mismatch'):
        with route.metadata().transaction(ordered=False) as owner:
            ensure_idle_task(owner, other)
    assert dump(route) == before
    foreign = RepositoryRoute(route.database, route.repository_root, 'foreign/project')
    with pytest.raises(KernelError, match='repository_namespace_changed'):
        foreign.metadata()
    assert dump(route) == before
    with pytest.raises(KernelError, match='task_location_identity'):
        route.idle(other)
    assert dump(route) == before


@pytest.mark.parametrize('operation', ['update', 'delete', 'second_row', 'replace'])
def test_repository_file_identity_cannot_be_changed_or_replaced(route, operation):
    before = dump(route)
    with pytest.raises(sqlite3.IntegrityError):
        with route.metadata().transaction(ordered=False) as owner:
            if operation == 'update':
                owner.execute("UPDATE repository_binding SET repository='foreign/project'")
            elif operation == 'delete':
                owner.execute('DELETE FROM repository_binding')
            elif operation == 'replace':
                owner.execute('INSERT OR REPLACE INTO repository_binding VALUES (1,?,?)',
                    ('foreign/project', str(route.repository_root)))
            else:
                owner.execute('INSERT INTO repository_binding VALUES (2,?,?)',
                    (route.repository, str(route.repository_root)))
    assert dump(route) == before


def test_bound_views_share_original_owner_poison_and_roll_back_both_aliases(route):
    create(route, 'A', parent=1)
    create(route, 'B', parent=2)
    before = dump(route)
    with pytest.raises(KernelError, match='transaction_aborted'):
        with route.at_run('A').transaction() as a:
            b = RunTransaction(a, 'B')
            a.execute("UPDATE subjects SET version=version+1 WHERE run_id='A'")
            b.execute("UPDATE subjects SET version=version+1 WHERE run_id='B'")
            try:
                b.execute('INSERT INTO missing_routing_table VALUES (1)')
            except sqlite3.OperationalError:
                pass
            assert a.failed and b.failed
    assert dump(route) == before
    with route.at_run('A').transaction(write=False) as a:
        b = RunTransaction(a, 'B')
        a.rows_read = 65536
        assert b.rows_read == 65536
        with pytest.raises(KernelError, match='query_limit'):
            b.one('SELECT 1')
        assert a.rows_read == b.rows_read == 65536


def test_committed_wal_metadata_is_visible_and_readers_do_not_change_database_or_wal(route):
    create(route)
    with closing(sqlite3.connect(route.database, isolation_level=None)) as keeper:
        keeper.row_factory = sqlite3.Row
        keeper.execute('PRAGMA foreign_keys=ON')
        keeper.execute('BEGIN IMMEDIATE')
        from agent_run.core.task_creation import ensure_idle_task
        ensure_idle_task(Transaction(keeper), task(route, 2))
        keeper.commit()
        wal = route.database.with_name(route.database.name + '-wal')
        assert wal.is_file() and wal.stat().st_size > 0
        before = route.database.read_bytes(), wal.read_bytes()
        with route.metadata().transaction(write=False) as owner:
            assert owner.execute('PRAGMA query_only').fetchone()[0] == 1
            assert read_repository_task(owner, task(route, 2))['current_run'] is None
        assert route.task(task(route, 2)).current_run is None
        assert (route.database.read_bytes(), wal.read_bytes()) == before


@pytest.mark.parametrize('operation', ['task', 'discover', 'open', 'create', 'reconcile'])
def test_read_failure_or_missing_database_never_implicitly_initializes_or_creates(tmp_path, operation):
    route = RepositoryRoute(tmp_path / 'absent' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    with pytest.raises(KernelError, match='missing_database'):
        if operation == 'task':
            route.task(task(route))
        elif operation == 'discover':
            route.discover()
        elif operation == 'open':
            route.at_run('A')
        elif operation == 'create':
            create(route)
        else:
            route.reconcile(task(route), 'original-proposal')
    assert not route.database.exists() and not route.database.parent.exists()


def test_existing_current_protocol_is_refused_by_staged_route_without_migration_or_deletion(tmp_path):
    database = tmp_path / 'kernel.sqlite3'
    Storage.create(database)
    route = RepositoryRoute(database, tmp_path / 'repository', 'example/project')
    before = database.read_bytes()
    with pytest.raises(UnsupportedState, match='unknown_protocol'):
        route.metadata()
    with pytest.raises((UnsupportedState, FileExistsError)):
        route.initialize()
    assert database.read_bytes() == before


@pytest.mark.parametrize('name', ['unowned_view', 'sqlitex_unowned'])
def test_unknown_schema_object_is_refused_without_querying_or_repairing_it(route, name):
    with closing(sqlite3.connect(route.database)) as db:
        db.execute('CREATE VIEW ' + name + ' AS SELECT 1 AS unowned')
        db.commit()
    before = dump(route)
    with pytest.raises(UnsupportedState, match='uncertain_state'):
        route.metadata()
    assert dump(route) == before


@pytest.mark.parametrize('committed', [False, True])
def test_real_storage_commit_reply_loss_reconciles_once_without_new_proposal_or_deadline(route, monkeypatch, committed):
    original_connect = sqlite3.connect
    original_transaction = Storage.transaction
    fired = []
    deadlines = []
    class ControlledConnection(sqlite3.Connection):
        def commit(self):
            trigger = self.total_changes > 0 and self.execute('PRAGMA query_only').fetchone()[0] == 0 and not fired
            if trigger:
                fired.append(True)
                if committed:
                    super().commit()
                raise OSError('Controlled commit reply unavailable')
            return super().commit()
    def connect(*args, **kwargs):
        kwargs['factory'] = ControlledConnection
        return original_connect(*args, **kwargs)
    from contextlib import contextmanager
    @contextmanager
    def transaction(storage, **kwargs):
        if kwargs.get('write', True):
            deadlines.append(kwargs.get('deadline'))
        with original_transaction(storage, **kwargs) as owner:
            yield owner
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(Storage, 'transaction', transaction)
    if committed:
        result = create(route)
        assert result.created and result.run_id == 'A'
        assert route.reconcile(task(route), 'A').run_id == 'A'
    else:
        with pytest.raises(KernelError, match='task_creation_unknown'):
            create(route)
    assert fired == [True]
    assert deadlines[0] == deadlines[1] and deadlines[0] is not None
    with route.metadata().transaction(write=False) as owner:
        assert owner.one('SELECT count(*) FROM runs')[0] == int(committed)
        assert owner.one('SELECT count(*) FROM task_runs')[0] == int(committed)
