"""Combined physical reads retain the real Core Source and Delivery boundaries.

The existing ScopedKernel owns real SQLite transactions. Physical packets and
post-commit exceptions are controlled inputs, not native storage/Host evidence.
"""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_run.core.operation_contract import DeliveryFacts, PreparedRequirements, ReadUnavailable
from agent_run.core.operation_runner import _read_observation
from agent_run.errors import KernelError
from agent_run.model import Commit, Execution, ParentIssue, Requirements, wire_json
from support.repository_scope import insert
from support.run_scoped_kernel import effect_runs


SUBJECT = 'parent:1'
BASE = Commit('a' * 40, 'b' * 40)


@pytest.fixture
def observation(monkeypatch):
    with effect_runs(monkeypatch) as (db, kernels):
        sources = {}
        for run, kernel in kernels.items():
            source = Requirements(ParentIssue(1, 'Parent ' + run, 'Original requirement'), ())
            sources[run] = source
            parent, graph = kernel._source_revisions(source)
            with kernel.storage.transaction() as tx:
                db.execute('UPDATE runs SET parent_revision=?,graph_revision=?,accepted_parent_revision=? WHERE id=?',
                           (parent, graph, parent, run))
                db.execute('UPDATE subjects SET revision=? WHERE run_id=?', (parent, run))
                kernel._refresh(tx, source, initial=True)
        yield db, kernels['A'], sources['A'], Execution('A', 'same-owner', 1)


def _ports(db, request, source, *, before_source=None, complete=None):
    """Only the two physical interfaces; neither callback supplies Core grants."""
    prepared = PreparedRequirements(source, object())
    calls = []

    def source_observation(parent, query):
        assert not db.in_transaction and parent == request.parent_number and query == request.query
        calls.append(('source', query))
        if before_source is not None:
            before_source()
        return prepared

    def complete_observation(query, returned):
        assert not db.in_transaction and returned is prepared
        calls.append(('delivery', query))
        return DeliveryFacts(BASE, BASE) if complete is None else complete(query)

    return SimpleNamespace(source_observation=source_observation,
        complete_observation=complete_observation), calls


def _approved_source_basis(db, kernel):
    """Persisted review/check/grant prerequisites for Source invalidation only."""
    with kernel.storage.transaction() as tx:
        run = tx.one("SELECT * FROM runs WHERE id='A'")
        context = tx.put(SUBJECT, 'role_context', wire_json({'review_context': {}, 'reviewed_commit': BASE.sha}))
        insert(db, 'attempts', {'run_id': 'A', 'id': 'review', 'subject': SUBJECT,
            'generation': 1, 'ordinal': 1, 'role': 'review', 'purpose': 'integration', 'window': 1,
            'parent_revision': run['parent_revision'], 'graph_revision': run['graph_revision'],
            'revision': run['parent_revision'], 'base_sha': BASE.sha,
            'candidate_sha': BASE.sha, 'candidate_tree': BASE.tree,
            'completion_revision': 'no-tickets', 'context_id': context, 'status': 'complete'})
        evidence = tx.put(SUBJECT, 'required_checks', wire_json({'original': 'checks'}))
        insert(db, 'checks', {'run_id': 'A', 'id': 'checks', 'subject': SUBJECT, 'pr_number': 17,
            'head_sha': BASE.sha, 'result': 'none', 'material_id': evidence})
        insert(db, 'actions', {'run_id': 'A', 'id': 'approval', 'kind': 'approve',
            'subject': SUBJECT, 'status': 'pending'})
        insert(db, 'grants', {'run_id': 'A', 'id': 'grant', 'subject': SUBJECT, 'action': 'approval',
            'pr_number': 17, 'head_sha': BASE.sha, 'base_sha': BASE.sha, 'review_attempt': 'review',
            'checks_id': 'checks', 'revision': run['parent_revision'], 'parent_revision': run['parent_revision'],
            'graph_revision': run['graph_revision'], 'completion_revision': 'no-tickets'})
        db.execute("UPDATE subjects SET kind='final_run',phase='merge',pr_number=17,review_attempt='review',"
                   "checks_id='checks',grant_id='grant' WHERE run_id='A'")


def test_combined_runner_accepts_source_before_using_prepared_delivery(observation):
    db, kernel, source, execution = observation
    request = kernel.begin_observation(SUBJECT, execution=execution)
    assert request.query is not None
    before = tuple(db.iterdump())
    ports, calls = _ports(db, request, source)
    world, fence = _read_observation(kernel, ports, request)
    assert world.source == world.target == BASE
    assert fence == request.fence
    assert calls == [('source', request.query), ('delivery', request.query)]
    assert tuple(db.iterdump()) == before


def test_legacy_synthetic_ports_keep_the_original_source_then_delivery_sequence(observation):
    db, kernel, source, execution = observation
    request = kernel.begin_observation(SUBJECT, execution=execution)
    calls = []

    def requirements(parent):
        assert not db.in_transaction and parent == request.parent_number
        calls.append('source')
        return source

    def delivery(query):
        assert not db.in_transaction and query == request.query
        calls.append('delivery')
        return DeliveryFacts(BASE, BASE)

    world, fence = _read_observation(kernel, SimpleNamespace(source=requirements, delivery=delivery),
                                     replace(request, query=None))
    assert world.source == BASE and fence == request.fence
    assert calls == ['source', 'delivery']


@pytest.mark.parametrize('failure', ['invalid_source', 'before_commit', 'unknown_ack'])
def test_original_source_acceptance_error_never_completes_delivery(observation, monkeypatch, failure):
    db, kernel, source, execution = observation
    request = kernel.begin_observation(SUBJECT, execution=execution)
    changed = replace(source, parent=replace(source.parent, body='Returned changed requirement',
        number=999 if failure == 'invalid_source' else source.parent.number))
    ports, calls = _ports(db, request, changed)
    before = tuple(db.iterdump())
    transaction, accept = kernel.storage.transaction, kernel.accept_requirements
    original, accepted_errors, returned = OSError('Original Source save failure'), [], []

    @contextmanager
    def failed_save(**options):
        write = options.get('write', True)
        with transaction(**options) as tx:
            yield tx
            if write and failure == 'before_commit':
                raise original
        if write and failure == 'unknown_ack':
            raise original

    def accept_source(*args):
        try:
            return accept(*args)
        except Exception as error:
            accepted_errors.append(error)
            raise

    monkeypatch.setattr(kernel.storage, 'transaction', failed_save)
    monkeypatch.setattr(kernel, 'accept_requirements', accept_source)
    with pytest.raises(KernelError if failure == 'invalid_source' else OSError) as caught:
        returned.append(_read_observation(kernel, ports, request))
    assert returned == [] and accepted_errors == [caught.value]
    assert accepted_errors[0] is caught.value
    assert calls == [('source', request.query)]
    if failure == 'invalid_source':
        assert caught.value.code == 'parent_identity'
    else:
        assert caught.value is original
    if failure == 'unknown_ack':
        # This is an injected exception after the real scoped COMMIT, not a
        # native save-clock/COMMIT acknowledgement-loss simulation.
        assert db.execute("SELECT source_sequence FROM runs WHERE id='A'").fetchone()[0] != request.fence.source_sequence
    else:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('failure', ['deadline', 'invalid'])
def test_delivery_failure_preserves_committed_source_and_revoked_grant(observation, failure):
    db, kernel, source, execution = observation
    _approved_source_basis(db, kernel)
    request = kernel.begin_observation(SUBJECT, execution=execution)
    assert request.query is not None
    changed = replace(source, parent=replace(source.parent, body='New authoritative Parent requirement'))
    budgets = tuple(map(tuple, db.execute('SELECT * FROM budgets ORDER BY run_id')))
    original = ReadUnavailable('github_read_failed' if failure == 'deadline' else 'github_invalid_response',
        'Original Delivery deadline' if failure == 'deadline' else 'Original invalid Delivery packet')
    completed = []

    def complete(query):
        assert query == request.query
        assert db.execute("SELECT grant_id,phase FROM subjects WHERE run_id='A'").fetchone()[:] == (None, 'refresh_final')
        completed.append(query)
        raise original

    ports, calls = _ports(db, request, changed, complete=complete)
    with pytest.raises(ReadUnavailable) as caught:
        _read_observation(kernel, ports, request)
    assert caught.value is original
    assert calls == [('source', request.query), ('delivery', completed[0])]
    assert db.execute("SELECT source_sequence FROM runs WHERE id='A'").fetchone()[0] != request.fence.source_sequence
    assert db.execute("SELECT blocked FROM runs WHERE id='A'").fetchone()[0] == 'final_refresh'
    assert db.execute("SELECT body FROM requirement_versions WHERE run_id='A' ORDER BY rowid DESC LIMIT 1").fetchone()[0] == changed.parent.body
    assert db.execute("SELECT count(*) FROM grants WHERE run_id='A'").fetchone()[0] == 1
    assert not db.execute('SELECT id FROM effects').fetchall()
    assert tuple(map(tuple, db.execute('SELECT * FROM budgets ORDER BY run_id'))) == budgets


def test_runner_completes_the_regenerated_query_after_source_changes_its_demand(observation):
    db, kernel, source, execution = observation
    _approved_source_basis(db, kernel)
    # Fixed checks-stage prerequisite with its prior grant already retired.
    db.execute("UPDATE subjects SET phase='checks',grant_id=NULL WHERE run_id='A'")
    request = kernel.begin_observation(SUBJECT, execution=execution)
    assert request.query is not None and request.query.check_head == BASE.sha
    changed = replace(source, parent=replace(source.parent, body='Changed Parent during checks'))

    def complete(query):
        assert query == replace(request.query, check_head=None)
        return DeliveryFacts(BASE, BASE)

    ports, calls = _ports(db, request, changed, complete=complete)
    world, fence = _read_observation(kernel, ports, request)
    assert calls == [('source', request.query), ('delivery', replace(request.query, check_head=None))]
    assert fence.source_sequence != request.fence.source_sequence
    assert world.parent_revision == db.execute("SELECT observed_parent_revision FROM runs WHERE id='A'").fetchone()[0]


@pytest.mark.parametrize('change', ['stop', 'epoch', 'source', 'generation'])
def test_returned_delivery_cannot_bypass_current_fence(observation, change):
    db, kernel, source, execution = observation
    request = kernel.begin_observation(SUBJECT, execution=execution)
    after_change = []

    def complete(query):
        if change == 'stop':
            kernel.control('stop', 'stop-during-combined-delivery')
        elif change == 'epoch':
            db.execute("UPDATE runs SET executor='successor',epoch=epoch+1 WHERE id='A'")
        elif change == 'source':
            kernel.refresh(lambda: replace(source, parent=replace(source.parent, body='Concurrent authoritative Source')))
        else:
            # Negative concurrent scope input; this does not produce a valid Requeue.
            db.execute("UPDATE subjects SET generation=generation+1 WHERE run_id='A'")
        after_change.append(tuple(db.iterdump()))
        return DeliveryFacts(BASE, BASE)

    ports, calls = _ports(db, request, source, complete=complete)
    code = 'executor_fenced' if change in {'stop', 'epoch'} else 'automatic_step_superseded'
    with pytest.raises(KernelError, match=code):
        _read_observation(kernel, ports, request)
    assert calls == [('source', request.query), ('delivery', request.query)]
    assert tuple(db.iterdump()) == after_change[0]


def test_source_superseded_during_combined_packet_does_not_complete_delivery(observation):
    db, kernel, source, execution = observation
    request = kernel.begin_observation(SUBJECT, execution=execution)
    changed = replace(source, parent=replace(source.parent, body='Newer accepted Source'))

    def during_source():
        kernel.refresh(lambda: changed)

    ports, calls = _ports(db, request, source, before_source=during_source)
    with pytest.raises(KernelError, match='automatic_step_superseded'):
        _read_observation(kernel, ports, request)
    assert calls == [('source', request.query)]
    assert db.execute("SELECT body FROM requirement_versions WHERE run_id='A' ORDER BY rowid DESC LIMIT 1").fetchone()[0] == changed.parent.body
