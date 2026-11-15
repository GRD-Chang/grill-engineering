"""UNRUN final-route contracts on real SQLite and controlled clock decisions.

No Host, process, namespace, model, credential or external service is executed.
The clock substitute proves state/ownership decisions, never kernel clock or
cross-process FIFO support. Public routing uses the actual managed registry.
"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.cli.main import main
from agent_run.config.paths import app_data_root
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, HostObservation, ParentIssue, Requirements, RunSpec
from agent_run.storage import routing, sqlite as storage_module
from agent_run.storage.preflight import PROTOCOL, UnsupportedState, require_supported_state
from agent_run.storage.routing import RepositoryRoute, open_run
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs


class StateDecisionClock:
    """Explicit controlled boundary; this class makes no native clock claim."""
    domain = json.dumps(['native-linux-local-wal-save/2', '12345678-1234-1234-1234-123456789abc',
        'clock_gettime(CLOCK_MONOTONIC)', 1, 2, 0, 0], separators=(',', ':'))
    def require(self):
        pass
    def close(self):
        pass


@pytest.fixture
def managed(tmp_path, monkeypatch, git_repo):
    workspace = ManagedWorkspace.for_repository('example/project')
    workspace.ensure_path(str(git_repo))
    monkeypatch.setattr(storage_module, 'SaveClock', StateDecisionClock)
    return workspace


def proposal(workspace, name='A', parent=1, *, language='en'):
    delivery = Delivery('same-subject', DeliveryKind.PARENT_ONLY, parent, 'unobserved',
        Commit('a' * 40, 'b' * 40), 'refs/heads/' + name, 'refs/heads/main')
    return RunSpec(name, workspace.repository, parent, 'unread', 'unread', (delivery,), language=language,
        repository_root=str(workspace.repository_root), inputs=fixture_inputs()), Requirements(
            ParentIssue(parent, 'Original parent', 'Original frozen requirement'), ())


def create(workspace, name='A', parent=1):
    spec, source = proposal(workspace, name, parent)
    return Kernel.create_task(workspace.state_root / 'kernel.sqlite3', spec, requirements=source)


def dump(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def test_single_public_schema_idle_task_and_exact_run_owner(managed):
    route = RepositoryRoute.for_workspace(managed)
    route.initialize()
    task = TaskKey(managed.repository_root, managed.repository, 1)
    route.idle(task)
    assert require_supported_state(route.database) == PROTOCOL == 'independent-delivery-repository-v23'
    assert not hasattr(routing, 'REPOSITORY_PUBLIC_READY')
    before = dump(route.database)
    assert route.task(task).current_run is None
    with route.metadata().transaction(write=False) as owner:
        assert type(owner) is Transaction
        for table in ('runs', 'subjects', 'invocations', 'invocation_outputs', 'budgets', 'actions', 'hosts'):
            assert owner.one('SELECT count(*) FROM ' + table)[0] == 0
        with pytest.raises(KernelError, match='run_scope_required'):
            owner.put('invented', 'unused', b'not written')
    with pytest.raises(KernelError, match='run_scope_required'):
        open_run(route.database)
    with pytest.raises(KernelError, match='run_scope_missing'):
        open_run(route.database, run_id='A')
    assert dump(route.database) == before


def test_default_status_history_settings_and_profile_keep_the_exact_run(managed, capsys):
    first, sibling = create(managed, 'A', 1), create(managed, 'B', 2)
    assert first.storage.path == sibling.storage.path
    before = dump(first.storage.path)
    for command, tail in (('status', []), ('history', []), ('settings', ['show', '--run'])):
        arguments = [command, *tail, 'A', '--repo', managed.repository, '--json']
        assert main(arguments) == 0
        output = json.loads(capsys.readouterr().out)
        assert output['run_id'] == 'A'
        if command == 'settings':
            assert output['scope'] == 'run' and output['source'] == 'run_snapshot'
            assert output['repository_root'] == str(managed.repository_root)
        else:
            assert output['repository'] == managed.repository
    assert dump(first.storage.path) == before
    with sibling.storage.transaction(write=False) as tx:
        sibling_profile = tx.one('SELECT profile_revision FROM runs WHERE id=?', ('B',))[0]
    assert main(['configure', 'A', '--repo', managed.repository, '--dev-model', 'future-only-model', '--json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['run_id'] == 'A' and output['profile_revision'] == 2
    with sibling.storage.transaction(write=False) as tx:
        assert tx.one('SELECT profile_revision FROM runs WHERE id=?', ('B',))[0] == sibling_profile
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM invocation_outputs')[0] == 0
    assert list((app_data_root() / 'native-task-locations').glob('*.json')) == []


def test_public_creation_commit_reply_loss_and_competing_proposal_reuse_original_frozen_run(managed, monkeypatch):
    original_connect, committed = sqlite3.connect, []
    class LostReply(sqlite3.Connection):
        def commit(self):
            exists = self.execute("SELECT count(*) FROM sqlite_schema WHERE name='tasks'").fetchone()[0]
            bound = exists and self.execute('SELECT count(*) FROM tasks WHERE current_run IS NOT NULL').fetchone()[0]
            if self.in_transaction and bound and not committed:
                super().commit()
                committed.append(True)
                raise OSError('controlled confirmed commit followed by lost reply')
            return super().commit()
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, 'connect', lambda *args, **kwargs:
            original_connect(*args, **kwargs, factory=LostReply))
        first = create(managed)
    assert committed == [True] and first.storage.run_id == 'A'
    original = dump(first.storage.path)
    spec, source = proposal(managed, 'loser', language='zh')
    replacement = Kernel.create_task(first.storage.path, spec, requirements=source)
    assert replacement.storage.run_id == 'A' and dump(first.storage.path) == original
    with RepositoryRoute.for_workspace(managed).metadata().transaction(write=False) as owner:
        assert owner.one('SELECT count(*) FROM runs')[0] == 1
        assert owner.one('SELECT current_run FROM tasks')[0] == 'A'
        assert owner.one('SELECT language FROM runs')[0] == 'en'
        for table in ('invocations', 'invocation_outputs', 'leases', 'hosts'):
            assert owner.one('SELECT count(*) FROM ' + table)[0] == 0


def test_same_action_and_subject_never_alias_host_receipts_or_carriers(managed):
    first, sibling = create(managed, 'A', 1), create(managed, 'B', 2)
    a = first.admit_host('same-action', kind='run', command=('/unexecuted/python',),
        environment_carrier=managed.state_root / 'A.environment', preflight=lambda: None)
    b = sibling.admit_host('same-action', kind='run', command=('/unexecuted/python',),
        environment_carrier=managed.state_root / 'B.environment', preflight=lambda: None)
    assert a.run_id == 'A' and b.run_id == 'B' and a.nonce != b.nonce
    assert first.run_paths().host_partition != sibling.run_paths().host_partition
    assert first.run_paths().artifacts != sibling.run_paths().artifacts
    # A refused business callback may register then retire its own save ticket.
    # Exclude only that queue sequence; every business table/material/budget,
    # other sequence and the complete outstanding ticket set must stay exact.
    queue_sequence = "INSERT INTO \"sqlite_sequence\" VALUES('save_admissions',"
    before = dump(first.storage.path)
    with pytest.raises(KernelError, match='host_binding_changed'):
        sibling.observe_host(a, HostObservation('unknown', a.unit_name))
    after = dump(first.storage.path)
    assert tuple(row for row in after if not row.startswith(queue_sequence)) == tuple(
        row for row in before if not row.startswith(queue_sequence))
    def sequence(rows):
        values = [int(row[len(queue_sequence):-2]) for row in rows if row.startswith(queue_sequence)]
        assert len(values) <= 1
        return values[0] if values else 0
    assert sequence(after) == sequence(before) + 1
    with RepositoryRoute.for_workspace(managed).metadata().transaction(write=False) as owner:
        assert owner.one('SELECT count(*) FROM save_admissions')[0] == 0
    assert first.host_request('same-action') == a and sibling.host_request('same-action') == b


@pytest.mark.parametrize('protocol', ['independent-delivery-kernel-v21', 'independent-delivery-repository-unactivated'])
def test_retired_protocol_keeps_committed_wal_raw_material_and_budget_before_any_write(tmp_path, protocol):
    database = tmp_path / 'kernel.sqlite3'
    raw, material = tmp_path / 'raw.return', tmp_path / 'original.material'
    raw.write_bytes(b'original opaque raw')
    material.write_bytes(b'original opaque material')
    with closing(sqlite3.connect(database, isolation_level=None)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE meta(protocol TEXT NOT NULL)')
        writer.execute('INSERT INTO meta VALUES (?)', (protocol,))
        writer.execute('CREATE TABLE original_budget(value INTEGER NOT NULL)')
        writer.execute('INSERT INTO original_budget VALUES (7)')
        wal = Path(str(database) + '-wal')
        assert wal.stat().st_size > 0
        before = [item.read_bytes() for item in (database, wal, raw, material)]
        with pytest.raises(UnsupportedState, match='unknown_protocol'):
            open_run(database, run_id='original')
        with pytest.raises(UnsupportedState, match='unknown_protocol'):
            RepositoryRoute.from_database(database)
        assert [item.read_bytes() for item in (database, wal, raw, material)] == before
        assert writer.execute('SELECT value FROM original_budget').fetchone()[0] == 7



def test_null_current_on_an_existing_task_never_authorizes_a_fresh_proposal(managed):
    first = create(managed)
    route = RepositoryRoute.for_workspace(managed)
    task = TaskKey(managed.repository_root, managed.repository, 1)
    # Controlled damaged metadata, not a business transition or recovery proof.
    # NULL itself satisfies the SQL FK but is not legitimate idle history.
    with closing(sqlite3.connect(first.storage.path)) as connection:
        connection.execute('UPDATE tasks SET current_run=NULL WHERE id=?', (task.fingerprint,))
        connection.commit()  # closing() alone would roll this corruption back.
    with route.metadata().transaction(write=False) as owner:
        assert owner.one('SELECT current_run FROM tasks WHERE id=?', (task.fingerprint,))[0] is None
    before = dump(first.storage.path)
    spec, source = proposal(managed, 'must-not-create')
    with pytest.raises(KernelError, match='task_location_identity'):
        Kernel.create_task(first.storage.path, spec, requirements=source)
    with pytest.raises(KernelError, match='task_location_identity'):
        route.open_task(task)
    assert dump(first.storage.path) == before
    with route.metadata().transaction(write=False) as owner:
        assert owner.one('SELECT count(*) FROM runs')[0] == 1
        assert owner.one('SELECT count(*) FROM invocations')[0] == 0
