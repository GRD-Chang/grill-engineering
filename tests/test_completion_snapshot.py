"""Observer predicate semantics, separate from native Core completion proof."""
from contextlib import closing
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage.sqlite import Transaction
from support.core_operations import task_run
from support.observer_errors import COMPLETION_SNAPSHOT_SQL, completion_snapshot


@pytest.mark.parametrize('phase', ['checks', 'complete'])
@pytest.mark.parametrize('rows,expected', [
    ([], False),
    ([('foreign', 'ticket:3', 1, 'current')], False),
    ([('wanted', 'ticket:4', 1, 'current')], False),
    ([('wanted', 'ticket:3', 0, 'old')], True),
])
def test_exact_run_subject_exists_keeps_original_any_generation_semantics(phase, rows, expected):
    # Independent relation fixtures only: no Run database or Core Completion
    # is created. The integration test still requires actual Core evidence.
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE subjects(run_id TEXT,id TEXT,phase TEXT)')
        db.execute('CREATE TABLE completions(run_id TEXT,subject TEXT,generation INTEGER,revision TEXT)')
        db.executemany('INSERT INTO subjects VALUES (?,?,?)', [
            ('wanted', 'ticket:3', phase), ('foreign', 'ticket:3', 'complete'),
            ('wanted', 'ticket:4', 'complete')])
        db.executemany('INSERT INTO completions VALUES (?,?,?,?)', rows)
        row = Transaction(db).one(COMPLETION_SNAPSHOT_SQL, ('wanted', 'ticket:3'))
        original_any = any(item['subject'] == 'ticket:3' for item in
            db.execute('SELECT * FROM completions WHERE run_id=?', ('wanted',)))
        assert row['phase'] == phase and bool(row['completed']) is expected
        assert bool(row['completed']) is original_any


def test_normal_scoped_snapshot_matches_full_views_with_less_read_work(tmp_path, monkeypatch):
    kernel, _ = task_run(tmp_path, 'original')
    connect = sqlite3.connect

    def measure(read):
        connections, statements = [], []

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            connections.append(db)
            db.set_trace_callback(statements.append)
            return db

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', traced)
            value = read()
        return value, len(connections), len(statements)

    def snapshot():
        with kernel.storage.transaction(write=False) as tx:
            return completion_snapshot(tx, 'parent')

    narrow, nc, ns = measure(snapshot)
    phase, pc, ps = measure(lambda: kernel.status('parent')['phase'])
    full, fc, fs = measure(lambda: (kernel.status('parent')['phase'],
        any(row['subject'] == 'parent' for row in kernel.source_status()['completions'])))
    assert narrow == full and narrow[0] == phase and narrow[1] is False
    assert nc == pc == 1 and fc >= 2 and ns < ps < fs
    print(f'snapshot connections={nc} SQL={ns}; status connections={pc} SQL={ps}; '
          f'full views connections={fc} SQL={fs}')
    with kernel.storage.transaction(write=False) as tx:
        with pytest.raises(KernelError, match='missing_or_ambiguous_fact'):
            completion_snapshot(tx, 'missing-subject')

    failure = OSError('Original read unavailable')
    with kernel.storage.transaction(write=False) as tx:
        def unavailable(*args, **kwargs):
            raise failure
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'one', unavailable)
            with pytest.raises(OSError) as caught:
                completion_snapshot(tx, 'parent')
            assert caught.value is failure


def test_unscoped_transaction_cannot_supply_completion_evidence():
    with closing(sqlite3.connect(':memory:')) as db:
        with pytest.raises(KernelError, match='run_scope_required'):
            completion_snapshot(Transaction(db), 'parent')
