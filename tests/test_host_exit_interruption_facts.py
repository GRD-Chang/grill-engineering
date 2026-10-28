"""Atomic Core interruption facts, not physical foreground namespace proof.

Normal Task/Run producers and the existing ScopedKernel seam retain actual
SQLite transactions, work selection, role returns and private Git operations.
Host and Worker observations below are explicit synthetic adapter inputs.
"""
from contextlib import closing, ExitStack
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation, Issue, ParentIssue, Policy, Requirements
from support import offline
from support.core_operations import CoreOperations, prepare_scripted_agent
from support.run_scoped_kernel import ScopedKernel


@pytest.fixture
def managed_scenario(tmp_path, monkeypatch):
    create = offline.create_kernel
    with ExitStack() as owned:
        connections = []

        def producer(*args, **kwargs):
            original = create(*args, **kwargs)
            db = owned.enter_context(closing(sqlite3.connect(original.storage.path, isolation_level=None)))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            kernel = ScopedKernel(db, original.storage.run_id)
            kernel.storage.path, kernel.storage.run_id = original.storage.path, original.storage.run_id
            connections.append(db)
            return kernel

        monkeypatch.setattr(offline, 'create_kernel', producer)

        def scenario(*, tickets=1):
            source = Requirements(ParentIssue(1, 'Parent', 'Original scope', tuple(range(2, 2 + tickets))),
                tuple(Issue(number, f'Ticket {number}', 'Original work', 'OPEN',
                    frozenset({'ready-for-agent'})) for number in range(2, 2 + tickets)))
            value = offline.GraphScenario(tmp_path / 'scenario', source, policy=Policy(concurrency=tickets))
            kernel = value.kernel
            kernel.control('stop', 'retire-fixture')
            kernel.confirm_executor_exit(value.execution)
            kernel.control('resume', 'prepare-managed')
            request = kernel.admit_host('original-host', kind='run', command=('controlled-executor',),
                environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
            assert kernel.dispatch_host(request)
            observer = {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'}
            common = {'boot_id': 'test-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
            domain = {'nonce': request.nonce, 'observer': observer,
                'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
                'executor': {**common, 'pid': 202, 'parent': 200, 'start_token': '203', 'namespace_pids': [202, 2]}}
            running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
                backend='foreground', domain=domain, usage_ready=True)
            assert kernel.observe_host(request, running) == 'running'
            value.execution = Execution(request.run_id, request.owner, request.epoch)
            exited = replace(running, disposition='exited', usage_ready=False, usage_released=True,
                exit_status=-9, ended_at=101., namespace_exit_status=-9, exit_evidence='retained_namespace_pidfd')
            assert len(connections) == 1
            return value, connections[0], request, exited

        yield scenario


def preparation(scenario):
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    return operations.until(lambda step: step.preparation is not None).preparation


def start(scenario, prepared):
    kernel = scenario.kernel
    call = prepare_scripted_agent(kernel, prepared).call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, call.reported_thread or 'offline-' + call.thread_id)
    process = {'schema': 'linux-namespace-process-v1', 'pid': 3, 'pgid': 3,
        'start_token': 'worker-' + call.invocation_id, 'host_pid': 2, 'host_start_token': '203',
        'boot_id': 'test-boot', 'host_namespace': 'pid:[20]', 'pid_namespace': 'pid:[20]'}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    return call, process


def returned(scenario, call, process):
    scenario.kernel.receive(call, scenario.agent(call))
    scenario.kernel.agent_exited(call, process, 0)


def interruptions(db, request):
    return db.execute("SELECT identity,material_id FROM history WHERE run_id=? AND kind='host_interrupted'",
        (request.run_id,)).fetchall()


def assert_retired(db, request):
    assert tuple(db.execute('SELECT executor,epoch FROM runs WHERE id=?', (request.run_id,)).fetchone()) == (
        None, request.epoch + 1)
    assert tuple(db.execute('SELECT status,usage_released FROM hosts WHERE run_id=? AND action_id=?',
        (request.run_id, 'original-host')).fetchone()) == ('exited', 1)
    assert db.execute('SELECT status FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?',
        (request.run_id, request.owner, request.epoch)).fetchone()[0] == 'exited'


@pytest.mark.parametrize('activity', ['leased', 'preparing', 'returned'])
def test_interruption_records_original_work_before_retirement(managed_scenario, activity):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    prepared = preparation(scenario)
    if activity != 'preparing':
        call, process = start(scenario, prepared)
        if activity == 'returned':
            returned(scenario, call, process)
    prior = kernel.operation_state()
    assert bool(prior.active) is (activity == 'leased')
    assert bool(prior.preparing) is (activity == 'preparing')
    assert prior.disposition in {'ready', 'waiting'}
    budget = kernel.status(prepared.subject)['budgets']
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert kernel.operation_state().disposition == 'quiescent'
    assert kernel.status(prepared.subject)['budgets'] == budget
    rows = interruptions(db, request)
    assert len(rows) == 1 and rows[0]['identity'] == request.nonce
    observation = db.execute('SELECT observation_id FROM hosts WHERE run_id=? AND action_id=?',
        (request.run_id, 'original-host')).fetchone()[0]
    assert rows[0]['material_id'] == observation
    body = db.execute('SELECT body FROM material WHERE run_id=? AND id=?', (request.run_id, observation)).fetchone()[0]
    assert json.loads(body) == asdict(exited)
    assert kernel.observe_host(request, exited) == 'exited'
    assert len(interruptions(db, request)) == 1


def test_host_exit_reads_preparation_material_only_for_settlement(managed_scenario, monkeypatch):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    prepared = preparation(scenario)
    scope_id = db.execute("SELECT scope_id FROM waits WHERE run_id=? AND kind='agent_preparation'",
        (request.run_id,)).fetchone()[0]
    decode = kernel._preparation
    reads = []

    def tracked(tx, row):
        reads.append(row['scope_id'])
        return decode(tx, row)

    monkeypatch.setattr(kernel, '_preparation', tracked)
    budget = kernel.status(prepared.subject)['budgets']
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert len(interruptions(db, request)) == 1
    assert kernel.status(prepared.subject)['budgets'] == budget
    # Contexts share the writer's cumulative read limit. Applicability must
    # not decode them before retirement decodes them for settlement.
    assert reads == [scope_id]


@pytest.mark.parametrize('exit_status,expected', [(0, False), (None, True)])
def test_exact_exit_preserves_normal_or_unknown_executor_status(managed_scenario, exit_status, expected):
    scenario, db, request, exited = managed_scenario()
    preparation(scenario)
    assert scenario.kernel.observe_host(request, replace(exited, exit_status=exit_status)) == 'exited'
    assert_retired(db, request)
    assert bool(interruptions(db, request)) is expected
    observation = scenario.kernel.host_usage('original-host')
    assert observation.exit_status == exit_status and observation.namespace_exit_status == -9


@pytest.mark.parametrize('control', ['stop', 'abandon'])
def test_requested_shutdown_does_not_report_active_work_as_interruption(managed_scenario, control):
    scenario, db, request, exited = managed_scenario()
    prepared = preparation(scenario)
    options = {} if control == 'stop' else {
        'resource_inspections': scenario.port.inspect_resources(scenario.kernel.generation_resources())}
    assert scenario.kernel.control(control, 'requested-shutdown', **options) in {'applied', 'pending'}
    assert scenario.kernel.operation_state().preparing == (prepared.reservation_id,)
    assert scenario.kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert interruptions(db, request) == []


def test_normal_human_wait_has_no_interruption(managed_scenario):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    call, process = start(scenario, preparation(scenario))
    kernel.receive(call, b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Original human decision"]}')
    kernel.agent_exited(call, process, 0)
    step = OperationRunner(kernel, scenario.execution, scenario.make_ports).run_once()
    assert (step.kind, step.reason) == ('paused', 'human_blocker')
    prior = kernel.operation_state()
    assert not prior.active and prior.disposition == 'quiescent'
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert kernel.status(call.subject)['gates'] == ['human_blocker']
    assert interruptions(db, request) == []


def test_closed_ticket_return_is_not_interrupted_automatic_work(managed_scenario):
    scenario, db, request, exited = managed_scenario()
    call, process = start(scenario, preparation(scenario))
    returned(scenario, call, process)
    scenario.requirements = replace(scenario.requirements,
        issues=tuple(replace(issue, state='CLOSED') for issue in scenario.requirements.issues))
    scenario.kernel.refresh(lambda: scenario.requirements)
    assert scenario.kernel.operation_state().disposition == 'quiescent'
    assert scenario.kernel.observe_host(request, exited) == 'exited'
    assert 'execution_failed' in scenario.kernel.status(call.subject)['gates']
    assert interruptions(db, request) == []


def test_retirement_releasing_integration_seat_cannot_reclassify_original_quiescence(managed_scenario):
    scenario, db, request, exited = managed_scenario(tickets=2)
    kernel = scenario.kernel
    runner = OperationRunner(kernel, scenario.execution, scenario.make_ports)
    held = None
    queued = []
    for _ in range(100):
        step = runner.run_once(agent_capacity=2)
        if step.preparation is not None:
            call, process = start(scenario, step.preparation)
            if call.role.value == 'publication' and held is None:
                held = (call, process)
            else:
                returned(scenario, call, process)
        if held is not None:
            queued = db.execute('SELECT subject FROM integration_queue WHERE run_id=? AND subject!=?',
                (request.run_id, held[0].subject)).fetchall()
            if queued:
                break
    assert held is not None and len(queued) == 1
    call, process = held
    assert db.execute('SELECT integration_holder FROM runs WHERE id=?', (request.run_id,)).fetchone()[0] == call.subject
    returned(scenario, call, process)
    closed_issue = kernel.delivery(call.subject).issue
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, state='CLOSED') if issue.number == closed_issue else issue for issue in scenario.requirements.issues))
    kernel.refresh(lambda: scenario.requirements)
    assert kernel.operation_state().disposition == 'quiescent'
    assert not kernel.operation_state().active
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert db.execute('SELECT integration_holder FROM runs WHERE id=?', (request.run_id,)).fetchone()[0] is None
    assert kernel.operation_state().disposition == 'ready'
    assert interruptions(db, request) == []


def test_unavailable_applicability_does_not_authorize_notice_or_block_exact_exit(managed_scenario, monkeypatch):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    prior = kernel.operation_state()
    assert prior.disposition == 'ready' and not prior.active and not prior.preparing

    def unavailable(tx, observation=None):
        assert not tx.failed
        raise KernelError('continuation_basis_missing', 'Controlled semantic read failure')

    monkeypatch.setattr(kernel, '_operation_state_in', unavailable)
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert interruptions(db, request) == []


def test_poisoned_applicability_read_keeps_the_original_transaction_failure(managed_scenario, monkeypatch):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    before = tuple(db.iterdump())

    def poisoned(tx, observation=None):
        try:
            tx.execute('SELECT absent_column FROM hosts')
        except sqlite3.Error as error:
            assert tx.failed
            raise KernelError('transaction_aborted') from error

    with monkeypatch.context() as patch:
        patch.setattr(kernel, '_operation_state_in', poisoned)
        with pytest.raises(KernelError, match='transaction_aborted'):
            kernel.observe_host(request, exited)
    assert tuple(db.iterdump()) == before
    assert kernel.owners[-1].failed
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert len(interruptions(db, request)) == 1


def test_commit_denial_rolls_back_exit_and_interruption_together(managed_scenario):
    scenario, db, request, exited = managed_scenario()
    kernel = scenario.kernel
    preparation(scenario)
    before = tuple(db.iterdump())
    denied = []

    def authorize(action, first, second, database, source):
        if action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT':
            denied.append(first)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            kernel.observe_host(request, exited)
    finally:
        db.set_authorizer(None)
    assert denied == ['COMMIT'] and tuple(db.iterdump()) == before
    assert kernel.active_host_action() == 'original-host'
    assert kernel.observe_host(request, exited) == 'exited'
    assert_retired(db, request)
    assert len(interruptions(db, request)) == 1
