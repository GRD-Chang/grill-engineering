"""Checks demand belongs to one Core continuation, never a cached permission.

Observation/transaction matrices use initial Core relations and read-only ports.
Representative delivery stories retain private Git and real effects. Both use
the existing ScopedKernel seam; neither claims native save or physical Host proof.
"""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import asdict, replace
from types import SimpleNamespace
import sqlite3
import json

import pytest

from support.github_retries import set_read_retry_sleep

from agent_run.core.operation_contract import ObserveIO, ReadUnavailable, Work
from agent_run.core.operation_runner import OperationRunner, observe_control
from agent_run.errors import KernelError
from agent_run.model import Checks, Commit, Delivery, DeliveryKind, ReadWait, RunSpec, wire_json
from support import offline
from support.effect_boundary import advance_to_effect
from support.offline import Scenario
from support.run_scoped_kernel import ScopedKernel


@contextmanager
def _scoped(original):
    with closing(sqlite3.connect(original.storage.path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, original.storage.run_id)
        kernel.storage.path, kernel.storage.run_id = original.storage.path, original.storage.run_id
        yield kernel, db


@pytest.fixture
def scoped_producer(monkeypatch):
    # Real Git integration stories retain the same Core transaction seam.
    create = offline.create_kernel
    with ExitStack() as owned:
        def producer(*args, **kwargs):
            kernel, _ = owned.enter_context(_scoped(create(*args, **kwargs)))
            return kernel
        monkeypatch.setattr(offline, 'create_kernel', producer)
        yield


@pytest.fixture
def decision_scenario(tmp_path, scoped_producer, request):
    scenario = Scenario(tmp_path / 'scenario', getattr(request, 'param', DeliveryKind.PARENT_ONLY), checks=('fail', 'none'))
    with scenario.kernel.storage.transaction(write=False) as tx:
        db = tx.connection
    yield scenario, db


@pytest.fixture
def reader(tmp_path, request):
    """Actual initial Core relations; observation tests need no repository or effects."""
    from support.core_operations import native_agent_fixture, ReadOnlyPorts
    from support.requirements_scenarios import source_run, independent_source
    from agent_run.core.operation_contract import DeliveryFacts

    path = tmp_path / 'reader.sqlite3'
    if getattr(request, 'param', None) == 'graph':
        original, source = source_run(path, independent_source())
        delivery = original.delivery('ticket:3')
        ports = ReadOnlyPorts(source, delivery.source_ref, delivery.target_ref, DeliveryFacts(delivery.base, None))
    else:
        delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'requirement', Commit('base', 'tree'),
                            'refs/heads/parent', 'refs/heads/main')
        original, ports = native_agent_fixture(path, RunSpec('reader-run', 'example/project', 1,
            'parent', 'graph', (delivery,)))
    with _scoped(original) as (kernel, db):
        execution = kernel.claim_executor('reader-owner')
        yield SimpleNamespace(kernel=kernel, execution=execution, delivery=delivery,
            requirements=ports.requirements, ports=ports,
            runner=lambda: OperationRunner(kernel, execution, lambda _binding: ports)), db


def _allocate(reader):
    from support.core_operations import prepare_scripted_agent

    runner = reader.runner()
    for _ in range(8):
        step = prepare_scripted_agent(reader.kernel, runner.run_once())
        if step.call is not None:
            return step.call
    raise AssertionError('Initial Core did not allocate Development')


@pytest.mark.parametrize('failure', ['malformed', 'read_failed', 'pipe_error'])
def test_complete_read_keeps_source_gate_or_original_wait_window(reader, tmp_path, monkeypatch, failure):
    import sys
    from agent_run.adapters import github_commands
    from agent_run.adapters.remote import RemoteEffects

    s, db = reader
    bounded, calls = github_commands._run_bounded_command, []
    native = RemoteEffects(tmp_path, 'example/project', runner_identity={'source': 'host'})
    code = ("print('{invalid')" if failure == 'malformed' else
            "import sys;sys.stderr.write('HTTP 403 unavailable');raise SystemExit(1)")

    def returned(arguments, **options):
        calls.append('read')
        if failure == 'pipe_error':
            raise OSError('Controlled incomplete transport')
        return bounded([sys.executable, '-I', '-c', code], **options)

    monkeypatch.setattr(github_commands, '_run_bounded_command', returned)
    set_read_retry_sleep(monkeypatch, lambda _: None)
    monkeypatch.setattr(s.ports, 'source', native.source)
    first = s.runner().run_once()
    assert db.execute('SELECT count(*) FROM effects').fetchone()[0] == 0
    if failure == 'malformed':
        assert first.kind == 'paused' and first.reason == 'github_invalid_response'
        assert db.execute('SELECT code FROM gates').fetchone()[0] == 'deterministic_contradiction'
        assert not db.execute('SELECT 1 FROM waits').fetchall() and calls == ['read']
    else:
        assert first.kind == 'waiting'
        original = tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone())
        assert original == (100., 700.)
        s.kernel.clock = lambda: 105.25
        assert s.runner().run_once().kind == 'waiting'
        assert tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone()) == original
        assert not db.execute('SELECT 1 FROM gates').fetchall()
        assert calls == ['read'] * (2 if failure == 'pipe_error' else 6)


@pytest.mark.parametrize('capacity', [0, 1])
def test_initial_observation_uses_existing_read_snapshot_without_a_save(reader, monkeypatch, capacity):
    s, db = reader
    transaction, modes = s.kernel.storage.transaction, []

    @contextmanager
    def counted(**options):
        modes.append(options.get('write', True))
        readonly = options.get('write') is False
        if readonly:
            db.execute('PRAGMA query_only=ON')
        try:
            with transaction(**options) as tx:
                yield tx
        finally:
            if readonly:
                db.execute('PRAGMA query_only=OFF')

    monkeypatch.setattr(s.kernel.storage, 'transaction', counted)
    before = tuple(db.iterdump())
    request = s.kernel.next_work(s.execution, agent_capacity=capacity)
    assert isinstance(request, ObserveIO) and request.work.subject == s.delivery.key
    assert request.work.allow_agent is bool(capacity)
    assert modes == [False, False], 'Observation must use the selection reader, then fresh supervision'
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('change', ['stop', 'epoch', 'source', 'scope', 'deadline'])
def test_initial_observation_is_rechecked_after_reader_exit(reader, monkeypatch, change):
    s, db = reader
    kernel, subject = s.kernel, s.delivery.key
    transaction, observe = kernel.storage.transaction, kernel._observe_work
    captured, changed = [], []

    def observed(*args, **options):
        request = observe(*args, **options)
        captured.append(request)
        return request

    @contextmanager
    def interleaved(**options):
        with transaction(**options) as tx:
            yield tx
        if captured and not changed:
            assert not db.in_transaction
            changed.append(change)
            with monkeypatch.context() as mutation:
                mutation.setattr(kernel.storage, 'transaction', transaction)
                if change in {'stop', 'epoch'}:
                    kernel.control('stop', 'stop-after-observation-selection')
                    if change == 'epoch':
                        kernel.confirm_executor_exit(s.execution)
                        kernel.control('resume', 'resume-after-observation-selection')
                        assert kernel.claim_executor('successor-owner').epoch > s.execution.epoch
                elif change == 'source':
                    kernel.refresh(lambda: replace(s.requirements, parent=replace(s.requirements.parent, body='Concurrent requirement')))
                elif change == 'scope':
                    # Negative concurrent scope input, not a grant or business producer.
                    db.execute('UPDATE subjects SET purpose=? WHERE run_id=? AND id=?', ('integration', s.execution.run_id, subject))
                else:
                    kernel._wait_for_read(captured[0].request.fence, s.execution, 'controlled_unavailable', 'Original unavailable read')
                    deadline = db.execute('SELECT deadline FROM waits').fetchone()[0]
                    kernel.clock = lambda: deadline

    monkeypatch.setattr(kernel, '_observe_work', observed)
    monkeypatch.setattr(kernel.storage, 'transaction', interleaved)
    result = kernel.next_work(s.execution)
    assert changed == [change] and len(captured) == 1
    assert not isinstance(result, ObserveIO)
    assert result.reason == ('executor_fenced' if change in {'stop', 'epoch'} else
                             'supervision_timeout' if change == 'deadline' else 'automatic_step_superseded')
    assert not db.execute('SELECT id FROM effects').fetchall()


@pytest.mark.parametrize('failure', ['budget', 'poison', 'close_io', 'close_kernel'])
def test_initial_observation_read_failure_never_routes_to_a_writer(reader, monkeypatch, failure):
    from agent_run.storage.sqlite import QUERY_LIMIT

    s, db = reader
    kernel, transaction, observe = s.kernel, s.kernel.storage.transaction, s.kernel._observe_work
    modes = []
    original = OSError('Original selection reader close failure') if failure == 'close_io' else KernelError('original_reader_close')
    before = tuple(db.iterdump())

    def interrupted(tx, *args, **options):
        if failure == 'budget':
            tx.bytes_read = QUERY_LIMIT
        elif failure == 'poison':
            with pytest.raises(sqlite3.DatabaseError):
                tx.execute('SELECT * FROM missing_observation_fixture_relation')
            assert tx.failed
        return observe(tx, *args, **options)

    @contextmanager
    def recorded(**options):
        modes.append(options.get('write', True))
        with transaction(**options) as tx:
            yield tx
        if len(modes) == 1 and failure.startswith('close'):
            raise original

    monkeypatch.setattr(kernel, '_observe_work', interrupted)
    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    with pytest.raises(OSError if failure == 'close_io' else KernelError) as caught:
        kernel.next_work(s.execution)
    if failure.startswith('close'):
        assert caught.value is original and modes == [False]
    else:
        assert caught.value.code == ('query_limit' if failure == 'budget' else 'transaction_aborted')
    assert modes and not any(modes) and tuple(db.iterdump()) == before


def test_initial_observation_construction_keeps_original_failure_classification(reader, monkeypatch):
    from agent_run.errors import ObservationSuperseded

    s, db = reader
    failure = ObservationSuperseded('automatic_step_superseded')
    fail, seen = s.kernel.fail_work, []

    def unavailable(*args, **options):
        raise failure

    def classified(work, error, **options):
        assert not db.in_transaction
        seen.append(error)
        return fail(work, error, **options)

    monkeypatch.setattr(s.kernel, '_observe_work', unavailable)
    monkeypatch.setattr(s.kernel, 'fail_work', classified)
    result = s.kernel.next_work(s.execution)
    assert seen == [failure] and (result.kind, result.reason) == ('waiting', 'automatic_step_superseded')


def test_allocated_deadline_retains_original_mutating_route(reader, monkeypatch):
    s, db = reader
    call = _allocate(s)
    s.kernel.clock = lambda: call.deadline_at

    def no_io(*args, **options):
        pytest.fail('Expired allocated invocation must settle before observation I/O')

    monkeypatch.setattr(s.ports, 'source', no_io)
    result = s.runner().run_once(agent_capacity=0)
    assert (result.kind, result.reason) == ('paused', 'invocation_deadline_exceeded')
    assert db.execute('SELECT dispatch_state FROM invocation_outputs WHERE invocation=?', (call.invocation_id,)).fetchone()[0] == 'unavailable'
    assert not db.execute('SELECT id FROM leases').fetchall()


@pytest.mark.parametrize('state', ['prepared', 'sent', 'stale_prepared', 'stale_observed'])
def test_pending_effect_keeps_original_route_and_settlement(decision_scenario, monkeypatch, state):
    from agent_run.core.operation_contract import EffectIO

    s, db = decision_scenario
    held = advance_to_effect(s, monkeypatch, 'workspace', sent=state == 'sent')
    if state == 'stale_observed':
        assert held.authorize is not None
        result = s.port.execute(held.effect, held.authorize)
        request = s.kernel.next_work(s.execution)
        assert isinstance(request, EffectIO) and request.kind == 'readback'
        assert s.kernel._reconcile(request, result).kind == 'progress'
        assert db.execute('SELECT status FROM effects WHERE id=?', (held.effect.effect_id,)).fetchone()[0] == 'observed'
    if state.startswith('stale_'):
        s.kernel.refresh(lambda: replace(s.requirements, parent=replace(s.requirements.parent, body='Concurrent requirement')))
    request = s.kernel.next_work(s.execution)
    if state == 'sent':
        assert isinstance(request, EffectIO) and request.kind == 'readback'
    elif state == 'prepared':
        assert isinstance(request, ObserveIO)
    else:
        assert request.kind == 'progress'
        assert request.reason == ('stale_intent_retired' if state == 'stale_prepared' else 'original_outcome_retained')


@pytest.mark.parametrize('prefix', ['ticket_before_pr', 'parent_before_push'])
def test_pr_activity_time_does_not_revoke_current_checks(request, prefix, monkeypatch):
    from agent_run.adapters.source import normalize_required_checks

    # Start before the PR/checks producer so both observations differ only in
    # activity time; the shared real prefix already supplied Development/Review.
    s = request.getfixturevalue(prefix)
    with _scoped(s.kernel) as (s.kernel, _db):
        s.checks[:] = ['none']
        delivery, activity = s.port.delivery, ['2026-10-21T10:00:00Z']

        def observed(*args, **kwargs):
            facts = delivery(*args, **kwargs)
            if facts.checks is None:
                return facts
            pr = facts.pr
            assert pr is not None
            raw = {'number': pr.number, 'state': pr.state.lower(), 'updated_at': activity[0],
                   'head': {'sha': pr.head.sha, 'ref': pr.source_ref.removeprefix('refs/heads/'),
                            'repo': {'full_name': pr.head_repository}},
                   'base': {'sha': pr.base.sha, 'ref': pr.target_ref.removeprefix('refs/heads/'),
                            'repo': {'full_name': pr.repository}}}
            checks = normalize_required_checks({'pr_before': raw, 'pr_after': raw, 'head_sha': pr.head.sha,
                'repository': pr.repository, 'repository_default_branch': 'main', 'required_checks': [],
                'classic_checks': [], 'classic_contexts': [], 'classic_protection_absent': False, 'rulesets': []})
            return replace(facts, checks=checks)

        monkeypatch.setattr(s.port, 'delivery', observed)
        boundary = 'merge' if s.delivery.kind == DeliveryKind.TICKET else 'approval'
        for _ in range(100):
            if s.kernel.status(s.delivery.key)['phase'] == boundary:
                break
            assert s.step().kind not in {'paused', 'waiting'}
        else:
            pytest.fail('Normal producer did not reach the checks authority boundary')
        with s.kernel.storage.transaction(write=False) as tx:
            row = tx.one('SELECT material_id FROM checks WHERE run_id=? AND id=(SELECT checks_id FROM subjects WHERE run_id=? AND id=?)',
                         (s.execution.run_id, s.execution.run_id, s.delivery.key))
            original = tx.get(row[0], s.delivery.key)
        activity[0] = '2026-10-21T10:00:01Z'
        current = s.world()
        assert current.checks is not None and current.checks.logs != json.loads(original)['logs']
        if boundary == 'approval':
            assert s.kernel.control('approve', 'approve-current-checks', subject=s.delivery.key, world=current) == 'pending'
        assert s.run().kind == 'complete'
        assert sum(write['operation'] == 'merge' for write in s.writes()) == 1
        with s.kernel.storage.transaction(write=False) as tx:
            assert tx.get(row[0], s.delivery.key) == original  # Preserve the original API evidence.


@pytest.mark.parametrize('after_selection', [-0.0005, 0.0, 0.0005])
def test_state_keeps_retry_when_deadline_arrives_after_selection(reader, monkeypatch, after_selection):
    s, db = reader
    kernel, now = s.kernel, [100.0]
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(s.delivery.key, execution=s.execution)
    kernel._wait_for_read(request.fence, s.execution, 'controlled_read', 'Original read unavailable')
    retry_at = db.execute('SELECT next_retry_at FROM waits').fetchone()[0]
    now[0] = retry_at - 0.001
    choose, choices = kernel._operation_choice, []

    def crossed(*args, **kwargs):
        selected = choose(*args, **kwargs)
        choices.append(selected)
        now[0] = retry_at + after_selection
        return selected

    monkeypatch.setattr(kernel, '_operation_choice', crossed)
    before = tuple(db.iterdump())
    state = kernel.operation_state()
    assert choices == [None]
    assert tuple(db.iterdump()) == before
    assert state.disposition == 'waiting' and state.wake_at == retry_at, state
    # The next fresh selector must immediately see the now-due original work.
    now[0] = retry_at
    assert kernel.operation_state().disposition == 'ready'


@pytest.mark.parametrize('after_selection', [-0.0005, 0.0, 0.0005])
def test_state_keeps_capacity_retry_when_selection_crosses_recovery_ready(reader, monkeypatch, after_selection):
    s, db = reader
    kernel, now = s.kernel, [100.0]
    kernel.clock = lambda: now[0]
    call = _allocate(s)
    kernel.bind_thread(call, 'capacity-retained-thread')
    assert kernel.dispatch_agent(call)
    process = dict(pid=23, start_token='capacity-process', pgid=23, host_pid=11, host_start_token='host-start')
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    kernel.agent_exited(call, process, 1, failure_kind='capacity')
    with kernel.storage.transaction(write=False) as tx:
        retry_at = tx.one('SELECT recovery_ready_at FROM invocation_outputs WHERE run_id=?', (s.execution.run_id,))[0]
    assert retry_at == 130.0 and not db.execute('SELECT * FROM waits').fetchall()
    now[0] = retry_at - 0.001
    choose, choices = kernel._operation_choice, []

    def crossed(*args, **options):
        selected = choose(*args, **options)
        choices.append(selected)
        now[0] = retry_at + after_selection
        return selected

    before = tuple(db.iterdump())
    with monkeypatch.context() as boundary:
        boundary.setattr(kernel, '_operation_choice', crossed)
        state = kernel.operation_state()
    assert choices == [None] and tuple(db.iterdump()) == before
    assert state.disposition == 'waiting' and state.wake_at == retry_at, state
    now[0] = retry_at
    assert kernel.operation_state().disposition == 'ready'


@pytest.mark.parametrize('seat', ['worker', 'preparation'])
def test_busy_supervision_keeps_sibling_cancellation_and_exact_deadlines(scoped_producer, tmp_path, monkeypatch, seat):
    from agent_run.model import Policy
    from support.core_operations import CoreOperations
    from support.offline import GraphScenario
    from support.requirements_scenarios import independent_source

    s = GraphScenario(tmp_path / 'siblings', independent_source(), policy=Policy(concurrency=2))
    now = [100.0]
    s.kernel.clock = lambda: now[0]
    operations = CoreOperations(s.kernel, s.execution, s.make_ports, prepare_agents=seat == 'worker')
    held = []
    for _ in range(32):
        step = operations.once(agent_capacity=2 - len(held))
        value = step.call if seat == 'worker' else step.preparation
        if value is not None:
            held.append(value)
            now[0] += 1.0
        if len(held) == 2:
            break
    assert len(held) == 2 and held[0].subject != held[1].subject
    deadline = lambda c: c.deadline_at if seat == 'worker' else c.preparation_deadline
    identity = lambda c: c.lease_id if seat == 'worker' else c.reservation_id
    first, second = sorted(held, key=deadline)
    now[0] = deadline(first) - 0.001
    choose = s.kernel._operation_choice

    def crossed(*args, **options):
        selected = choose(*args, **options)
        now[0] = deadline(first)
        return selected

    with monkeypatch.context() as boundary:
        boundary.setattr(s.kernel, '_operation_choice', crossed)
        state = operations.runner.state()
    assert state.cancel == (identity(first),)
    assert state.wake_at == deadline(first)
    now[0] = deadline(first)
    supervision = operations.runner.supervise()
    assert supervision.cancel == (identity(first),)
    assert supervision.wake_at == deadline(second)
    assert set(supervision.active if seat == 'worker' else supervision.preparing) == {identity(c) for c in held}
    budget = {c.subject: s.kernel.status(c.subject)['budgets'] for c in held}
    s.kernel.control('stop', 'stop-siblings')
    stopped = operations.runner.supervise()
    assert set(stopped.cancel) == {identity(c) for c in held}
    assert set(stopped.active if seat == 'worker' else stopped.preparing) == {identity(c) for c in held}
    assert {c.subject: s.kernel.status(c.subject)['budgets'] for c in held} == budget


@pytest.mark.parametrize('kind', ['state', 'supervise'])
@pytest.mark.parametrize('failure', ['budget', 'poison'])
def test_supervision_keeps_transaction_budget_and_poison(reader, monkeypatch, kind, failure):
    from agent_run.storage.sqlite import QUERY_LIMIT

    s, db = reader
    transaction = s.kernel.storage.transaction
    before = tuple(db.iterdump())

    @contextmanager
    def exhausted(**options):
        assert options.get('write') is False
        with transaction(**options) as tx:
            if failure == 'budget':
                tx.bytes_read = QUERY_LIMIT
            else:
                with pytest.raises(sqlite3.DatabaseError):
                    tx.execute('SELECT * FROM intentionally_missing_supervision_relation')
                assert tx.failed
            yield tx

    monkeypatch.setattr(s.kernel.storage, 'transaction', exhausted)
    with pytest.raises(KernelError, match='query_limit' if failure == 'budget' else 'transaction_aborted'):
        getattr(s.runner(), kind)()
    assert tuple(db.iterdump()) == before


def test_supervision_reloads_epoch_without_granting_old_runner_permission(reader, monkeypatch):
    s, db = reader
    runner, owners = s.runner(), []
    supervise = s.kernel._operation_supervision_in

    def observed(tx, snapshot, *facts):
        owners.append((snapshot.run['executor'], snapshot.run['epoch']))
        return supervise(tx, snapshot, *facts)

    monkeypatch.setattr(s.kernel, '_operation_supervision_in', observed)
    runner.supervise()
    assert owners[-1] == (s.execution.owner, s.execution.epoch)
    s.kernel.control('stop', 'stop-original-owner')
    s.kernel.confirm_executor_exit(s.execution)
    s.kernel.control('resume', 'resume-original-run')
    successor = s.kernel.claim_executor('successor-owner')
    runner.supervise()
    assert owners[-1] == (successor.owner, successor.epoch) and successor.epoch > s.execution.epoch
    rejected = runner.run_once(agent_capacity=0)
    assert rejected.kind == 'paused' and rejected.reason == 'executor_fenced'
    assert not db.execute('SELECT id FROM leases').fetchall()


def test_continuation_loader_rereads_return_without_lease_and_capacity_zero(reader):
    from agent_run.core.attempt_continuation import load_continuation

    s, db = reader
    call = _allocate(s)
    with s.kernel.storage.transaction(write=False) as tx:
        live = load_continuation(tx, call.attempt_id)
    assert live.prior.status == 'running' and live.prior.raw_id is None
    s.kernel.receive(call, b'{"result_kind":"development","summary":"Original return","human_blockers":null}')
    assert db.execute('SELECT count(*) FROM leases').fetchone()[0] == 0
    with s.kernel.storage.transaction(write=False) as tx:
        returned = load_continuation(tx, call.attempt_id)
    assert returned.thread_id == live.thread_id and returned.context == live.context
    assert returned.prior.status == 'returned' and returned.prior.raw_id is not None
    step = s.runner().run_once(agent_capacity=0)
    assert step.kind == 'progress' and step.call is None
    assert not db.execute('SELECT id FROM leases').fetchall()


@pytest.mark.parametrize('missing_lease', [False, True])
def test_continuation_loader_does_not_treat_sent_return_as_exit(reader, missing_lease):
    from agent_run.core.attempt_continuation import load_continuation

    s, db = reader
    call = _allocate(s)
    assert s.kernel.dispatch_agent(call)
    s.kernel.receive(call, b'{"result_kind":"development","summary":"Original return","human_blockers":null}')
    if missing_lease:
        # Negative input removes only capacity, never supplies exit evidence.
        db.execute('DELETE FROM leases WHERE run_id=?', (s.execution.run_id,))
    with s.kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, call.attempt_id)
        waiting = s.kernel._continuation_wait(tx, s.kernel._job(tx, call.subject))
    assert facts.prior.status == 'returned' and facts.prior.dispatch_state == 'sent'
    assert facts.prior.exit_code is None and facts.prior.process_identity is None
    assert waiting.reason == 'previous_invocation_exit'
    assert db.execute('SELECT count(*) FROM invocations').fetchone()[0] == 1
    supervision = s.runner().supervise()
    state = s.kernel.operation_state()
    assert state.disposition == 'waiting' and not s.kernel.worker_settled(call)
    assert (supervision.active, supervision.cancel, supervision.wake_at, supervision.preparing) == (
        state.active, state.cancel, state.wake_at, state.preparing)


def test_real_progress_finishes_readonly_without_changing_facts_or_io(decision_scenario, monkeypatch):
    from collections import Counter

    s, db = decision_scenario
    kernel, records, physical = s.kernel, [], Counter()
    transaction, finish, make_ports = kernel.storage.transaction, kernel._finish_automatic_step, s.make_ports
    finishing = False

    def facts():
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: [tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')] for name in tables}

    @contextmanager
    def recorded(**options):
        if finishing:
            records.append(options.get('write', True))
        readonly_finish = finishing and options.get('write') is False
        if readonly_finish:
            db.execute('PRAGMA query_only=ON')
        try:
            with transaction(**options) as tx:
                yield tx
        finally:
            if readonly_finish:
                db.execute('PRAGMA query_only=OFF')

    def completed(*args):
        nonlocal finishing
        before, observed = facts(), physical.copy()
        finishing = True
        try:
            result = finish(*args)
        finally:
            finishing = False
        assert facts() == before and physical == observed
        return result

    def ports_for(binding):
        ports = make_ports(binding)
        for name in ('source', 'delivery', 'inspect', 'reconcile', 'execute'):
            method = getattr(ports, name)
            def called(*args, _name=name, _method=method, **kwargs):
                physical[_name] += 1
                return _method(*args, **kwargs)
            monkeypatch.setattr(ports, name, called)
        return ports

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    monkeypatch.setattr(kernel, '_finish_automatic_step', completed)
    monkeypatch.setattr(s, 'make_ports', ports_for)
    assert s.step().kind == 'progress'
    assert records == [False], 'A real completed transition with no read wait must not request a save'


def test_finish_writer_reobserves_real_progress_after_reader_exit(decision_scenario, monkeypatch):
    from agent_run.model import Step

    s, db = decision_scenario
    kernel, subject = s.kernel, s.delivery.key
    with kernel.storage.transaction(write=False) as tx:
        token = kernel._operation_token(tx, subject, False)
    transaction, records, advanced = kernel.storage.transaction, [], []
    injecting = False

    @contextmanager
    def interleaved(**options):
        nonlocal injecting
        outer = not injecting
        if outer:
            records.append(options.get('write', True))
        with transaction(**options) as tx:
            yield tx
        if outer and not options.get('write', True) and not advanced:
            assert not db.in_transaction
            injecting = True
            try:
                advanced.append(s.step())
            finally:
                injecting = False

    monkeypatch.setattr(kernel.storage, 'transaction', interleaved)
    step = Step('progress', subject)
    assert kernel._finish_automatic_step(token, s.execution, step) == step
    assert records == [False, True] and advanced[0].kind == 'progress'
    assert not db.execute('SELECT 1 FROM gates').fetchall()


def test_zero_physical_capacity_still_consumes_the_original_return(reader):
    s, db = reader
    call = _allocate(s)
    s.kernel.receive(call, b'{"result_kind":"development","summary":"Original return","human_blockers":null}')
    original = db.execute('SELECT result_id FROM attempts WHERE run_id=? AND id=?',
                          (call.execution.run_id, call.attempt_id)).fetchone()[0]
    assert original is not None
    count = db.execute('SELECT count(*) FROM invocations').fetchone()[0]
    result = s.runner().run_once(agent_capacity=0)
    assert result.kind == 'progress' and result.call is None and result.preparation is None
    assert s.kernel.status(call.subject)['phase'] == 'candidate'
    assert db.execute('SELECT count(*) FROM invocations').fetchone()[0] == count
    assert db.execute('SELECT result_id FROM attempts WHERE run_id=? AND id=?',
                      (call.execution.run_id, call.attempt_id)).fetchone()[0] == original


def _consume_read(scenario, request, consumer, source=None):
    if consumer == 'guard':
        return scenario.kernel._guard_automatic_step(request.fence, scenario.execution, after_read=True)
    return scenario.kernel.accept_requirements(request, source or scenario.requirements)


@pytest.mark.parametrize('consumer', ['guard', 'source'])
@pytest.mark.parametrize('has_wait', [False, True])
def test_healthy_read_uses_one_query_only_snapshot(reader, monkeypatch, consumer, has_wait):
    s, db = reader
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
def test_exact_read_deadline_commits_only_under_fresh_writer(reader, monkeypatch, consumer):
    s, db = reader
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
def test_read_to_writer_gap_rechecks_all_current_facts(reader, monkeypatch, consumer, change):
    s, db = reader
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
def test_read_escalation_preserves_storage_failure(reader, monkeypatch, consumer, failure_at):
    s, db = reader
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


@pytest.mark.parametrize('reader', ['graph'], indirect=True)
@pytest.mark.parametrize('change', ['labels', 'parent_order', 'issue_order', 'state', 'order_reliability'])
def test_source_probe_compares_complete_bytes_and_preserves_fresh_pair(reader, monkeypatch, change):
    s, db = reader
    kernel, source = s.kernel, s.requirements
    request = kernel.begin_observation(s.delivery.key, execution=s.execution)
    original = kernel.accept_requirements(request, source)
    ports = s.ports
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
def test_read_fast_path_keeps_original_transaction_budget(reader, monkeypatch, consumer):
    from agent_run.storage.sqlite import QUERY_LIMIT

    s, db = reader
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
def test_source_fast_path_does_not_accept_missing_or_invalid_facts(reader, invalid):
    s, db = reader
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
                assert source.query is not None and source.query.read_checks is False
                request = kernel.accept_requirements(source, ports.source(source.parent_number))
                assert request.query == source.query
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


@pytest.mark.parametrize('next_observation', ['complete-contradiction', 'original-deadline'])
def test_mixed_source_failure_never_commits_or_renews_a_supervision_window(
    reader, monkeypatch, tmp_path, next_observation,
):
    from agent_run.adapters.remote import RemoteEffects
    from support.remote_effects import _reader

    s, db = reader
    directory = tmp_path / 'mixed-source'
    directory.mkdir()
    _, log = _reader(directory, monkeypatch)
    state = directory / 'responses.json'
    document = json.loads(state.read_text())
    document['9']['state'] = 'INVALID'
    document['graphql_errors'] = [{'message': 'Controlled missing required permission'}]
    state.write_text(json.dumps(document))
    set_read_retry_sleep(monkeypatch, lambda _: None)
    native = RemoteEffects(tmp_path, 'example/project', runner_identity={'source': 'host'})
    monkeypatch.setattr(s.ports, 'source', native.source)
    before = [tuple(row) for row in db.execute('SELECT * FROM source_observations')]
    assert s.runner().run_once().kind == 'waiting'
    original = tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone())
    assert original == (100., 700.)
    s.kernel.clock = lambda: 105.25
    assert s.runner().run_once().kind == 'waiting'
    assert tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone()) == original
    if next_observation == 'complete-contradiction':
        del document['graphql_errors']
        state.write_text(json.dumps(document))
        retry_at = db.execute('SELECT next_retry_at FROM waits').fetchone()[0]
        assert 105.25 < retry_at < original[1]
        s.kernel.clock = lambda: retry_at + 0.25
        result = s.runner().run_once()
        assert (result.kind, result.reason) == ('paused', 'github_invalid_response')
        assert db.execute('SELECT code FROM gates').fetchone()[0] == 'deterministic_contradiction'
    else:
        s.kernel.clock = lambda: 700.
        result = s.runner().run_once()
        assert (result.kind, result.reason) == ('paused', 'supervision_timeout')
    assert [tuple(row) for row in db.execute('SELECT * FROM source_observations')] == before
    assert not db.execute('SELECT 1 FROM effects').fetchall()
    assert all(json.loads(row)[:2] == ['api', 'graphql'] for row in log.read_text().splitlines())


def test_final_attempt_overflow_is_incomplete_and_preserves_original_wait(reader, monkeypatch, tmp_path):
    from agent_run.adapters.remote import RemoteEffects
    from test_source_snapshot_observation import _flat

    s, db = reader
    directory = tmp_path / 'exhausted-source'
    directory.mkdir()
    _, state, log = _flat(directory, monkeypatch)
    document = json.loads(state.read_text())
    document['9']['body'] = document['3']['body'] = 'T' * 630000
    state.write_text(json.dumps(document))
    program = directory / 'bin/gh'
    marker = "    if 'CurrentRequiredChecks' in fields['query']:"
    text = program.read_text()
    assert marker in text
    program.write_text(text.replace(marker, '''    if 'SourceSnapshot' in fields['query']:
        with open(os.environ['SOURCE_TEST_LOG']) as calls: attempt=sum(1 for _ in calls)
        if attempt%3:
            print('HTTP 503 controlled temporary failure',file=sys.stderr);raise SystemExit(1)
''' + marker, 1))
    native = RemoteEffects(tmp_path, 'example/project', runner_identity={'source': 'host'})
    monkeypatch.setattr(s.ports, 'source', native.source)
    before = [tuple(row) for row in db.execute('SELECT * FROM source_observations')]
    assert s.runner().run_once().kind == 'waiting'
    assert len(log.read_text().splitlines()) == 3
    original = tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone())
    assert original == (100., 700.)
    assert not db.execute('SELECT 1 FROM gates').fetchall()
    s.kernel.clock = lambda: 105.25
    assert s.runner().run_once().kind == 'waiting'
    assert len(log.read_text().splitlines()) == 6
    assert tuple(db.execute('SELECT started_at,deadline FROM waits').fetchone()) == original
    assert not db.execute('SELECT 1 FROM gates').fetchall()
    assert [tuple(row) for row in db.execute('SELECT * FROM source_observations')] == before
    assert not db.execute('SELECT 1 FROM effects').fetchall()
