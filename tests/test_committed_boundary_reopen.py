"""Committed Core boundaries, closed handles and independent SQLite readback.

Normal Task/Run producers and the existing ScopedKernel transaction seam keep
these decision tests independent of native save admission and physical Hosts.
Matrix workspace/ref results are explicit inputs, never seeded completion.
The separate branch-intent test uses real local Git through the same Core seam.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_run.core.operation_contract import DeliveryFacts, Work
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, EffectResult, Policy, RunSpec, wire_json
from support.core_operations import ReadOnlyPorts, observed_call
from support.repository_fixture import create_kernel, foreign_task_run
from support.run_scoped_kernel import ScopedKernel


BASE = Commit('a' * 40, 'b' * 40)
SUBJECT = 'ticket:2'


class CommittedReplyLost(RuntimeError):
    pass


class WorkspaceFacts(ReadOnlyPorts):
    """Only synthetic external results; all dispatch/receipt writes are Core's."""
    def __init__(self, source, spec, *, missing_run_branch):
        delivery = spec.deliveries[0]
        super().__init__(source, delivery.source_ref, delivery.target_ref,
            DeliveryFacts(None if missing_run_branch else BASE, None,
                publisher_actor='fixture-publisher',
                publisher_identity=spec.inputs.runner_identity))
        self.dispatched = []

    def execute(self, effect, authorize):
        assert effect.operation in {'workspace', 'run_branch'}
        authorize.prepare()()
        self.dispatched.append(effect)
        self.facts = replace(self.facts, target=BASE,
            source=BASE if effect.operation == 'workspace' else None)
        return EffectResult('target', commit=BASE)


def rows(db, table, run='A'):
    return tuple(tuple(row) for row in db.execute(
        f'SELECT * FROM {table} WHERE run_id=? ORDER BY rowid', (run,)))


def snapshot(db, run):
    """All owned rows, including immutable materials and the original Task."""
    result = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if 'run_id' in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            result[table] = rows(db, table, run)
    result['runs'] = tuple(tuple(row) for row in db.execute('SELECT * FROM runs WHERE id=?', (run,)))
    result['tasks'] = tuple(tuple(row) for row in db.execute('SELECT * FROM tasks WHERE current_run=?', (run,)))
    return result


@contextmanager
def opened(path):
    # mode=rw refuses a missing file: reopening cannot silently create a Run.
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, 'A')
        kernel.storage.path, kernel.storage.run_id = path, 'A'
        yield db, kernel


def arm_fault(monkeypatch, kernel, db, boundary):
    queries = {
        'refresh': "SELECT source_sequence FROM runs WHERE id='A'",
        'ready': "SELECT subject,ready FROM work_queue WHERE run_id='A'",
        'preparation': "SELECT scope_id FROM waits WHERE run_id='A' AND kind='agent_preparation'",
        'admit': "SELECT id FROM invocations WHERE run_id='A'",
        'receipt': "SELECT receipt_id FROM effects WHERE run_id='A' AND status='observed'",
        'consumed': "SELECT identity FROM history WHERE run_id='A' AND kind='effect_consumed'",
        'intent': "SELECT id FROM effects WHERE run_id='A' AND status='prepared'",
        'charge': "SELECT id FROM attempts WHERE run_id='A' AND accounted=1",
    }
    original, committed = kernel.storage.transaction, []

    @contextmanager
    def transaction(**kwargs):
        with original(**kwargs) as tx:
            before = tuple(map(tuple, db.execute(queries[boundary])))
            yield tx
            changed = tuple(map(tuple, db.execute(queries[boundary]))) != before
        # Original transaction owner has really COMMITted; no rollback fault.
        if changed:
            assert not db.in_transaction
            with closing(sqlite3.connect(kernel.storage.path)) as witness:
                committed.append(snapshot(witness, 'A'))
            raise CommittedReplyLost(boundary)

    monkeypatch.setattr(kernel.storage, 'transaction', transaction)
    return committed


def until_preparation(runner):
    for _ in range(8):
        step = runner.run_once()
        assert step.call is None
        if step.preparation is not None:
            return step.preparation
        assert step.kind == 'progress', step
    raise AssertionError('No preparation after bounded Core transitions')


def allocate_again(kernel, runner, *, allow_agent=True):
    # ScopedKernel intentionally has a single transaction handle. Exercise the
    # allocation decision directly when Runner's idle path needs nested reads.
    world = runner.observe(SUBJECT)
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, SUBJECT, runner.execution)
        work = Work(runner.execution, kernel._operation_token(tx, SUBJECT, False),
            SUBJECT, False, allow_agent, None, None)
        return kernel._allocate(tx, run, job, work, world)


@pytest.mark.parametrize('boundary,operation', [
    ('refresh', 'workspace'), ('ready', 'workspace'),
    ('preparation', 'workspace'), ('admit', 'workspace'),
    ('receipt', 'workspace'), ('consumed', 'workspace'), ('consumed', 'run_branch'),
    ('charge', 'workspace'),
], ids=['refresh', 'ready-queue', 'preparation-reservation', 'admit-agent',
        'effect-receipt', 'effect-consumed-workspace', 'effect-consumed-run-branch',
        'release-charge'])
def test_committed_boundary_reopens_original_work(tmp_path, monkeypatch, boundary, operation):
    from agent_run.model import Issue, ParentIssue, Requirements
    from support.offline import fixture_inputs

    source = Requirements(ParentIssue(1, 'Parent', 'Original requirement', (2,)), (
        Issue(2, 'Ticket', 'Original ticket', 'OPEN', frozenset({'ready-for-agent'})),))
    delivery = Delivery(SUBJECT, DeliveryKind.TICKET, 2, 'unobserved', BASE,
        'refs/heads/ticket', 'refs/heads/run', checkout=str(tmp_path / 'checkout'),
        owned_root=str(tmp_path), common_git_dir=str(tmp_path / 'git'))
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unobserved', BASE,
        delivery.target_ref, 'refs/heads/main')
    spec = RunSpec('A', 'example/repository', 1, 'unobserved', 'unobserved', (delivery, final),
        Policy(ticket_rounds=2, development_deadline_seconds=120),
        repository_root=str(tmp_path), language='zh', inputs=fixture_inputs())
    path = tmp_path / 'repository.sqlite3'
    created = create_kernel(path, spec, requirements=source)
    foreign_task_run(created, spec, source)
    ports = WorkspaceFacts(source, spec, missing_run_branch=operation == 'run_branch')

    with opened(path) as (db, kernel):
        execution = kernel.claim_executor('original-owner')
        runner = OperationRunner(kernel, execution, lambda _: ports)
        sibling, budgets = snapshot(db, 'other-run'), rows(db, 'budgets')
        binding = rows(db, 'task_runs')
        if boundary in {'refresh', 'ready', 'preparation', 'admit'}:
            assert runner.run_once().kind == 'progress'  # workspace receipt
            assert runner.run_once().kind == 'progress'  # workspace consumed
        preparation = until_preparation(runner) if boundary in {'refresh', 'admit', 'charge'} else None
        if boundary == 'charge':
            call = kernel.admit_agent(preparation).call
            assert call is not None
            assert kernel.dispatch_agent(call)
            # Explicit external input for this Core/commit test, never OS exit proof.
            process = {'pid': 4242, 'start_token': 'synthetic', 'pgid': 4242,
                'host_pid': 4241, 'host_start_token': 'synthetic-host'}
            kernel.agent_started(call, process)
            runner.authorize_agent(call)
            original_leases = rows(db, 'leases')
        if boundary == 'refresh':
            # A later real observation must retain work and gate changed scope.
            source = replace(source, issues=(replace(source.issues[0], body='Changed ticket'),))
            ports.requirements = source
        if boundary == 'consumed':
            assert runner.run_once().kind == 'progress'  # receipt, not consumption
        dispatched_before = tuple(ports.dispatched)
        with monkeypatch.context() as patch:
            committed = arm_fault(patch, kernel, db, boundary)
            with pytest.raises(CommittedReplyLost, match=boundary):
                if boundary == 'refresh':
                    kernel.refresh(lambda: source)
                elif boundary == 'admit':
                    kernel.admit_agent(preparation)
                elif boundary == 'charge':
                    kernel.agent_source_ready(call)
                else:
                    runner.run_once(agent_capacity=0 if boundary == 'ready' else 1)
            assert len(committed) == 1
        assert snapshot(db, 'other-run') == sibling
        if boundary == 'preparation':
            with kernel.storage.transaction(write=False) as tx:
                preparation, _ = kernel._preparation(tx,
                    tx.one("SELECT * FROM waits WHERE run_id='A' AND kind='agent_preparation'"))

    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        db.execute('SELECT 1')
    with opened(path) as (reopened, kernel):
        kernel.clock = lambda: 110.
        assert reopened is not db
        assert snapshot(reopened, 'A') == committed[0]
        assert rows(reopened, 'task_runs') == binding
        if boundary != 'charge':
            assert rows(reopened, 'budgets') == budgets
        runner = OperationRunner(kernel, execution, lambda _: ports)
        status = kernel.status(SUBJECT)
        if boundary == 'charge':
            assert observed_call(kernel, preparation) == call
            assert status['budgets']['requirements']['developments_used'] == 1
            assert reopened.execute("SELECT accounted FROM attempts WHERE run_id='A'").fetchone()[0] == 1
            output = reopened.execute("SELECT dispatch_state,raw_id FROM invocation_outputs WHERE run_id='A'").fetchone()
            assert output[:] == ('started', None)
            assert rows(reopened, 'leases') == original_leases
            before = snapshot(reopened, 'A')
            assert kernel.dispatch_agent(call) is False
            assert allocate_again(kernel, runner).reason == 'previous_invocation_exit'
            assert snapshot(reopened, 'A') == before
            # Even an explicit resume cannot discard a still-unsettled original Worker.
            with pytest.raises(KernelError, match='worker_exit_unproven'):
                kernel.control('resume', 'cannot-replay-unknown-release', subject=SUBJECT)
            kernel.agent_exited(call, process, -15, failure='release_acknowledgement_unknown')
            assert kernel.worker_settled(call)
            assert kernel.status(SUBJECT)['gates'] == []
            paused = runner.run_once()
            assert (paused.kind, paused.reason) == ('paused', 'execution_recovery_unavailable')
            kernel.control('resume', 'resume-after-controlled-exit', subject=SUBJECT)
            resumed = until_preparation(runner)
            assert (resumed.attempt_id, resumed.thread_id) == (call.attempt_id, call.thread_id)
            resumed_call = kernel.admit_agent(resumed).call
            assert resumed_call.invocation_id != call.invocation_id
            assert kernel.dispatch_agent(resumed_call)
            kernel.agent_started(resumed_call, process)
            runner.authorize_agent(resumed_call)
            kernel.agent_source_ready(resumed_call)
            kernel.agent_source_ready(resumed_call)
            kernel.receive(resumed_call,
                b'{"result_kind":"development","summary":"controlled return","human_blockers":null}')
            kernel.agent_exited(resumed_call, process, 0)
            assert kernel.worker_settled(resumed_call)
            assert kernel.status(SUBJECT)['budgets']['requirements']['developments_used'] == 1
            assert len(rows(reopened, 'attempts')) == 1 and len(rows(reopened, 'invocations')) == 2
            assert not rows(reopened, 'leases')
            assert snapshot(reopened, 'other-run') == sibling
            assert tuple(ports.dispatched) == dispatched_before
            assert reopened.execute('PRAGMA foreign_key_check').fetchall() == []
            assert reopened.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            return
        elif boundary == 'refresh':
            assert status['gates'] == ['requeue_required']
            assert reopened.execute("SELECT current_attempt FROM subjects WHERE run_id='A' AND id=?",
                (SUBJECT,)).fetchone()[0] == preparation.attempt_id
            before = snapshot(reopened, 'A')
            kernel.refresh(lambda: source)
            assert kernel.operation_state().cancel == (preparation.reservation_id,)
            assert snapshot(reopened, 'A') == before
        elif boundary == 'ready':
            queue = rows(reopened, 'work_queue')
            assert len(queue) == 1
            before = snapshot(reopened, 'A')
            assert allocate_again(kernel, runner, allow_agent=False).kind == 'needs_agent'
            assert snapshot(reopened, 'A') == before
            assert rows(reopened, 'work_queue') == queue
            preparation = until_preparation(runner)
            assert not rows(reopened, 'work_queue')
        elif boundary == 'preparation':
            with kernel.storage.transaction(write=False) as tx:
                wait = tx.one("SELECT * FROM waits WHERE run_id='A' AND kind='agent_preparation'")
                recovered, _ = kernel._preparation(tx, wait)
            assert recovered == preparation
            assert preparation.preparation_deadline == 700.
            assert kernel.operation_state().preparing == (preparation.reservation_id,)
            before = snapshot(reopened, 'A')
            assert allocate_again(kernel, runner).reason == 'previous_invocation_exit'
            assert snapshot(reopened, 'A') == before
        elif boundary == 'admit':
            call = observed_call(kernel, preparation)
            assert (call.attempt_id, call.thread_id, call.lease_id, call.context) == (
                preparation.attempt_id, preparation.thread_id, preparation.reservation_id, preparation.context)
            assert call.deadline_at == 220.
            assert kernel.operation_state().active == (call.lease_id,)
            assert kernel.operation_state().preparing == ()
            assert reopened.execute("SELECT dispatch_state,process_identity,raw_id,started_at "
                "FROM invocation_outputs WHERE run_id='A'").fetchone()[:] == ('allocated', None, None, None)
            assert reopened.execute("SELECT backend_id FROM threads WHERE run_id='A'").fetchone()[0] is None
            before = snapshot(reopened, 'A')
            with pytest.raises(KernelError, match='preparation_binding'):
                kernel.admit_agent(preparation)
            assert allocate_again(kernel, runner).reason == 'previous_invocation_exit'
            assert snapshot(reopened, 'A') == before
        else:
            effect = ports.dispatched[-1]
            row = reopened.execute("SELECT * FROM effects WHERE run_id='A' AND id=?", (effect.effect_id,)).fetchone()
            assert row['status'] == ('observed' if boundary == 'receipt' else 'confirmed')
            assert row['attempts'] == 1 and row['receipt_id'] is not None
            assert row['before_sha'] == effect.before_sha and row['base_sha'] == effect.base.sha
            with kernel.storage.transaction(write=False) as tx:
                assert tx.get(row['payload_id'], SUBJECT) == wire_json(effect.payload)
                receipt = tx.get(row['receipt_id'], SUBJECT)
            assert not rows(reopened, 'attempts') and not rows(reopened, 'waits')
            assert status['phase'] == ('development' if boundary == 'consumed' and operation == 'workspace' else 'workspace')
            preparation = until_preparation(runner)
            after = reopened.execute("SELECT * FROM effects WHERE run_id='A' AND id=?", (effect.effect_id,)).fetchone()
            assert after['status'] == 'confirmed'
            assert {key: after[key] for key in row.keys() if key != 'status'} == {
                key: row[key] for key in row.keys() if key != 'status'}
            with kernel.storage.transaction(write=False) as tx:
                assert tx.get(after['payload_id'], SUBJECT) == wire_json(effect.payload)
                assert tx.get(after['receipt_id'], SUBJECT) == receipt
            for kind in ('effect_prepared', 'effect_dispatched', 'effect_observed', 'effect_consumed'):
                assert reopened.execute('SELECT count(*) FROM history WHERE run_id=? AND kind=? AND identity=?',
                    ('A', kind, effect.effect_id)).fetchone()[0] == 1
            assert [item.effect_id for item in ports.dispatched].count(effect.effect_id) == 1

        assert rows(reopened, 'budgets') == budgets
        assert snapshot(reopened, 'other-run') == sibling
        assert reopened.execute('PRAGMA foreign_key_check').fetchall() == []
        assert reopened.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert reopened.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert reopened.execute("SELECT count(*) FROM attempts WHERE run_id='A'").fetchone()[0] == 1
        assert reopened.execute("SELECT accounted FROM attempts WHERE run_id='A'").fetchone()[0] == 0
        assert len(rows(reopened, 'invocations')) == (1 if boundary == 'admit' else 0)
        assert len(rows(reopened, 'invocation_outputs')) == (1 if boundary == 'admit' else 0)
        assert len(rows(reopened, 'leases')) == (1 if boundary == 'admit' else 0)
        assert not rows(reopened, 'worker_activity') and not rows(reopened, 'hosts')
        if boundary not in {'receipt', 'consumed'}:
            assert tuple(ports.dispatched) == dispatched_before
        assert [item.operation for item in ports.dispatched] == (
            ['run_branch', 'workspace'] if operation == 'run_branch' else ['workspace'])


def test_branch_intent_reopens_before_any_physical_git_write(tmp_path, monkeypatch):
    """Real local Git; Core transaction seam, not native save/Host evidence."""
    from agent_run.model import Issue, ParentIssue, Requirements
    from support.offline import OfflinePort

    port = OfflinePort.create(tmp_path / 'external')
    spec, delivery = port.prepare(DeliveryKind.TICKET)
    port.git.run('update-ref', '-d', delivery.source_ref, delivery.base.sha)
    source = Requirements(ParentIssue(1, 'Parent', 'Original requirement', (2,)), (
        Issue(2, 'Ticket', 'Original ticket', 'OPEN', frozenset({'ready-for-agent'})),))
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unobserved', delivery.base,
        delivery.target_ref, 'refs/heads/main')
    spec = replace(spec, run_id='A', deliveries=(delivery, final))
    path = tmp_path / 'repository.sqlite3'
    create_kernel(path, spec, requirements=source)
    physical_calls = []

    class ActualWorkspace(ReadOnlyPorts):
        def delivery(self, request):
            return port.delivery(request, ('none',), [],
                publisher_identity=spec.inputs.runner_identity)

        def execute(self, effect, authorize):
            assert effect.operation == 'workspace'
            physical_calls.append(effect)
            return port.execute(effect, authorize)

        def reconcile(self, effect):
            return port.reconcile(effect)

        def inspect(self, resources):
            return port.inspect_resources(resources)

    ports = ActualWorkspace(source, delivery.source_ref, delivery.target_ref,
        DeliveryFacts(None, delivery.base))
    with opened(path) as (db, kernel):
        execution = kernel.claim_executor('branch-owner')
        runner = OperationRunner(kernel, execution, lambda _: ports)
        with monkeypatch.context() as fault:
            committed = arm_fault(fault, kernel, db, 'intent')
            with pytest.raises(CommittedReplyLost, match='intent'):
                runner.run_once()
        assert len(committed) == 1
        original = dict(db.execute("SELECT * FROM effects WHERE run_id='A'").fetchone())
        assert original['operation'] == 'workspace' and original['status'] == 'prepared'
        assert original['attempts'] == 0 and original['receipt_id'] is None
        assert physical_calls == [] and port.writes() == []
        assert not port.git.run('for-each-ref', '--format=%(refname)', delivery.source_ref).stdout
        assert not Path(delivery.checkout).exists()

    with opened(path) as (db, kernel):
        assert snapshot(db, 'A') == committed[0]
        runner = OperationRunner(kernel, execution, lambda _: ports)
        assert runner.run_once().kind == 'progress'
        assert len(physical_calls) == 1
        effect = physical_calls[0]
        assert (effect.effect_id, effect.before_sha, effect.source_ref, effect.target_ref) == (
            original['id'], original['before_sha'], original['source_ref'], original['target_ref'])
        assert port.git.commit(effect.source_ref) == effect.base
        assert port.git.run('rev-parse', 'HEAD', cwd=Path(delivery.checkout)).stdout.strip() == effect.base.sha
        assert runner.run_once().kind == 'progress'
        until_preparation(runner)
        after = db.execute("SELECT * FROM effects WHERE run_id='A' AND id=?", (effect.effect_id,)).fetchone()
        assert after['status'] == 'confirmed' and after['attempts'] == 1
        assert after['payload_id'] == original['payload_id'] and after['receipt_id'] is not None
        assert len(physical_calls) == 1
        for kind in ('effect_prepared', 'effect_dispatched', 'effect_observed', 'effect_consumed'):
            assert db.execute("SELECT count(*) FROM history WHERE run_id='A' AND kind=? AND identity=?",
                (kind, effect.effect_id)).fetchone()[0] == 1
