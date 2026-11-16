"""Post-failure SQL/note contracts; no native Host, Worker or SaveClock proof."""
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from agent_run.storage.sqlite import QUERY_LIMIT
from support import systemd_manager
from support.repository_scope import add_subject, connection, insert, seed
from support.run_scoped_kernel import ScopedKernel

RUN, OTHER = 'a' * 32, 'b' * 32
PREFIX = 'Commit Resume stage timeline: '


@pytest.fixture
def timeline(monkeypatch, request):
    db = connection()
    request.addfinalizer(db.close)
    db.execute('BEGIN')  # The real schema defers the Run/profile reference cycle.
    kernels = {}
    for run in (RUN, OTHER):
        seed(db, run)
        for subject in ('ticket:2', 'ticket:3', 'final:1'):
            add_subject(db, run, subject)
        for number, (subject, role, start, end) in enumerate((
                ('ticket:3', 'review', 100., 101.),
                ('ticket:3', 'publication', 128., 129.),
                ('final:1', 'review', 156., 157.)), 1):
            attempt, invocation, thread = (f'{base + number:032x}' for base in (100, 200, 300))
            insert(db, 'threads', dict(run_id=run, id=thread, subject=subject, role=role, profile_revision=1))
            insert(db, 'attempts', dict(run_id=run, id=attempt, subject=subject, generation=1, ordinal=number,
                role=role, purpose='integration', window=1, parent_revision='r', graph_revision='g',
                revision='r', base_sha='c' * 40, completion_revision='original', context_id='profile', status='consumed'))
            insert(db, 'invocations', dict(run_id=run, id=invocation, attempt=attempt, thread=thread,
                executor='owner', epoch=1, status='returned', allocated_at=99., deadline_seconds=60., deadline_at=159.))
            insert(db, 'invocation_outputs', dict(run_id=run, invocation=invocation, ordinal=0,
                context_id='profile', lease=run + invocation, dispatch_state='exited', allocated_at=99.,
                started_at=start, returned_at=end, mode='new'))
        insert(db, 'effects', dict(run_id=run, id='e' * 32, subject='ticket:3',
            operation='integrate' if run == RUN else 'close', status='confirmed', parent_revision='r',
            graph_revision='g', revision='r', generation=1, source_ref='refs/heads/private',
            target_ref='refs/heads/main', before_sha='c' * 40, base_sha='c' * 40,
            base_tree='d' * 40, payload_id='profile'))
        for index, kind in enumerate(('effect_prepared', 'effect_dispatched', 'effect_observed', 'effect_consumed')):
            insert(db, 'history', dict(run_id=run, subject='ticket:3', kind=kind, identity='e' * 32,
                generation=1, created_at=102. + index, phase='integration'))
        # The role event deliberately has the same ID as an Effect: never map it as one.
        insert(db, 'history', dict(run_id=run, subject='ticket:3', kind='role_consumed', identity='e' * 32,
            generation=1, created_at=130., phase='publication_commit'))
        kernels[run] = ScopedKernel(db, run)
    db.commit()
    monkeypatch.setattr(systemd_manager, 'Kernel', lambda _path, *, run_id: kernels[run_id])
    return db, kernels[RUN]


def observe():
    error = subprocess.TimeoutExpired('original public Resume', 82.5)
    systemd_manager._commit_resume_timeline_note(error, Path('/unused/private-database'), RUN)
    note, = error.__notes__
    assert len(note.encode()) <= systemd_manager.MANAGED_NOTE_BYTES
    assert note.startswith(PREFIX)
    return json.loads(note[len(PREFIX):])


def test_exact_snapshot_has_times_effect_mapping_and_no_writes_or_private_bodies(timeline):
    db, kernel = timeline
    db.execute("UPDATE material SET body=?,size=?", (b'PRIVATE_BODY_MUST_NOT_BE_READ', 29))
    before = tuple(db.iterdump())
    statements = []
    db.set_trace_callback(statements.append)
    db.set_authorizer(lambda action, table, column, *_:
        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'material' and column == 'body'
        else sqlite3.SQLITE_OK)
    facts = observe()
    db.set_authorizer(None)
    assert tuple(db.iterdump()) == before
    assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP')) for s in statements)
    assert facts['run_id'] == RUN and facts['windows'] == [[101., 128.], [129., 156.]]
    assert len(facts['anchors']) == 3 and len(facts['rows']) == 5
    assert facts['window_unavailable'] == [None, None]
    assert [row[1] for row in facts['rows']] == [102., 103., 104., 105., 130.]
    assert all(row[-1] == 'integrate' for row in facts['rows'][:4])
    assert facts['rows'][-1][-1] is None
    assert not facts['partial_at_row_limit']
    assert OTHER not in json.dumps(facts) and 'PRIVATE_BODY' not in json.dumps(facts)
    assert '/unused/' not in json.dumps(facts) and 'refs/heads/' not in json.dumps(facts)
    assert kernel.owners[-1].rows_read > 3  # Original cumulative Transaction accounting remains active.


@pytest.mark.parametrize('count', [127, 128, 140])
def test_history_bound_is_ordered_and_at_limit_is_conservatively_partial(timeline, count):
    db, _ = timeline
    db.execute('DELETE FROM history WHERE run_id=?', (RUN,))
    for _ in range(count):
        insert(db, 'history', dict(run_id=RUN, subject='ticket:3', kind='effect_prepared', identity='e' * 32,
            generation=1, created_at=103., phase='integration'))
    facts = observe()
    assert len(facts['rows']) == min(count, 128)
    assert [row[0] for row in facts['rows']] == sorted(row[0] for row in facts['rows'])
    assert facts['partial_at_row_limit'] is (count >= 128)


@pytest.mark.parametrize('updates,windows,times,unavailable', [
    ([(3, 'returned_at', None)], [[101., 128.], [129., 156.]], [102., 103., 104., 105., 130.], [None, None]),
    ([(1, 'started_at', None)], [[101., 128.], [129., 156.]], [102., 103., 104., 105., 130.], [None, None]),
    ([(1, 'returned_at', None)], [None, [129., 156.]], [130.], ['missing_endpoint', None]),
    ([(2, 'started_at', None)], [None, [129., 156.]], [130.], ['missing_endpoint', None]),
    ([(2, 'returned_at', None)], [[101., 128.], None], [102., 103., 104., 105.], [None, 'missing_endpoint']),
    ([(3, 'started_at', None)], [[101., 128.], None], [102., 103., 104., 105.], [None, 'missing_endpoint']),
    ([(2, 'started_at', 99.)], [None, [129., 156.]], [130.], ['incomparable_endpoints', None]),
    ([(3, 'started_at', 126.)], [[101., 128.], None], [102., 103., 104., 105.], [None, 'incomparable_endpoints']),
    ([(1, 'returned_at', None), (3, 'started_at', None)], [None, None], [], ['missing_endpoint', 'missing_endpoint']),
])
def test_windows_need_only_their_own_endpoints_and_keep_other_available_facts(timeline, updates, windows, times, unavailable):
    db, _ = timeline
    for number, column, value in updates:
        assert column in ('started_at', 'returned_at')
        db.execute(f'UPDATE invocation_outputs SET {column}=? WHERE run_id=? AND invocation=?',
                   (value, RUN, f'{200 + number:032x}'))
    facts = observe()
    assert facts['window_labels'] == ['review_return_to_publication_start', 'publication_return_to_final_review_start']
    assert facts['windows'] == windows and facts['window_unavailable'] == unavailable
    assert [row[1] for row in facts['rows']] == times
    assert len(facts['anchors']) == 3 and not facts['partial_at_row_limit']
    for number, column, value in updates:
        anchor, = [row for row in facts['anchors'] if row['invocation'] == f'{200 + number:032x}']
        assert anchor[column] == value  # No invented return or end-of-window timestamp.


def test_running_final_review_without_a_return_keeps_both_completed_gaps(timeline):
    db, _ = timeline
    invocation, attempt = f'{203:032x}', f'{103:032x}'
    db.execute("UPDATE invocation_outputs SET dispatch_state='started',returned_at=NULL WHERE run_id=? AND invocation=?", (RUN, invocation))
    db.execute("UPDATE invocations SET status='running' WHERE run_id=? AND id=?", (RUN, invocation))
    db.execute("UPDATE attempts SET status='pending',current_invocation=? WHERE run_id=? AND id=?", (invocation, RUN, attempt))
    facts = observe()
    assert facts['windows'] == [[101., 128.], [129., 156.]]
    assert facts['window_unavailable'] == [None, None] and len(facts['rows']) == 5
    final, = [row for row in facts['anchors'] if row['subject'] == 'final:1']
    assert final['started_at'] == 156. and final['returned_at'] is None


@pytest.mark.parametrize('number,windows,times', [
    (1, [None, [129., 156.]], [130.]),
    (2, [None, None], []),
    (3, [[101., 128.], None], [102., 103., 104., 105.]),
    (None, [None, None], []),
])
def test_absent_role_anchor_does_not_discard_an_independent_window(timeline, number, windows, times):
    db, _ = timeline
    if number is None:
        db.execute('DELETE FROM invocation_outputs WHERE run_id=?', (RUN,))
    else:
        db.execute('DELETE FROM invocation_outputs WHERE run_id=? AND invocation=?', (RUN, f'{200 + number:032x}'))
    facts = observe()
    assert facts['windows'] == windows and [row[1] for row in facts['rows']] == times
    assert facts['window_unavailable'] == ['missing_endpoint' if window is None else None for window in windows]
    assert len(facts['anchors']) == (0 if number is None else 2)


@pytest.mark.parametrize('fault', ['ambiguous', 'private_phase',
                                 'private_identity', 'private_operation', 'missing_effect',
                                 'wrong_effect_subject', 'wrong_generation'])
def test_missing_ambiguous_or_unsafe_facts_are_unavailable_not_inferred(timeline, fault):
    db, _ = timeline
    if fault == 'ambiguous':
        db.execute('UPDATE attempts SET role=\'review\' WHERE run_id=? AND role=\'publication\'', (RUN,))
    elif fault == 'private_phase':
        db.execute('UPDATE history SET phase=\'PRIVATE_SECRET_DETAIL\' WHERE run_id=?', (RUN,))
    elif fault == 'private_identity':
        db.execute('UPDATE history SET identity=\'PRIVATE_SECRET_DETAIL\' WHERE run_id=?', (RUN,))
    elif fault == 'private_operation':
        db.execute('UPDATE effects SET operation=\'PRIVATE_SECRET_DETAIL\' WHERE run_id=?', (RUN,))
    elif fault == 'missing_effect':
        db.execute('UPDATE history SET identity=? WHERE run_id=? AND kind!=\'role_consumed\'', ('f' * 32, RUN))
    elif fault == 'wrong_effect_subject':
        db.execute('UPDATE effects SET subject=\'ticket:2\' WHERE run_id=?', (RUN,))
    else:
        db.execute('UPDATE effects SET generation=2 WHERE run_id=?', (RUN,))
    assert observe() == {'unavailable': 'ValueError'}


@pytest.mark.parametrize('failure', [OSError, TimeoutError, InterruptedError, KeyboardInterrupt])
@pytest.mark.parametrize('stage', ['open', 'read', 'close', 'read-and-close'])
def test_diagnostic_failure_preserves_original_object_and_traceback(timeline, monkeypatch, failure, stage):
    _, kernel = timeline
    original_transaction, closes = kernel.storage.transaction, []

    @contextmanager
    def failed_close(**kwargs):
        assert kwargs == {'write': False}
        if stage == 'open':
            raise failure('PRIVATE_OPEN_FAILURE')
        try:
            with original_transaction(**kwargs) as tx:
                if stage in ('read', 'read-and-close'):
                    def failed_read(*args, **kwargs):
                        raise failure('PRIVATE_READ_FAILURE')
                    monkeypatch.setattr(tx, 'rows', failed_read)
                yield tx
        finally:
            closes.append(True)
            if stage in ('close', 'read-and-close'):
                raise failure('PRIVATE_CLOSE_FAILURE')

    monkeypatch.setattr(kernel.storage, 'transaction', failed_close)
    original = subprocess.TimeoutExpired('original Resume', 82.5)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        try:
            raise original
        except subprocess.TimeoutExpired as error:
            before = error.__traceback__
            systemd_manager._commit_resume_timeline_note(error, Path('/unused'), RUN)
            assert error.__traceback__ is before
            raise
    assert caught.value is original and closes == ([] if stage == 'open' else [True])
    assert json.loads(original.__notes__[0][len(PREFIX):]) == {'unavailable': failure.__name__}


def test_existing_read_budget_is_not_reset_or_retried(timeline, monkeypatch):
    _, kernel = timeline
    original_transaction = kernel.storage.transaction

    @contextmanager
    def exhausted(**kwargs):
        with original_transaction(**kwargs) as tx:
            tx.bytes_read = QUERY_LIMIT
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', exhausted)
    assert observe() == {'unavailable': 'KernelError'}
    assert len(kernel.owners) == 1


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_add_note_is_attempted_once_and_cannot_replace_the_failure(timeline, failure):
    class Original(subprocess.TimeoutExpired):
        calls = 0

        def add_note(self, note):
            self.calls += 1
            raise failure('PRIVATE_NOTE_FAILURE')

    original = Original('Resume', 82.5)
    systemd_manager._commit_resume_timeline_note(original, Path('/unused'), RUN)
    assert original.calls == 1 and not hasattr(original, '__notes__')


def test_byte_limit_and_serialization_failure_do_not_publish_unbounded_notes(timeline, monkeypatch):
    monkeypatch.setattr(systemd_manager, 'json', SimpleNamespace(dumps=lambda *a, **k: 'x' * 30000))
    assert observe() == {'unavailable': 'note_byte_limit'}

    def failed(*args, **kwargs):
        raise KeyboardInterrupt('PRIVATE_SERIALIZATION_FAILURE')

    monkeypatch.setattr(systemd_manager, 'json', SimpleNamespace(dumps=failed))
    original = subprocess.TimeoutExpired('Resume', 82.5)
    systemd_manager._commit_resume_timeline_note(original, Path('/unused'), RUN)
    assert not hasattr(original, '__notes__')


@pytest.mark.parametrize('observation_fault', [(None, None)] + [
    (stage, failure) for stage in ('costs', 'snapshot', 'serialize', 'note')
    for failure in (OSError, TimeoutError, KeyboardInterrupt)])
def test_actual_story_calls_export_only_after_resume_timeout(observation_fault):
    path = Path(__file__).with_name('test_kernel_managed_entry_end_to_end.py')
    module = ast.parse(path.read_text())
    story = next(n for n in module.body if isinstance(n, ast.FunctionDef) and
        n.name == 'test_controlled_native_commit_failure_reaps_both_workers_before_disarm_and_public_resume')
    selected = next(n for n in ast.walk(story) if isinstance(n, ast.Try) and
        any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'resumed' for t in s.targets) for s in n.body))
    program = compile(ast.fix_missing_locations(ast.Module(body=[selected], type_ignores=[])), str(path), 'exec')
    for failed in (False, True):
        calls, observed, public_calls = [], [], []
        stage, failure = observation_fault

        def observe_stage(name):
            observed.append(name)
            if stage == name:
                raise failure('PRIVATE_SECONDARY_FAILURE')

        class Original(subprocess.TimeoutExpired):
            def add_note(self, note):
                observe_stage('note')
                super().add_note(note)

        original = Original('Resume', 82.5)

        def snapshot(*args):
            observe_stage('snapshot')
            return {}

        def serialize(value):
            observe_stage('serialize')
            return json.dumps(value)

        def public(*args, **kwargs):
            public_calls.append(True)
            if failed:
                raise original
            return SimpleNamespace(returncode=0)

        scope = dict(json=SimpleNamespace(dumps=serialize), subprocess=SimpleNamespace(run=public, TimeoutExpired=subprocess.TimeoutExpired),
            sys=SimpleNamespace(executable='/unused/python'), spec=SimpleNamespace(run_id=RUN), git_repo=Path('/unused'),
            environment={}, remaining=lambda: 82.5, tmp_path=Path('/unused'), observations={},
            kernel=SimpleNamespace(storage=SimpleNamespace(path=Path('/unused/database'))),
            note_costs=lambda *a: observe_stage('costs'), _managed_diagnostics=snapshot,
            _commit_resume_timeline_note=lambda *args: calls.append(args))
        if failed:
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                exec(program, scope)
            assert caught.value is original and calls == [(original, Path('/unused/database'), RUN)]
            expected = ['costs', 'snapshot']
            if stage != 'snapshot':
                expected.append('serialize')
                if stage != 'serialize':
                    expected.append('note')
            assert observed == expected
        else:
            exec(program, scope)
            assert calls == observed == []
        assert public_calls == [True]
