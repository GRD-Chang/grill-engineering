"""Real WAL commit growth must be admitted before the control commits."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.errors import KernelError
import agent_run.storage.sqlite as storage
from support.core_operations import agent_preparation_fixture


def physical_bytes(database: Path) -> int:
    return sum(path.stat().st_size for path in
               (database, Path(str(database) + '-wal'), Path(str(database) + '-shm'))
               if path.exists())


def test_public_stop_reserves_commit_growth_and_retries_without_partial_action(tmp_path, monkeypatch):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    preparation = step.preparation
    assert preparation is not None
    kernel.preparation_failed(preparation, 'worker_preparation_failed', {'message': 'No resources created'})
    assert kernel.control('resume', 'begin-controls', subject=preparation.subject) == 'applied'
    database = kernel.storage.path
    budgets = kernel.status(preparation.subject)['budgets']
    # An ordinary open reader retains this small WAL. Keep the real default
    # physical/database ratio, scaling both caps to a few MiB, not host disk.
    assert storage.PHYSICAL_LIMIT == 4 * storage.DATABASE_LIMIT == 1024 * 1024 * 1024
    with closing(sqlite3.connect(database)) as reader:
        page_size = reader.execute('PRAGMA page_size').fetchone()[0]
        pages = reader.execute('PRAGMA page_count').fetchone()[0]
        monkeypatch.setattr(storage, 'DATABASE_LIMIT', 2 * pages * page_size)
        monkeypatch.setattr(storage, 'PHYSICAL_LIMIT', 4 * storage.DATABASE_LIMIT)
        boundaries = []
        connect = sqlite3.connect

        def observed_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if str(args[0]) == database.as_uri() + '?mode=rw':
                connection.set_trace_callback(lambda sql: boundaries.append(sql)
                    if sql in ('BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK') else None)
            return connection

        monkeypatch.setattr(sqlite3, 'connect', observed_connection)
        rejected = False
        for index in range(500):
            for kind in ('stop', 'resume'):
                before = tuple(reader.iterdump())
                action = f'quota-{kind}-{index}'
                boundaries.clear()
                try:
                    assert kernel.control(kind, action, subject=preparation.subject) == 'applied'
                except KernelError as error:
                    assert error.code == 'storage_limit'
                    assert boundaries == ['BEGIN IMMEDIATE', 'ROLLBACK']
                    assert tuple(reader.iterdump()) == before
                    rejected = True
                    break
                assert physical_bytes(database) <= storage.PHYSICAL_LIMIT
            if rejected:
                break
        assert rejected, 'the bounded public controls must reach the physical admission boundary'
        assert reader.execute('PRAGMA page_count').fetchone()[0] * page_size < storage.DATABASE_LIMIT
        before = tuple(reader.iterdump())
        boundaries.clear()
        with pytest.raises(KernelError, match='storage_limit'):
            kernel.control('stop', 'capacity-stop', subject=preparation.subject)
        assert boundaries == ['BEGIN IMMEDIATE', 'ROLLBACK']
        assert tuple(reader.iterdump()) == before
        assert physical_bytes(database) <= storage.PHYSICAL_LIMIT
    # Last-connection cleanup checkpoints the WAL; same limits and exact public
    # intent now work. No config increase, SQL repair, new Attempt or refund.
    assert kernel.control('stop', 'capacity-stop', subject=preparation.subject) == 'applied'
    assert kernel.control('stop', 'capacity-stop', subject=preparation.subject) == 'applied'
    assert kernel.status(preparation.subject)['budgets'] == budgets
    assert sum(event['kind'] == 'control_applied' and event['identity'] == 'capacity-stop'
               for event in kernel.history(preparation.subject)) == 1
    assert physical_bytes(database) <= storage.PHYSICAL_LIMIT


@pytest.mark.parametrize('pinned_snapshot', [False, True])
def test_commit_capacity_includes_spill_checkpoint_and_wal_index_growth(tmp_path, monkeypatch, pinned_snapshot):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    preparation = step.preparation
    assert preparation is not None
    database = kernel.storage.path
    original_database_size = database.stat().st_size
    monkeypatch.setattr(storage, 'DATABASE_LIMIT', 32 * 1024 * 1024)
    monkeypatch.setattr(storage, 'PHYSICAL_LIMIT', 4 * storage.DATABASE_LIMIT)
    identities = []
    with closing(sqlite3.connect(database)) as reader:
        if pinned_snapshot:
            reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM material').fetchone()
        for index in range(6 if pinned_snapshot else 2):
            body = bytes([index]) * (3 * 1024 * 1024)
            with kernel.storage.transaction() as tx:
                identities.append(tx.put(preparation.subject, f'capacity-material-{index}', body))
            assert physical_bytes(database) <= storage.PHYSICAL_LIMIT
        if pinned_snapshot:
            # A real old snapshot prevents checkpointing. More than 4096 WAL
            # frames require another real 32 KiB index region.
            assert database.stat().st_size == original_database_size
            assert Path(str(database) + '-wal').stat().st_size > 16 * 1024 * 1024
            assert Path(str(database) + '-shm').stat().st_size >= 65536
        else:
            # The ordinary 1000-frame autocheckpoint grows the main database
            # during COMMIT while this idle connection keeps sidecars present.
            assert database.stat().st_size > original_database_size
    with kernel.storage.transaction(write=False) as tx:
        for index, identity in enumerate(identities):
            assert tx.get(identity, preparation.subject) == bytes([index]) * (3 * 1024 * 1024)
    assert physical_bytes(database) <= storage.PHYSICAL_LIMIT
