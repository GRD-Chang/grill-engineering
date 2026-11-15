"""Default Parent-only budget decisions, without a physical Host/Git story.

External Git receipts and process observations are explicit synthetic inputs;
normal Core preparation, admission, Go, feedback and accounting write all facts.
"""
from contextlib import closing, contextmanager
import sqlite3

import pytest

from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec, wire_json
from support.core_operations import CoreOperations, native_agent_fixture
from support.review_facts import ReviewFacts
from support.offline import review_result
from support.run_scoped_kernel import ScopedKernel


SUBJECT = 'parent:1'
BASE = Commit('a' * 40, 'b' * 40)


@contextmanager
def opened(path, run):
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, run)
        kernel.storage.path, kernel.storage.run_id = path, run
        yield db, kernel


def budgets(db, run):
    return [tuple(row) for row in db.execute(
        'SELECT * FROM budgets WHERE run_id=? ORDER BY purpose,window', (run,))]


def usage(kernel):
    budget = kernel.status(SUBJECT)['budgets']['requirements']
    assert (budget['window'], budget['development_limit'], budget['review_limit']) == (1, 10, 10)
    assert budget['review_required'] is True
    return budget['developments_used'], budget['reviews_used']


@pytest.mark.parametrize('last_review', ['pass', 'fail'])
def test_default_parent_tenth_review_controls_publication_without_an_eleventh_development(tmp_path, last_review):
    path = tmp_path / 'budget.sqlite3'
    # No Policy override: the normal producer must freeze the actual defaults.
    spec = RunSpec('budget', 'example/repository', 1, 'unused', 'unused', (
        Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, 1, 'unused', BASE,
            'refs/heads/parent', 'refs/heads/main', checkout=str(tmp_path / 'checkout')),))
    _, original = native_agent_fixture(path, spec)
    ports = ReviewFacts(original)
    execution, retained = None, None
    calls = []
    for first, last in ((1, 9), (10, 10)):
        # The second segment must continue the original file/Run/window.
        with opened(path, spec.run_id) as (db, kernel):
            if execution is None:
                execution = kernel.claim_executor('budget-owner')
            else:
                assert budgets(db, spec.run_id) == retained
                assert usage(kernel) == (9, 9)
            operations = CoreOperations(kernel, execution, lambda _: ports)
            for ordinal in range(first, last + 1):
                for role in ('development', 'review'):
                    expected = (ordinal - 1, ordinal - 1) if role == 'development' else (ordinal, ordinal - 1)
                    assert usage(kernel) == expected
                    call = operations.until(lambda step: step.call is not None, limit=16).call
                    assert call is not None and call.role == role and call.purpose == 'requirements'
                    assert call.context['generation'] == 1 and usage(kernel) == expected
                    calls.append(call)
                    # These are synthetic process observations, not OS exit proof.
                    process = {'pid': 23, 'pgid': 23, 'start_token': 'synthetic-' + call.invocation_id,
                        'host_pid': 11, 'host_start_token': 'synthetic-host'}
                    assert kernel.dispatch_agent(call)
                    kernel.agent_started(call, process)
                    operations.runner.authorize_agent(call)
                    kernel.agent_source_ready(call)
                    assert usage(kernel) == (ordinal, ordinal - 1)
                    raw = wire_json({'result_kind': 'development', 'summary': f'Candidate {ordinal}',
                        'human_blockers': None}) if role == 'development' else wire_json(
                            review_result(last_review if ordinal == 10 else 'fail'))
                    kernel.receive(call, raw)
                    kernel.agent_exited(call, process, 0)
                    assert usage(kernel) == (ordinal, ordinal - 1)
                    assert operations.once(agent_capacity=0).kind == 'progress'
                    assert usage(kernel) == ((ordinal, ordinal - 1) if role == 'development' else (ordinal, ordinal))
                    with kernel.storage.transaction(write=False) as tx:
                        attempt = tx.one('SELECT accounted,result_id,status FROM attempts WHERE run_id=? AND id=?',
                            (spec.run_id, call.attempt_id))
                        assert attempt['accounted'] == 1 and attempt['status'] == 'consumed'
                        assert tx.get(attempt['result_id'], SUBJECT) == raw
            retained = budgets(db, spec.run_id)

    with opened(path, spec.run_id) as (db, kernel):
        assert budgets(db, spec.run_id) == retained and usage(kernel) == (10, 10)
        operations = CoreOperations(kernel, execution, lambda _: ports)
        boundary = operations.until(lambda step: step.kind == 'paused' or kernel.status(SUBJECT)['phase'] == 'publish', limit=16)
        state = kernel.status(SUBJECT)
        if last_review == 'pass':
            assert boundary.kind == 'progress' and state['phase'] == 'publish'
            assert state['gates'] == [] and state['gate_mode'] == 'review'
        else:
            assert boundary.kind == 'paused' and state['phase'] == 'development'
            assert state['gates'] == ['review_budget_exhausted']
            assert state['gate_mode'] != 'deterministic_fallback'
            scheduling = operations.runner.state()
            assert scheduling.disposition == 'quiescent'
            assert scheduling.active == scheduling.preparing == ()
        assert budgets(db, spec.run_id) == retained and usage(kernel) == (10, 10)
        assert db.execute('SELECT sum(developments_used),sum(reviews_used),count(*),sum(current) FROM budgets '
            "WHERE run_id=? AND subject=? AND purpose='requirements'", (spec.run_id, SUBJECT)).fetchone()[:] == (10, 10, 1, 1)
        assert [tuple(row) for row in db.execute(
            'SELECT role,count(*),sum(accounted) FROM attempts WHERE run_id=? GROUP BY role ORDER BY role',
            (spec.run_id,))] == [('development', 10, 10), ('review', 10, 10)]
        assert db.execute('SELECT count(*) FROM invocations WHERE run_id=?', (spec.run_id,)).fetchone()[0] == 20
        assert not db.execute('SELECT id FROM leases WHERE run_id=?', (spec.run_id,)).fetchall()
        assert len({call.attempt_id for call in calls}) == len({call.invocation_id for call in calls}) == 20
        assert {row[0] for row in db.execute('SELECT id FROM attempts WHERE run_id=?', (spec.run_id,))} == {
            call.attempt_id for call in calls}
        assert ports.candidates == 10
        settled = state

    with opened(path, spec.run_id) as (db, kernel):
        assert kernel.status(SUBJECT) == settled
        assert budgets(db, spec.run_id) == retained and usage(kernel) == (10, 10)
        if last_review == 'pass':
            publication = CoreOperations(kernel, execution, lambda _: ports, prepare_agents=False).once().preparation
            assert publication is not None and publication.role == 'publication'
            kernel.guard_preparation(publication)
            assert budgets(db, spec.run_id) == retained and usage(kernel) == (10, 10)
            assert db.execute('SELECT count(*) FROM invocations WHERE run_id=?', (spec.run_id,)).fetchone()[0] == 20
