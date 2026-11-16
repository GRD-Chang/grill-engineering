"""Bounded native resource guards preserve complete evidence and old state."""
from contextlib import closing
from io import StringIO
import sqlite3

import pytest

import agent_run.core.kernel as core
import agent_run.storage.sqlite as storage
from agent_run.cli.render import render
from agent_run.errors import KernelError
from agent_run.queries.read_facts import QueryError
from agent_run.queries.run import QueryDocument
from support.core_operations import agent_preparation_fixture
from support.sqlite_faults import assert_save_rollback, logical_state


def native_work(root):
    kernel, _, _, step, _ = agent_preparation_fixture(root)
    assert step.preparation is not None
    return kernel, step.preparation


@pytest.mark.parametrize('size', [0, storage.MATERIAL_LIMIT])
def test_material_accepts_empty_and_exact_four_mib_without_truncation(tmp_path, size):
    kernel, preparation = native_work(tmp_path)
    body = b'x' * size
    before = kernel.status(preparation.subject)['budgets']
    with kernel.storage.transaction() as tx:
        identity = tx.put(preparation.subject, 'resource-boundary', body)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.get(identity, preparation.subject) == body
    assert kernel.status(preparation.subject)['budgets'] == before


def test_oversize_material_rolls_back_earlier_writes_and_preserves_budget(tmp_path):
    kernel, preparation = native_work(tmp_path)
    before = logical_state(kernel.storage.path)
    with pytest.raises(KernelError, match='^material_limit'):
        with kernel.storage.transaction() as tx:
            tx.put(preparation.subject, 'earlier-uncommitted-material', b'Keep this exact original evidence.')
            tx.put(preparation.subject, 'too-large', b'x' * (storage.MATERIAL_LIMIT + 1))
    assert_save_rollback(kernel.storage.path, before, registrations=1)


@pytest.mark.parametrize('limit', [-1, 0, 129])
def test_query_batch_rejects_invalid_bounds_without_changing_any_fact(tmp_path, limit):
    kernel, _ = native_work(tmp_path)
    before = logical_state(kernel.storage.path)
    with pytest.raises(KernelError, match='^query_limit'):
        with kernel.storage.transaction(write=False) as tx:
            tx.rows('SELECT 1 AS value', limit=limit)
    assert logical_state(kernel.storage.path) == before


def test_query_accepts_exact_row_budget_and_rejects_one_more_row(tmp_path):
    kernel, _ = native_work(tmp_path)
    batch = 'WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<128) SELECT n FROM seq'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.rows_read > 0
        batches, remaining = divmod(65536 - tx.rows_read, 128)
        for _ in range(batches):
            rows = tx.rows(batch)
            assert len(rows) == 128 and rows[0][0] == 1 and rows[-1][0] == 128
        if remaining:
            rows = tx.rows(batch + ' WHERE n<=?', (remaining,))
            assert [row[0] for row in rows] == list(range(1, remaining + 1))
        assert tx.rows_read == 65536
        assert tx.rows('SELECT 1 WHERE 0') == []
        with pytest.raises(KernelError, match='^query_limit'):
            tx.rows('SELECT 1')


def test_query_accepts_exact_byte_budget_then_rejects_one_more_byte(tmp_path):
    kernel, preparation = native_work(tmp_path)
    body = b'x' * storage.MATERIAL_LIMIT
    with kernel.storage.transaction() as tx:
        identity = tx.put(preparation.subject, 'query-boundary', body)
    before = logical_state(kernel.storage.path)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.bytes_read > 0  # The selected Run identity already used this owner budget.
        for _ in range((storage.QUERY_LIMIT - tx.bytes_read) // len(body)):
            assert tx.rows('SELECT body FROM material WHERE run_id=? AND id=?', (tx.run_id, identity))[0][0] == body
        remaining = storage.QUERY_LIMIT - tx.bytes_read
        if remaining:
            assert tx.rows('SELECT substr(body,1,?) FROM material WHERE run_id=? AND id=?',
                           (remaining, tx.run_id, identity))[0][0] == body[:remaining]
        assert tx.bytes_read == storage.QUERY_LIMIT
        with pytest.raises(KernelError, match='^query_limit'):
            tx.rows("SELECT CAST('x' AS BLOB)")
    assert logical_state(kernel.storage.path) == before


def test_query_budget_rejection_rolls_back_the_entire_write_scope(tmp_path, monkeypatch):
    kernel, preparation = native_work(tmp_path)
    before = logical_state(kernel.storage.path)
    with pytest.raises(KernelError, match='^query_limit'):
        with monkeypatch.context() as bounds, kernel.storage.transaction() as tx:
            tx.put(preparation.subject, 'earlier-query-scope-write', b'No partial commit.')
            # Calibrate to the already consumed read budget, rather than
            # generating 32 MiB of unrelated records or changing any policy.
            bounds.setattr(storage, 'QUERY_LIMIT', tx.bytes_read)
            tx.rows("SELECT CAST('x' AS BLOB)")
    assert_save_rollback(kernel.storage.path, before, registrations=1)


def test_current_record_guard_accepts_exact_calibrated_size_and_rejects_one_less(tmp_path, monkeypatch):
    kernel, preparation = native_work(tmp_path)
    assert core.CURRENT_LIMIT == 256 * 1024
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT * FROM subjects WHERE id=?', (preparation.subject,))
        # Existing record contract: UTF-8 bytes for text, 16 for other fields.
        size = sum(len(item.encode()) if isinstance(item, str) else 16 for item in row)
    before = logical_state(kernel.storage.path)
    monkeypatch.setattr(core, 'CURRENT_LIMIT', size)
    assert kernel.status(preparation.subject)['pending_attempt'] == preparation.attempt_id
    monkeypatch.setattr(core, 'CURRENT_LIMIT', size - 1)
    with pytest.raises(KernelError, match='^current_record_limit'):
        kernel.status(preparation.subject)
    assert logical_state(kernel.storage.path) == before


def test_database_page_limit_rejects_growth_and_rolls_back_all_material(tmp_path, monkeypatch):
    kernel, preparation = native_work(tmp_path)
    assert storage.DATABASE_LIMIT == 256 * 1024 * 1024
    with kernel.storage.transaction(write=False) as tx:
        size = tx.connection.execute('PRAGMA page_count').fetchone()[0] * tx.connection.execute('PRAGMA page_size').fetchone()[0]
    before = logical_state(kernel.storage.path)
    body_entered = False
    with monkeypatch.context() as bounds:
        bounds.setattr(storage, 'DATABASE_LIMIT', size)
        with kernel.storage.transaction(write=False):
            pass
        with pytest.raises(KernelError, match='^storage_limit'):
            with kernel.storage.transaction() as tx:
                body_entered = True
                tx.put(preparation.subject, 'page-growth', b'x' * 65536)
    assert body_entered  # Registration committed; the business growth was rejected.
    assert_save_rollback(kernel.storage.path, before, registrations=1)


def test_existing_physical_over_limit_refuses_before_the_body_and_preserves_state(tmp_path, monkeypatch):
    kernel, _ = native_work(tmp_path)
    assert storage.PHYSICAL_LIMIT == 1024 * 1024 * 1024
    path = kernel.storage.path
    with closing(sqlite3.connect(path)) as anchor:
        anchor.execute('SELECT count(*) FROM material').fetchone()
        paths = (path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm'))
        size = sum(item.stat().st_size for item in paths if item.exists())
        before = logical_state(path)
        with monkeypatch.context() as bounds:
            bounds.setattr(storage, 'PHYSICAL_LIMIT', size - 1)
            with pytest.raises(KernelError, match='^storage_limit'):
                with kernel.storage.transaction():
                    pytest.fail('An already over-limit database must not enter the body')
        assert logical_state(path) == before


def test_public_json_response_accepts_exact_limit_and_refuses_oversize_before_output():
    # {"body": ""} is 12 UTF-8 bytes. No per-field truncation is permitted.
    assert storage.QUERY_LIMIT == 32 * 1024 * 1024
    body = 'x' * (storage.QUERY_LIMIT - 12)
    stream = StringIO()
    render(QueryDocument('status', 'en', {'body': body}), style='json', details=False, stream=stream)
    assert stream.tell() == storage.QUERY_LIMIT + 1  # one JSON-line terminator
    stream.close()
    rejected = StringIO()
    with pytest.raises(QueryError) as refused:
        render(QueryDocument('status', 'en', {'body': body + 'x'}), style='json', details=False, stream=rejected)
    assert refused.value.code == 'query_limit'
    assert rejected.getvalue() == ''
