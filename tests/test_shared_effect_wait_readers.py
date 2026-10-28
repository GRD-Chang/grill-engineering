"""Consumer selection over the existing pure Core/SQLite Effect fixture.

Relational prerequisites are explicit. Core prepares intents, observes controlled
results and persists wait scopes; no native Storage clock, adapter, Host, Worker
or physical receipt is replaced or exercised. Original native callers remain.
"""
from types import SimpleNamespace

import pytest

from agent_run.core.operation_contract import Work
from agent_run.errors import KernelError
from agent_run.model import Commit, EffectResult, Execution, World
from support.core_operations import _window
from support.effect_boundary import _local_marker, _merge_record, effect_state, state_of
from support.run_scoped_kernel import effect_runs


SUBJECT = 'parent:1'
BASE = Commit('a' * 40, 'b' * 40)
WORLD = World('r', 'g', 'r', BASE, BASE, publisher_actor='controlled-actor',
    publisher_identity={'source': 'host'})


@pytest.fixture
def effects(monkeypatch):
    with effect_runs(monkeypatch) as value:
        yield value


def scenario(kernel):
    return SimpleNamespace(kernel=kernel, delivery=SimpleNamespace(key=SUBJECT))


def prepare(kernel, run, operation='workspace'):
    work = Work(Execution(run, 'same-owner', 1), 'pending', SUBJECT, False, True, None, None)
    with kernel.storage.transaction() as tx:
        header, job = kernel._owned(tx, SUBJECT, work.execution)
        return kernel._prepare_effect(tx, header, job, work, WORLD, operation,
            before=BASE.sha, payload={'original_run': run})


def consume_target(kernel, request):
    assert kernel._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    assert kernel._reconcile(request, EffectResult('target', commit=BASE)).kind == 'progress'
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, SUBJECT, request.work.execution)
        row = tx.one('SELECT * FROM effects WHERE run_id=? AND id=?',
            (request.work.execution.run_id, request.effect.effect_id))
        assert kernel._apply_effect(tx, run, job, row, WORLD).kind == 'progress'


@pytest.mark.parametrize('reader', [state_of, effect_state], ids=['activation', 'cleanup'])
@pytest.mark.parametrize('selected', ['A', 'B'])
def test_effect_state_readers_keep_same_id_within_original_run(effects, reader, selected):
    db, kernels = effects
    requests = {run: prepare(kernels[run], run) for run in ('A', 'B')}
    a = requests['A']
    consume_target(kernels['A'], a)
    before = tuple(db.iterdump())
    identity = requests[selected].effect
    value = reader(scenario(kernels[selected]), {'id': identity.effect_id} if reader is state_of else identity)
    expected = ('confirmed', 1) if selected == 'A' else ('prepared', 0)
    assert value[:2] == expected
    if reader is effect_state:
        assert value[2] == db.execute('SELECT payload_id FROM effects WHERE run_id=? AND id=?',
            (selected, identity.effect_id)).fetchone()[0]
    assert tuple(db.iterdump()) == before


def test_effect_reader_rejects_foreign_callback_even_when_id_exists_locally(effects):
    db, kernels = effects
    prepare(kernels['A'], 'A')
    foreign = prepare(kernels['B'], 'B').effect
    before = tuple(db.iterdump())
    with pytest.raises(KernelError, match='effect_binding'):
        effect_state(scenario(kernels['A']), foreign)
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('kind', ['activation', 'cleanup', 'workspace', 'merge', 'wait'])
def test_missing_local_fact_is_not_satisfied_by_unique_sibling_fact(effects, kind):
    db, kernels = effects
    if kind == 'wait':
        with kernels['B'].storage.transaction() as tx:
            run, job = kernels['B']._owned(tx, SUBJECT, Execution('B', 'same-owner', 1))
            assert kernels['B']._wait(tx, run, job, 'github_read', 60).kind == 'waiting'
    else:
        request = prepare(kernels['B'], 'B', 'merge' if kind == 'merge' else 'workspace')
        if kind == 'workspace':
            consume_target(kernels['B'], request)
    before = tuple(db.iterdump())
    expected = 'effect_binding' if kind == 'cleanup' else 'missing_or_ambiguous_fact'
    with pytest.raises(KernelError, match=expected):
        selected = scenario(kernels['A'])
        if kind == 'activation':
            state_of(selected, {'id': request.effect.effect_id})
        elif kind == 'cleanup':
            effect_state(selected, request.effect)
        elif kind == 'workspace':
            _local_marker(selected)
        elif kind == 'merge':
            _merge_record(selected)
        else:
            _window(selected)
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('selected', ['A', 'B'])
def test_merge_reader_cannot_pair_a_sibling_latest_intent_with_original_material(effects, selected):
    db, kernels = effects
    for run in ('A', 'B'):
        prepare(kernels[run], run, 'merge')
    before = tuple(db.iterdump())
    row, payload, receipt = _merge_record(scenario(kernels[selected]))
    assert row['run_id'] == selected and row['operation'] == 'merge'
    assert payload['original_run'] == selected and row['status'] == 'prepared'
    assert receipt is None  # No physical merge or receipt is invented.
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('selected', ['A', 'B'])
def test_workspace_marker_join_never_multiplies_same_subject_from_sibling_run(effects, selected):
    db, kernels = effects
    for run in ('A', 'B'):
        request = prepare(kernels[run], run)
        consume_target(kernels[run], request)
    before = tuple(db.iterdump())
    assert _local_marker(scenario(kernels[selected])) == BASE.sha
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('selected', ['A', 'B'])
def test_read_window_keeps_its_original_run_start_and_absolute_deadline(effects, selected):
    db, kernels = effects
    for run, now in (('A', 100.25), ('B', 700.25)):
        kernel = kernels[run]
        kernel.clock = lambda now=now: now
        with kernel.storage.transaction() as tx:
            header, job = kernel._owned(tx, SUBJECT, Execution(run, 'same-owner', 1))
            assert kernel._wait(tx, header, job, 'github_read', 60).kind == 'waiting'
    before = tuple(db.iterdump())
    start = 100.25 if selected == 'A' else 700.25
    assert _window(scenario(kernels[selected])) == {'started_at': start, 'deadline': start + 60}
    assert tuple(db.iterdump()) == before
