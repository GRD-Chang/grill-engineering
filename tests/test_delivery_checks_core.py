"""Checks demand belongs to one Core continuation, never a cached permission.

Normal Task/Run producers, private Git, and the existing ScopedKernel decision
seam. No native save clock, physical Host, Worker, or GitHub service is exercised.
"""
from contextlib import closing, ExitStack
from dataclasses import replace
import sqlite3

import pytest

from agent_run.core.operation_contract import ObserveIO, ReadUnavailable, Work
from agent_run.core.operation_runner import OperationRunner, observe_control
from agent_run.errors import KernelError
from agent_run.model import Checks, DeliveryKind
from support import offline
from support.effect_boundary import advance_to_effect
from support.offline import Scenario
from support.run_scoped_kernel import ScopedKernel


@pytest.fixture
def decision_scenario(tmp_path, monkeypatch):
    create = offline.create_kernel
    with ExitStack() as owned:
        connections = []
        def producer(*args, **kwargs):
            original = create(*args, **kwargs)
            db = owned.enter_context(closing(sqlite3.connect(original.storage.path, isolation_level=None)))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            connections.append(db)
            kernel = ScopedKernel(db, original.storage.run_id)
            kernel.storage.path, kernel.storage.run_id = original.storage.path, original.storage.run_id
            return kernel
        monkeypatch.setattr(offline, 'create_kernel', producer)
        scenario = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY, checks=('fail', 'none'))
        assert len(connections) == 1
        yield scenario, connections[0]


def test_local_preparation_omits_unconsumed_checks_without_reusing_its_permission(decision_scenario):
    scenario, db = decision_scenario
    kernel, subject = scenario.kernel, scenario.delivery.key
    original_ports, selected, open_pr, ordinary = scenario.make_ports, set(), set(), []
    retained = []

    def ports_for(binding):
        ports = original_ports(binding)
        delivery, execute = ports.delivery, ports.execute
        local = []
        def observe(request):
            if local:
                assert request.read_checks is False and request.check_head is None
            else:
                assert request.read_checks is True
                ordinary.append(request)
            facts = delivery(request)
            if local and facts.pr is not None and facts.pr.state == 'OPEN':
                open_pr.add(local[-1].operation)
            return facts
        ports.delivery = observe

        def write(effect, authorize):
            if effect.operation not in kernel.LOCAL_PREPARATION:
                return execute(effect, authorize)
            selected.add(effect.operation)
            local.append(effect)
            try:
                source = kernel.begin_effect_observation(effect)
                assert source.fence.local_effect == effect
                request = kernel.accept_requirements(source, ports.source(source.parent_number))
                facts = observe(request.query)
                world, fence = kernel.accept_delivery(request, facts)
                assert fence.local_effect == effect and world.checks is None and not facts.checks_requested
                retained.append((request, facts))
                before = tuple(db.iterdump())
                for malformed in (
                        replace(facts, checks_requested=True),
                        replace(facts, checks=Checks(17, 'unrequested', 'none'))):
                    with pytest.raises(ReadUnavailable, match='exactly observed'):
                        kernel.accept_delivery(request, malformed)
                with pytest.raises(ReadUnavailable, match='exactly observed'):
                    kernel.accept_delivery(replace(request, query=replace(request.query, read_checks=True)), facts)
                for wrong in (replace(effect, dispatch_serial=effect.dispatch_serial + 1),
                              replace(effect, operation='merge')):
                    with pytest.raises(KernelError):
                        kernel.accept_delivery(replace(request, fence=replace(fence, local_effect=wrong)), facts)
                # A local result cannot become a general scheduling observation.
                with kernel.storage.transaction(write=False) as tx:
                    token = kernel._operation_token(tx, subject, False)
                work = Work(scenario.execution, token, subject, False, True, None, None)
                general = ObserveIO(work, kernel.begin_observation(subject, execution=scenario.execution))
                with pytest.raises(KernelError, match='observation_binding'):
                    kernel.accept_work(general, (world, fence))
                assert tuple(db.iterdump()) == before
                return execute(effect, authorize)
            finally:
                local.pop()
        ports.execute = write
        return ports

    scenario.make_ports = ports_for
    assert scenario.run().kind == 'approval'
    assert selected == open_pr == set(kernel.LOCAL_PREPARATION)
    assert ordinary and any(request.check_head is not None for request in ordinary)
    assert scenario.development_count == 2  # The first real checks failure still repaired.
    before = tuple(db.iterdump())
    for request, facts in retained:
        with pytest.raises(KernelError):
            kernel.accept_delivery(request, facts)
    assert tuple(db.iterdump()) == before

    world = observe_control(kernel, subject, scenario.make_ports)
    assert world.checks is not None and world.checks.result == 'none'
    for index, result in enumerate((None, 'pending', 'unknown')):
        checks = None if result is None else replace(world.checks, result=result)
        with pytest.raises(KernelError, match='approval_checks'):
            kernel.control('approve', f'incomplete-{index}', subject=subject, world=replace(world, checks=checks))
        assert tuple(db.iterdump()) == before
    assert kernel.control('approve', 'accepted', subject=subject, world=world) == 'pending'
    head = world.pr.head.sha
    scenario.checks[:] = ['fail']
    assert scenario.world().pr.head.sha == head
    assert scenario.step().kind == 'progress'
    assert kernel.status(subject)['phase'] == 'checks'
    with kernel.storage.transaction(write=False) as tx:
        run = kernel.storage.run_id
        assert tx.one('SELECT grant_id FROM subjects WHERE run_id=? AND id=?', (run, subject))[0] is None
        assert not tx.rows("SELECT id FROM effects WHERE run_id=? AND operation='merge'", (run,))
        assert tx.one('SELECT final_action FROM runs WHERE id=?', (run,))[0] is None


def test_required_checks_outage_keeps_original_wait_deadline_and_budget(decision_scenario):
    scenario, db = decision_scenario
    kernel, subject = scenario.kernel, scenario.delivery.key
    scenario.checks[:] = ['pending']
    assert scenario.run().kind == 'waiting'
    before = kernel.status(subject)
    run = kernel.storage.run_id
    with kernel.storage.transaction(write=False) as tx:
        window = tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?', (run, subject)))
    assert window == (100., 2800., 1)
    original = scenario.make_ports
    requests = []
    def outage(binding):
        ports = original(binding)
        delivery = ports.delivery
        def read(request):
            requests.append(request)
            if request.check_head is not None:
                assert request.read_checks is True
                raise ReadUnavailable('github_read_failed', 'controlled required checks outage')
            return delivery(request)
        ports.delivery = read
        return ports
    kernel.clock = lambda: 105.
    runner = OperationRunner(kernel, scenario.execution, outage)
    for _ in range(4):
        step = runner.run_once()
        if step.kind == 'waiting':
            break
        assert step.kind == 'progress'
    assert step.kind == 'waiting' and step.call is None
    assert requests and all(request.read_checks for request in requests)
    assert kernel.status(subject)['budgets'] == before['budgets']
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?', (run, subject))) == (100., 2800., 2)
        assert not tx.rows("SELECT id FROM effects WHERE run_id=? AND operation='merge'", (run,))
    kernel.clock = lambda: 2800.
    step = runner.run_once()
    assert (step.kind, step.reason, step.call) == ('paused', 'supervision_timeout', None)
    assert kernel.status(subject)['budgets'] == before['budgets']
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?', (run, subject))) == (100., 2800., 2)
        assert tx.one('SELECT integrity_check FROM pragma_integrity_check')[0] == 'ok'


def test_stop_revokes_the_exact_already_prepared_local_admission(decision_scenario, monkeypatch):
    scenario, db = decision_scenario
    boundary = advance_to_effect(scenario, monkeypatch, 'candidate', developments=1)
    admission = boundary.authorize.prepare()
    kernel, effect = scenario.kernel, boundary.effect
    before = kernel.status(effect.subject)['budgets']
    assert kernel.control('stop', 'stop-local-preparation', subject=effect.subject) == 'applied'
    stopped = tuple(db.iterdump())
    with pytest.raises(KernelError):
        admission()
    assert tuple(db.iterdump()) == stopped
    assert kernel.status(effect.subject)['budgets'] == before
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE run_id=? AND id=?',
            (kernel.storage.run_id, effect.effect_id))) == ('cancelled', 0)


@pytest.mark.parametrize('failed_read', ['source', 'delivery'])
def test_local_preparation_still_bounds_its_required_source_and_pr_reads(decision_scenario, monkeypatch, failed_read):
    from agent_run.model import ReadWait
    scenario, db = decision_scenario
    boundary = advance_to_effect(scenario, monkeypatch, 'candidate', developments=1)
    kernel, effect = scenario.kernel, boundary.effect
    before = kernel.status(effect.subject)['budgets']
    def unavailable(*args, **kwargs):
        raise ReadUnavailable('github_read_failed', 'Required identity observation is unavailable')
    monkeypatch.setattr(scenario.port, failed_read, unavailable)
    with pytest.raises(ReadWait) as first:
        boundary.authorize.prepare()
    assert first.value.retry_at == 105.
    with kernel.storage.transaction(write=False) as tx:
        wait = tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?',
            (kernel.storage.run_id, effect.subject)))
    assert wait == (100., 700., 1)
    kernel.clock = lambda: 700.
    with pytest.raises(ReadWait) as expired:
        boundary.authorize.prepare()
    assert expired.value.step.reason == 'supervision_timeout'
    assert kernel.status(effect.subject)['budgets'] == before
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE run_id=? AND id=?',
            (kernel.storage.run_id, effect.effect_id))) == ('prepared', 0)
        assert tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?',
            (kernel.storage.run_id, effect.subject))) == wait
