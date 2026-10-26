"""Host persistence boundaries with real SQLite and synthetic observations only.

No Host, Worker, subprocess, systemd, namespace or external service is started.
Recorded physical prerequisites do not claim the corresponding OS proof ran.
"""
from contextlib import closing
from dataclasses import asdict, replace
import sqlite3

import pytest

from agent_run.core import kernel as core
from agent_run.errors import KernelError
from agent_run.model import Commit, Execution, HostLaunch, HostObservation, Policy, World, wire_json
from agent_run.core.operation_contract import Work
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs
from support.repository_scope import connection, insert, seed
from support.run_scoped_kernel import ScopedKernel

SUBJECT = 'parent:1'


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / 'repository.sqlite3'
    with closing(connection(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        owner = Transaction(db)
        requests = {}
        for run in ('A', 'B'):
            seed(db, run)
            tx = RunTransaction(owner, run)
            policy = wire_json(core.Kernel._policy_values(Policy()))
            db.execute("UPDATE runs SET policy=?,executor='same-owner',epoch=1 WHERE id=?", (policy, run))
            inputs = fixture_inputs()
            profile = tx.put(SUBJECT, 'profile', wire_json(inputs.profiles))
            db.execute('UPDATE profile_revisions SET material_id=? WHERE run_id=?', (profile, run))
            frozen = asdict(inputs)
            del frozen['profiles']
            material = tx.put(SUBJECT, 'creation_inputs', wire_json(frozen))
            insert(db, 'run_inputs', {'run_id': run, 'subject': SUBJECT, 'material_id': material})
            for purpose in ('requirements', 'integration', 'ci'):
                insert(db, 'budgets', {'run_id': run, 'subject': SUBJECT, 'purpose': purpose,
                    'window': 1, 'development_limit': 3, 'review_limit': 3, 'review_required': 1})
            insert(db, 'resync_allowances', {'run_id': run, 'subject': SUBJECT, 'generation': 1, 'allowance': 3})
            request = HostLaunch(run, 1, 'same-owner', 'nonce-' + run, 'unit-' + run + '.service',
                ('/controlled/python',), tmp_path / run / 'environment.json')
            requests[run] = request
            material = tx.put(SUBJECT, 'host_request', wire_json({**asdict(request),
                'environment_carrier': str(request.environment_carrier), 'intent': {'kind': 'run'}}))
            insert(db, 'actions', {'run_id': run, 'id': 'same-action', 'kind': 'run', 'status': 'applied'})
            insert(db, 'hosts', {'run_id': run, 'epoch': 1, 'owner': request.owner, 'nonce': request.nonce,
                'action_id': 'same-action', 'subject': SUBJECT, 'request_id': material, 'unit_name': request.unit_name,
                'backend': 'systemd', 'allocated_at': 100., 'status': 'allocated'})
        db.commit()
        kernels = {run: ScopedKernel(db, run) for run in ('A', 'B')}
        for kernel in kernels.values():
            kernel.storage.path = path
        yield db, kernels, requests


def rows(db, run):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in tables if name != 'meta'}


def observation(request, *, exited=False, released=False):
    return HostObservation('exited' if exited else 'running', request.unit_name,
        invocation_id='native-' + request.run_id, cgroup='/controlled/' + request.run_id,
        boot_id='controlled-boot', main_pid=321 if request.run_id == 'A' else 322,
        start_token='start-' + request.run_id, usage_ready=not exited,
        exit_status=1 if exited else None, ended_at=101. if exited else None, usage_released=released)


def running(kernel, request):
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, observation(request)) == 'running'


def worker(db, run, request, *, process_matches=True):
    tx = RunTransaction(Transaction(db), run)
    context = tx.put(SUBJECT, 'context_fixture', b'{}')
    process = tx.put(SUBJECT, 'synthetic_process_identity', wire_json({
        'host_pid': 321 if run == 'A' else 322, 'host_start_token': 'start-' + run,
        'boot_id': 'controlled-boot', 'host_cgroup': '/controlled/' + run,
        'cgroup': '/controlled/' + run + '/worker' if process_matches else '/foreign/worker'}))
    insert(db, 'threads', {'run_id': run, 'id': 'same-thread', 'subject': SUBJECT,
        'role': 'development', 'profile_revision': 1})
    insert(db, 'attempts', {'run_id': run, 'id': 'same-attempt', 'subject': SUBJECT, 'generation': 1,
        'ordinal': 1, 'role': 'development', 'purpose': 'requirements', 'window': 1,
        'parent_revision': 'r', 'graph_revision': 'g', 'revision': 'r', 'base_sha': 'a' * 40,
        'completion_revision': 'no-tickets', 'context_id': context, 'thread_id': 'same-thread', 'status': 'running'})
    insert(db, 'invocations', {'run_id': run, 'id': 'same-invocation', 'attempt': 'same-attempt',
        'thread': 'same-thread', 'executor': request.owner, 'epoch': request.epoch, 'status': 'running',
        'allocated_at': 100., 'deadline_seconds': 7., 'deadline_at': 107.})
    insert(db, 'invocation_outputs', {'run_id': run, 'invocation': 'same-invocation', 'ordinal': 0,
        'context_id': context, 'lease': 'lease-' + run, 'process_identity': process,
        'dispatch_state': 'running', 'allocated_at': 100., 'mode': 'start'})
    insert(db, 'leases', {'run_id': run, 'id': 'lease-' + run, 'subject': SUBJECT,
        'invocation': 'same-invocation', 'executor': request.owner, 'epoch': request.epoch})
    db.execute('UPDATE subjects SET current_attempt=? WHERE run_id=?', ('same-attempt', run))
    db.execute('UPDATE attempts SET current_invocation=? WHERE run_id=?', ('same-invocation', run))


def test_lookup_dispatch_and_usage_are_bound_to_selected_run_with_same_action_id(scenario):
    db, kernels, requests = scenario
    assert kernels['A'].host_request('same-action') == requests['A']
    assert kernels['B'].host_request('same-action') == requests['B']
    assert kernels['A'].host_usage('same-action') is None
    before_b = rows(db, 'B')
    running(kernels['A'], requests['A'])
    assert kernels['A'].host_dispatched('same-action')
    assert not kernels['B'].host_dispatched('same-action')
    assert kernels['A'].host_usage('same-action') == observation(requests['A'])
    assert rows(db, 'B') == before_b
    assert kernels['A'].active_host_action() == 'same-action'
    assert not kernels['A'].dispatch_host(requests['A'])


def test_ambiguous_active_host_actions_still_refuse_instead_of_choosing_one(scenario):
    db, kernels, requests = scenario
    request = replace(requests['A'], epoch=2, owner='second-owner', nonce='second-nonce', unit_name='second.service')
    tx = RunTransaction(Transaction(db), 'A')
    material = tx.put(SUBJECT, 'host_request', wire_json({**asdict(request),
        'environment_carrier': str(request.environment_carrier), 'intent': {'kind': 'run'}}))
    insert(db, 'actions', {'run_id': 'A', 'id': 'second-action', 'kind': 'run', 'status': 'applied'})
    insert(db, 'hosts', {'run_id': 'A', 'epoch': 2, 'owner': request.owner, 'nonce': request.nonce,
        'action_id': 'second-action', 'subject': SUBJECT, 'request_id': material, 'unit_name': request.unit_name,
        'backend': 'systemd', 'allocated_at': 100., 'status': 'allocated'})
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='query_limit'):
        kernels['A'].active_host_action()
    assert {run: rows(db, run) for run in ('A', 'B')} == before


@pytest.mark.parametrize('callback', ['action', 'dispatch', 'authorize', 'observe', 'control', 'backend',
    'confirm', 'activate', 'retire', 'process'])
def test_foreign_host_callbacks_cannot_write_audit_or_settle_the_sibling(scenario, callback):
    db, kernels, requests = scenario
    request = requests['B']
    execution = Execution(request.run_id, request.owner, request.epoch)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError):
        if callback == 'action':
            kernels['A'].host_action(request)
        elif callback == 'dispatch':
            kernels['A'].dispatch_host(request)
        elif callback == 'authorize':
            kernels['A'].authorize_host(request)
        elif callback == 'observe':
            kernels['A'].observe_host(request, observation(request, exited=True, released=True))
        elif callback == 'control':
            kernels['A'].control_host(request, 'stop', 'foreign-control')
        elif callback == 'backend':
            kernels['A'].host_backend(execution)
        elif callback == 'confirm':
            kernels['A'].confirm_executor_exit(execution)
        elif callback == 'activate':
            kernels['A'].activate_observation(execution)
        elif callback == 'retire':
            kernels['A']._retire_failed_executor(execution, RuntimeError('Controlled failure'))
        else:
            with kernels['A'].storage.transaction(write=False) as tx:
                host = tx.one("SELECT * FROM hosts WHERE run_id='B'")
                kernels['A']._host_contains_process(tx, host, {})
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_unknown_observation_never_releases_owner_epoch_or_resume_gate(scenario):
    db, kernels, requests = scenario
    assert kernels['A'].dispatch_host(requests['A'])
    before_b = rows(db, 'B')
    unknown = HostObservation('unknown', requests['A'].unit_name)
    assert kernels['A'].observe_host(requests['A'], unknown) == 'unknown'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT executor,epoch FROM runs WHERE id='A'").fetchone()[:] == ('same-owner', 1)
    assert db.execute("SELECT status,usage_released FROM hosts WHERE run_id='A'").fetchone()[:] == ('unknown', 0)
    before_a = rows(db, 'A')
    with pytest.raises(KernelError, match='executor_exit_unproven'):
        kernels['A'].control('resume', 'must-not-resume-unknown')
    assert rows(db, 'A') == before_a


def test_exact_host_exit_only_settles_owned_process_and_does_not_invent_raw_exit_code(scenario):
    db, kernels, requests = scenario
    for run in ('A', 'B'):
        running(kernels[run], requests[run])
    db.execute('BEGIN IMMEDIATE')
    for run in ('A', 'B'):
        worker(db, run, requests[run])
    db.commit()
    before_b = rows(db, 'B')
    assert kernels['A'].observe_host(requests['A'], observation(requests['A'], exited=True)) == 'exited'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT executor,epoch FROM runs WHERE id='A'").fetchone()[:] == (None, 2)
    assert db.execute("SELECT status,usage_released FROM hosts WHERE run_id='A'").fetchone()[:] == ('exited', 0)
    assert db.execute("SELECT count(*) FROM leases WHERE run_id='A'").fetchone()[0] == 0
    assert db.execute("SELECT dispatch_state,exit_code,raw_id FROM invocation_outputs WHERE run_id='A'").fetchone()[:] == ('exited', None, None)
    assert db.execute("SELECT status,deadline_at FROM invocations WHERE run_id='A'").fetchone()[:] == ('interrupted', 107.)
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'execution_failed'
    released = observation(requests['A'], exited=True, released=True)
    assert kernels['A'].observe_host(requests['A'], released) == 'exited'
    before_a = rows(db, 'A')
    assert kernels['A'].host_usage('same-action') == released
    assert kernels['A'].observe_host(requests['A'], released) == 'exited'
    assert rows(db, 'A') == before_a and rows(db, 'B') == before_b
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_host_exit_with_foreign_process_identity_retains_worker_lease(scenario):
    db, kernels, requests = scenario
    running(kernels['A'], requests['A'])
    db.execute('BEGIN IMMEDIATE')
    worker(db, 'A', requests['A'], process_matches=False)
    db.commit()
    before_b = rows(db, 'B')
    assert kernels['A'].observe_host(requests['A'], observation(requests['A'], exited=True)) == 'exited'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT count(*) FROM leases WHERE run_id='A'").fetchone()[0] == 1
    assert db.execute("SELECT dispatch_state,exit_code FROM invocation_outputs WHERE run_id='A'").fetchone()[:] == ('running', None)
    with kernels['A'].storage.transaction(write=False) as tx:
        assert kernels['A']._agent_seats(tx) == 1


def test_unmanaged_claim_does_not_borrow_a_sibling_managed_host(scenario):
    db, kernels, _ = scenario
    db.execute("DELETE FROM hosts WHERE run_id='A'")
    db.execute("UPDATE runs SET executor=NULL WHERE id='A'")
    before_b = rows(db, 'B')
    assert kernels['A'].claim_executor('new-owner') == Execution('A', 'new-owner', 2)
    assert rows(db, 'B') == before_b
    with pytest.raises(KernelError, match='managed_host_required'):
        kernels['B'].claim_executor('new-owner')


@pytest.mark.parametrize('valid', [False, True])
def test_explicit_unmanaged_exit_checks_exact_invocation_set_and_never_releases_sibling(scenario, valid):
    db, kernels, requests = scenario
    db.execute('BEGIN IMMEDIATE')
    db.execute('DELETE FROM hosts')
    for run in ('A', 'B'):
        worker(db, run, requests[run])
        insert(db, 'retired_executors', {'run_id': run, 'owner': 'same-owner', 'epoch': 1, 'status': 'pending'})
    db.execute('UPDATE runs SET epoch=2')
    db.commit()
    before_b, before_a = rows(db, 'B'), rows(db, 'A')
    execution = Execution('A', 'same-owner', 1)
    if not valid:
        with pytest.raises(KernelError, match='worker_exit_unproven'):
            kernels['A'].confirm_executor_exit(execution, ('not-the-owned-invocation',))
        assert rows(db, 'A') == before_a
    else:
        kernels['A'].confirm_executor_exit(execution, ('same-invocation',))
        assert db.execute("SELECT status FROM retired_executors WHERE run_id='A'").fetchone()[0] == 'exited'
        assert db.execute("SELECT count(*) FROM leases WHERE run_id='A'").fetchone()[0] == 0
        assert db.execute("SELECT executor FROM runs WHERE id='A'").fetchone()[0] is None
        assert db.execute("SELECT exit_code FROM invocation_outputs WHERE run_id='A'").fetchone()[0] is None
    assert rows(db, 'B') == before_b


def test_failed_executor_revocation_does_not_release_leases_or_duplicate_audit(scenario):
    db, kernels, requests = scenario
    db.execute('BEGIN IMMEDIATE')
    for run in ('A', 'B'):
        worker(db, run, requests[run])
    db.commit()
    before_b = rows(db, 'B')
    failure = RuntimeError('Controlled failure')
    execution = Execution('A', 'same-owner', 1)
    kernels['A']._retire_failed_executor(execution, failure)
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT epoch FROM runs WHERE id='A'").fetchone()[0] == 2
    assert db.execute("SELECT status FROM retired_executors WHERE run_id='A'").fetchone()[0] == 'pending'
    assert db.execute("SELECT count(*) FROM leases WHERE run_id='A'").fetchone()[0] == 1
    before_a = rows(db, 'A')
    kernels['A']._retire_failed_executor(execution, failure)
    assert rows(db, 'A') == before_a and rows(db, 'B') == before_b


def test_observer_activation_captures_only_selected_run_sent_writes(scenario):
    db, kernels, requests = scenario
    for run in ('A', 'B'):
        running(kernels[run], requests[run])
        tx = RunTransaction(Transaction(db), run)
        payload = tx.put(SUBJECT, 'effect_fixture', b'{}')
        insert(db, 'effects', {'run_id': run, 'id': 'same-effect', 'subject': SUBJECT, 'operation': 'inspect_resources',
            'status': 'sent', 'executor': 'same-owner', 'epoch': 1, 'parent_revision': 'r', 'graph_revision': 'g',
            'revision': 'r', 'generation': 1, 'source_ref': 'refs/heads/work', 'target_ref': 'refs/heads/main',
            'before_sha': 'a' * 40, 'base_sha': 'a' * 40, 'base_tree': 'b' * 40, 'payload_id': payload})
    before = {run: rows(db, run) for run in ('A', 'B')}
    observed = kernels['A'].activate_observation(Execution('A', 'same-owner', 1))
    assert observed.execution.run_id == 'A' and observed.action_id == 'same-action'
    assert len(observed.pending) == 1 and observed.pending[0].subject == SUBJECT
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_host_observation_write_failure_keeps_original_owner_poison_and_rolls_back(scenario):
    db, kernels, requests = scenario
    assert kernels['A'].dispatch_host(requests['A'])
    db.execute("CREATE TRIGGER controlled_host_failure BEFORE UPDATE ON hosts "
        "BEGIN SELECT RAISE(ABORT,'Controlled Host write failure'); END")
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(sqlite3.IntegrityError, match='Controlled Host write failure'):
        kernels['A'].observe_host(requests['A'], observation(requests['A']))
    assert kernels['A'].owners[-1].failed
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_host_exit_settles_only_its_pre_go_preparation_without_invocation_or_fee(scenario):
    db, kernels, requests = scenario
    preparations = {}
    base = Commit('a' * 40, 'b' * 40)
    world = World('r', 'g', 'r', base, base)
    for run in ('A', 'B'):
        running(kernels[run], requests[run])
        execution = Execution(run, 'same-owner', 1)
        with kernels[run].storage.transaction() as tx:
            header, job = kernels[run]._owned(tx, SUBJECT, execution)
            preparations[run] = kernels[run]._allocate(tx, header, job,
                Work(execution, 'controlled', SUBJECT, False, True, None, None), world).preparation
        assert preparations[run] is not None
    before_b = rows(db, 'B')
    deadline = preparations['A'].preparation_deadline
    assert kernels['A'].observe_host(requests['A'], observation(requests['A'], exited=True)) == 'exited'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT count(*) FROM waits WHERE run_id='A' AND kind='agent_preparation'").fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM invocations').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM invocation_outputs').fetchone()[0] == 0
    assert db.execute('SELECT sum(developments_used+reviews_used) FROM budgets').fetchone()[0] == 0
    assert preparations['A'].preparation_deadline == deadline


def test_host_callback_cannot_reset_original_owner_read_budget(scenario, monkeypatch):
    db, kernels, requests = scenario
    original = kernels['A']._host
    def exhausted(tx, request):
        tx.rows_read = 65536
        return original(tx, request)
    monkeypatch.setattr(kernels['A'], '_host', exhausted)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='query_limit'):
        kernels['A'].observe_host(requests['A'], observation(requests['A']))
    assert kernels['A'].owners[-1].rows_read == 65536
    assert {run: rows(db, run) for run in ('A', 'B')} == before
