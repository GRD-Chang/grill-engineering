"""Same-snapshot Run adoption keeps the owner budget and fresh guard boundaries."""
from contextlib import closing, contextmanager
import json
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.model import Execution
from agent_run.storage.sqlite import QUERY_LIMIT
from support.run_scoped_kernel import ScopedKernel, effect_runs


@pytest.mark.parametrize('encoding', ['UTF-8', 'UTF-16le'])
@pytest.mark.parametrize('policy_bytes', [8 * 1024 * 1024, 20 * 1024 * 1024])
def test_observation_adopts_one_run_without_reencoding_or_resetting_budget(monkeypatch, encoding, policy_bytes):
    with effect_runs(monkeypatch) as (original, _), closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA encoding='" + encoding + "'")
        db.executescript('\n'.join(original.iterdump()))
        raw = bytes(db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0])
        padded = raw + b' ' * (policy_bytes - len(raw))
        assert json.loads(padded) == json.loads(raw)
        db.execute("UPDATE runs SET policy=? WHERE id='A'", (padded,))
        kernel = ScopedKernel(db, 'A')
        request = kernel.begin_observation('parent:1', execution=Execution(run_id='A', owner='same-owner', epoch=1))
        assert request.parent_number == 1
        assert request.query.source_ref == 'refs/heads/work'
        assert request.fence.subject == 'parent:1'
        # Healthy demand and its supervision share this short read operation.
        # A large equivalent policy fits because it is actually adopted once,
        # rather than because a helper reset counters or changed limits.
        assert len(kernel.owners) == 1
        assert all(policy_bytes < owner.bytes_read <= QUERY_LIMIT for owner in kernel.owners)
        assert db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0] == padded


def test_observation_still_rejects_a_run_exceeding_the_original_owner_budget(monkeypatch):
    with effect_runs(monkeypatch) as (db, kernels):
        raw = bytes(db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0])
        db.execute("UPDATE runs SET policy=? WHERE id='A'", (raw + b' ' * (QUERY_LIMIT - len(raw)),))
        with pytest.raises(KernelError, match='query_limit'):
            kernels['A'].begin_observation('parent:1', execution=Execution(run_id='A', owner='same-owner', epoch=1))
        assert len(kernels['A'].owners) == 1


@pytest.mark.parametrize('raw', [b'{bad json', b'[]', 'utf16'])
def test_policy_representation_is_still_validated_at_its_actual_consumer(monkeypatch, raw):
    with effect_runs(monkeypatch) as (db, kernels):
        kernel = kernels['A']
        expected = kernel._policy(db.execute("SELECT * FROM runs WHERE id='A'").fetchone())
        if raw == 'utf16':
            raw = bytes(db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0]).decode().encode('utf-16')
        db.execute("UPDATE runs SET policy=? WHERE id='A'", (raw,))
        # Constructing a demand never decoded policy. Reuse must neither add a
        # format restriction here nor bypass validation when policy is consumed.
        kernel.begin_observation('parent:1', execution=Execution(run_id='A', owner='same-owner', epoch=1))
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            if raw.startswith(b'\xff\xfe'):
                assert kernel._policy(run) == expected
            else:
                with pytest.raises((ValueError, KernelError)):
                    kernel._policy(run)


@pytest.mark.parametrize('changed', ["executor='other'", 'epoch=2', 'stopped=1'])
def test_each_new_observation_checks_current_control(monkeypatch, changed):
    with effect_runs(monkeypatch) as (db, kernels):
        kernel, execution = kernels['A'], Execution(run_id='A', owner='same-owner', epoch=1)
        kernel.begin_observation('parent:1', execution=execution)
        db.execute("UPDATE runs SET " + changed + " WHERE id='A'")
        with pytest.raises(KernelError, match='executor_fenced|automatic_step_paused'):
            kernel.begin_observation('parent:1', execution=execution)


def test_foreign_run_cannot_supply_a_fence(monkeypatch):
    with effect_runs(monkeypatch) as (db, kernels):
        foreign = db.execute("SELECT * FROM runs WHERE id='B'").fetchone()
        with kernels['A'].storage.transaction(write=False) as tx:
            with pytest.raises(KernelError, match='effect_binding'):
                kernels['A']._read_fence(tx, 'parent:1', Execution(run_id='A', owner='same-owner', epoch=1), False, run=foreign)


@pytest.mark.parametrize('failure', [OSError('close'), InterruptedError('cancel')])
def test_read_exit_failure_prevents_supervision_or_return(monkeypatch, failure):
    with effect_runs(monkeypatch) as (_db, kernels):
        kernel = kernels['A']
        transaction = kernel.storage.transaction
        reached = []
        @contextmanager
        def fail_after_read(**kwargs):
            with transaction(**kwargs) as tx:
                yield tx
            raise failure
        monkeypatch.setattr(kernel.storage, 'transaction', fail_after_read)
        monkeypatch.setattr(kernel, '_guard_automatic_step', lambda *args, **kwargs: reached.append(True))
        with pytest.raises(type(failure)) as caught:
            kernel.begin_observation('parent:1', execution=Execution(run_id='A', owner='same-owner', epoch=1))
        assert caught.value is failure
        assert reached == []


def test_first_run_read_remains_subject_to_sql_authorizer_and_owner_poison(monkeypatch):
    with effect_runs(monkeypatch) as (db, kernels):
        kernel = kernels['A']
        def authorizer(operation, table, column, _database, _source):
            return sqlite3.SQLITE_DENY if (operation, table, column) == (sqlite3.SQLITE_READ, 'runs', 'policy') else sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                kernel.begin_observation('parent:1', execution=Execution(run_id='A', owner='same-owner', epoch=1))
            assert len(kernel.owners) == 1 and kernel.owners[0].failed
            assert not db.in_transaction
        finally:
            db.set_authorizer(None)
