"""Checks demand belongs to one Core continuation, never a cached permission.

Normal Task/Run producers, private Git, and the existing ScopedKernel decision
seam. No native save clock, physical Host, Worker, or GitHub service is exercised.
"""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import asdict, replace
import sqlite3

import pytest

from agent_run.core.operation_contract import ObserveIO, ReadUnavailable, Work
from agent_run.core.operation_runner import OperationRunner, observe_control
from agent_run.errors import KernelError
from agent_run.model import Checks, DeliveryKind, ReadWait, wire_json
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


def test_healthy_guard_and_identical_source_avoid_empty_saves(decision_scenario, monkeypatch):
    scenario, db = decision_scenario
    kernel, active, records = scenario.kernel, [], []
    transaction = kernel.storage.transaction

    @contextmanager
    def counted(**options):
        before = db.total_changes
        with transaction(**options) as tx:
            yield tx
        records.append((active[-1] if active else 'other', options.get('write', True), db.total_changes - before))

    def watched(name, method):
        def call(*args, **kwargs):
            active.append(name)
            try:
                return method(*args, **kwargs)
            finally:
                active.pop()
        return call

    monkeypatch.setattr(kernel.storage, 'transaction', counted)
    for name in ('_guard_automatic_step', 'accept_requirements'):
        monkeypatch.setattr(kernel, name, watched(name, getattr(kernel, name)))
    assert scenario.run().kind == 'approval'
    selected = [row for row in records if row[0] != 'other']
    counts = {name: {'transactions': sum(row[0] == name for row in records),
                     'writers': sum(row[0] == name and row[1] for row in records),
                     'dirty': sum(row[0] == name and bool(row[2]) for row in records)}
              for name in ('_guard_automatic_step', 'accept_requirements', 'other')}
    print('healthy-read transaction counts:', counts)
    assert all(counts[name]['transactions'] for name in counts)
    assert all(row[2] == 0 for row in selected)
    assert not [row for row in selected if row[1]], 'Healthy observation paths must not request an ordered save'


def _consume_read(scenario, request, consumer, source=None):
    if consumer == 'guard':
        return scenario.kernel._guard_automatic_step(request.fence, scenario.execution, after_read=True)
    return scenario.kernel.accept_requirements(request, source or scenario.requirements)


@pytest.mark.parametrize('consumer', ['guard', 'source'])
@pytest.mark.parametrize('has_wait', [False, True])
def test_healthy_read_uses_one_query_only_snapshot(decision_scenario, monkeypatch, consumer, has_wait):
    s, db = decision_scenario
    kernel, subject = s.kernel, s.delivery.key
    request = kernel.begin_observation(subject, execution=s.execution)
    if has_wait:
        kernel._wait_for_read(request.fence, s.execution, 'controlled_read', 'Original read unavailable')
    before = tuple(db.iterdump())
    transaction, modes = kernel.storage.transaction, []

    @contextmanager
    def readonly(**options):
        modes.append((options.get('write', True), options.get('ordered', True)))
        assert options.get('write') is False
        db.execute('PRAGMA query_only=ON')
        try:
            with transaction(**options) as tx:
                yield tx
                assert tx.rows_read > 0 and tx.bytes_read > 0
        finally:
            db.execute('PRAGMA query_only=OFF')

    monkeypatch.setattr(kernel.storage, 'transaction', readonly)
    _consume_read(s, request, consumer)
    if consumer == 'guard' and has_wait:
        step = kernel._guard_automatic_step(request.fence, s.execution)
        assert step.kind == 'waiting' and step.retry_at == 105.
    assert modes == [(False, True)] * (2 if consumer == 'guard' and has_wait else 1)
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('consumer', ['guard', 'source'])
def test_exact_read_deadline_commits_only_under_fresh_writer(decision_scenario, monkeypatch, consumer):
    s, db = decision_scenario
    kernel, subject = s.kernel, s.delivery.key
    now = [100.25]  # Business supervision clock in the existing unit seam, not SaveClock.
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(subject, execution=s.execution)
    kernel._wait_for_read(request.fence, s.execution, 'controlled_read', 'Original read unavailable')
    window = tuple(db.execute('SELECT * FROM waits').fetchone())
    now[0] = 700.25
    transaction, modes = kernel.storage.transaction, []

    @contextmanager
    def recorded(**options):
        modes.append((options.get('write', True), options.get('ordered', True)))
        with transaction(**options) as tx:
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    if consumer == 'guard':
        step = _consume_read(s, request, consumer)
    else:
        with pytest.raises(ReadWait) as caught:
            _consume_read(s, request, consumer)
        step = caught.value.step
    assert step.kind == 'paused' and step.reason == 'supervision_timeout'
    assert modes == [(False, True), (True, True)]
    assert tuple(db.execute('SELECT * FROM waits').fetchone()) == window
    assert db.execute("SELECT code FROM gates WHERE code='supervision_timeout'").fetchone() is not None
    assert db.execute("SELECT count(*) FROM history WHERE kind='paused' AND identity='supervision_timeout'").fetchone()[0] == 1


@pytest.mark.parametrize('consumer', ['guard', 'source', 'changed_source'])
@pytest.mark.parametrize('change', ['stop', 'epoch', 'source', 'retired_wait'])
def test_read_to_writer_gap_rechecks_all_current_facts(decision_scenario, monkeypatch, consumer, change):
    s, db = decision_scenario
    kernel, subject = s.kernel, s.delivery.key
    now = [100.25]
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(subject, execution=s.execution)
    kernel._wait_for_read(request.fence, s.execution, 'controlled_read', 'Original read unavailable')
    if consumer != 'changed_source':
        now[0] = 700.25
    returned = replace(s.requirements, parent=replace(s.requirements.parent, body='Returned changed requirement'))
    transaction, modes, changed = kernel.storage.transaction, [], []

    @contextmanager
    def interleaved(**options):
        modes.append((options.get('write', True), options.get('ordered', True)))
        with transaction(**options) as tx:
            yield tx
        if not options.get('write', True) and not changed:
            assert not db.in_transaction
            changed.append(change)
            with monkeypatch.context() as mutation:
                mutation.setattr(kernel.storage, 'transaction', transaction)
                if change in {'stop', 'epoch'}:
                    kernel.control('stop', 'stop-in-read-writer-gap')
                    if change == 'epoch':
                        kernel.confirm_executor_exit(s.execution)
                        kernel.control('resume', 'resume-in-read-writer-gap')
                        successor = kernel.claim_executor('successor-owner')
                        assert successor.epoch > s.execution.epoch
                elif change == 'source':
                    parent = (replace(s.requirements.parent, body='Concurrent requirement') if consumer == 'guard'
                              else replace(s.requirements.parent, sub_issue_order_reliable=False))
                    concurrent = replace(s.requirements, parent=parent)
                    if consumer != 'guard':
                        assert kernel._source_revisions(concurrent) == kernel._source_revisions(s.requirements)
                    kernel.refresh(lambda: concurrent)
                else:
                    # Negative concurrency input: retire only the real wait,
                    # never manufacture an execution or completed business row.
                    db.execute('DELETE FROM waits WHERE run_id=?', (s.execution.run_id,))

    monkeypatch.setattr(kernel.storage, 'transaction', interleaved)
    if change == 'retired_wait':
        result = _consume_read(s, request, 'source' if consumer == 'changed_source' else consumer,
                               returned if consumer == 'changed_source' else None)
        if consumer == 'guard':
            assert result is None
        assert not db.execute('SELECT 1 FROM waits').fetchall()
    else:
        with pytest.raises(KernelError):
            _consume_read(s, request, 'source' if consumer == 'changed_source' else consumer,
                          returned if consumer == 'changed_source' else None)
    assert changed == [change] and modes == [(False, True), (True, True)]
    assert not db.execute("SELECT 1 FROM gates WHERE code='supervision_timeout'").fetchall()
    bodies = [row[0] for row in db.execute('SELECT body FROM source_observations')]
    if change != 'retired_wait' or consumer != 'changed_source':
        assert all(b'Returned changed requirement' not in body for body in bodies)


@pytest.mark.parametrize('consumer', ['guard', 'source'])
@pytest.mark.parametrize('failure_at', ['reader_open', 'reader_close', 'writer_open', 'writer_commit', 'writer_close'])
def test_read_escalation_preserves_storage_failure(decision_scenario, monkeypatch, consumer, failure_at):
    s, db = decision_scenario
    kernel = s.kernel
    request = kernel.begin_observation(s.delivery.key, execution=s.execution)
    kernel._wait_for_read(request.fence, s.execution, 'controlled_read', 'Original read unavailable')
    kernel.clock = lambda: 700.
    transaction, modes, failure = kernel.storage.transaction, [], OSError('Original storage failure')
    before = tuple(db.iterdump())

    @contextmanager
    def failed(**options):
        write = options.get('write', True)
        modes.append(write)
        if failure_at == ('writer_open' if write else 'reader_open'):
            raise failure
        with transaction(**options) as tx:
            yield tx
            if write and failure_at == 'writer_commit':
                raise failure
        if not write and failure_at == 'reader_close':
            raise failure
        if write and failure_at == 'writer_close':
            raise failure

    monkeypatch.setattr(kernel.storage, 'transaction', failed)
    with pytest.raises(OSError) as caught:
        _consume_read(s, request, consumer)
    assert caught.value is failure
    assert modes == ([False] if failure_at.startswith('reader') else [False, True])
    if failure_at == 'writer_close':
        assert db.execute("SELECT code FROM gates WHERE code='supervision_timeout'").fetchone() is not None
    else:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('change', ['labels', 'parent_order', 'issue_order', 'state', 'order_reliability'])
def test_source_probe_compares_complete_bytes_and_preserves_fresh_pair(decision_scenario, tmp_path, monkeypatch, change):
    from support.offline import GraphScenario
    from support.requirements_scenarios import independent_source

    # Reuse the fixture's normal Task/Run producer and scoped transaction seam.
    s = GraphScenario(tmp_path / 'graph', independent_source())
    kernel, source = s.kernel, s.requirements
    with kernel.storage.transaction(write=False) as tx:
        db = tx.connection
    request = kernel.begin_observation(s.delivery.key, execution=s.execution)
    original = kernel.accept_requirements(request, source)
    ports = s.make_ports(kernel.operation_binding(s.execution))
    old_facts = ports.delivery(original.query)
    if change == 'parent_order':
        changed = replace(source, parent=replace(source.parent, sub_issue_numbers=tuple(reversed(source.parent.sub_issue_numbers))))
    elif change == 'issue_order':
        changed = replace(source, issues=tuple(reversed(source.issues)))
    elif change == 'order_reliability':
        changed = replace(source, parent=replace(source.parent, sub_issue_order_reliable=False))
    else:
        first = replace(source.issues[0], **({'labels': frozenset({'ready-for-human'})} if change == 'labels' else {'state': 'CLOSED'}))
        changed = replace(source, issues=(first, *source.issues[1:]))
    assert changed != source and kernel._source_revisions(changed) == kernel._source_revisions(source)
    before = db.execute('SELECT count(*) FROM source_observations').fetchone()[0]
    transaction, modes = kernel.storage.transaction, []

    @contextmanager
    def recorded(**options):
        modes.append((options.get('write', True), options.get('ordered', True)))
        with transaction(**options) as tx:
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    current = kernel.accept_requirements(request, changed)
    assert modes == [(False, True), (True, True)]
    assert db.execute('SELECT count(*) FROM source_observations').fetchone()[0] == before + 1
    expected = wire_json({'parent': asdict(changed.parent), 'issues': [
        {**asdict(issue), 'labels': sorted(issue.labels)} for issue in changed.issues]})
    assert db.execute('SELECT body FROM source_observations ORDER BY sequence DESC LIMIT 1').fetchone()[0] == expected
    with pytest.raises(KernelError, match='automatic_step_superseded'):
        kernel.accept_delivery(original, old_facts)
    world, fence = kernel.accept_delivery(current, ports.delivery(current.query))
    assert fence == current.fence and world.parent_revision == kernel._source_revisions(source)[0]


@pytest.mark.parametrize('consumer', ['guard', 'source'])
def test_read_fast_path_keeps_original_transaction_budget(decision_scenario, monkeypatch, consumer):
    from agent_run.storage.sqlite import QUERY_LIMIT

    s, db = decision_scenario
    kernel = s.kernel
    request = kernel.begin_observation(s.delivery.key, execution=s.execution)
    transaction, modes = kernel.storage.transaction, []
    before = tuple(db.iterdump())

    @contextmanager
    def exhausted(**options):
        modes.append(options.get('write', True))
        with transaction(**options) as tx:
            tx.bytes_read = QUERY_LIMIT
            assert kernel.owners[-1].bytes_read == QUERY_LIMIT
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', exhausted)
    with pytest.raises(KernelError, match='query_limit'):
        _consume_read(s, request, consumer)
    assert modes == [False] and tuple(db.iterdump()) == before


@pytest.mark.parametrize('invalid', ['missing', 'parent', 'size'])
def test_source_fast_path_does_not_accept_missing_or_invalid_facts(decision_scenario, invalid):
    s, db = decision_scenario
    request = s.kernel.begin_observation(s.delivery.key, execution=s.execution)
    source = (None if invalid == 'missing' else replace(s.requirements, parent=replace(s.requirements.parent,
        **({'number': 999} if invalid == 'parent' else {'body': 'x' * (4 * 1024 * 1024)}))))
    code = {'missing': 'source_not_configured', 'parent': 'parent_identity', 'size': 'source_limit'}[invalid]
    before = tuple(db.iterdump())
    with pytest.raises(KernelError, match=code):
        s.kernel.accept_requirements(request, source)
    assert tuple(db.iterdump()) == before


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
