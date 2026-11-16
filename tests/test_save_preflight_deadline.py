"""Controlled deadline classification on real SQLite; not native FIFO evidence."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage import preflight, save_order, sqlite as storage
from agent_run.storage.preflight import UnsupportedState


@pytest.fixture
def database(tmp_path):
    return storage.Storage.create(tmp_path / 'state.sqlite3', namespace=('example/project', str(tmp_path.resolve())))


@pytest.mark.parametrize('readings', [
    [100., 101.],
    [100., 100., 101.],
    [100., 100., 100., 101.],
    [100., 100., 100., 100., 100., 101.],
])
def test_save_preflight_expiry_uses_the_original_absolute_deadline(database, monkeypatch, readings):
    identity = save_order.database_identity(database.path)
    values = iter(readings)
    monkeypatch.setattr(save_order, 'monotonic', lambda: next(values))
    with pytest.raises(KernelError) as caught:
        database._verify_save_database(identity, 101.)
    assert caught.value.code == 'storage_busy'
    assert isinstance(caught.value.__cause__, preflight._ReadBudgetExpired)
    assert caught.value.__cause__.deadline == 101.


@pytest.mark.parametrize('ordered', [False, True])
def test_initial_preflight_expiry_never_opens_a_body_or_captures_save_clock(database, monkeypatch, ordered):
    now = [100.]
    monkeypatch.setattr(storage, 'monotonic', lambda: now[0])
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    initialize = preflight._ReadBudget.__init__
    def expired(budget, path, timeout, deadline):
        assert deadline == 101. and timeout == 1.
        now[0] = 101.
        initialize(budget, path, timeout, deadline)
    monkeypatch.setattr(preflight._ReadBudget, '__init__', expired)
    monkeypatch.setattr(storage, 'SaveClock', lambda: pytest.fail('Expired preflight captured a SaveClock'))
    with pytest.raises(KernelError) as caught:
        with database.transaction(ordered=ordered):
            pytest.fail('Expired preflight entered its business body')
    assert caught.value.code == 'storage_busy' and not save_order._owners


def test_readonly_expiry_keeps_the_existing_unsupported_state_contract(database, monkeypatch):
    readings = iter([100., 101.])
    monkeypatch.setattr(save_order, 'monotonic', lambda: next(readings))
    with pytest.raises(UnsupportedState) as caught:
        with preflight.read_state(database.path, timeout=1., deadline=101.):
            pytest.fail('Expired read yielded a connection')
    assert (caught.value.code, caught.value.reason, caught.value.protocol) == ('unsupported_state', 'uncertain_state', None)


@pytest.mark.parametrize('boundary', ['initial', 'verify'])
def test_another_observation_deadline_is_not_reclassified(database, monkeypatch, boundary):
    exact = preflight._ReadBudgetExpired(database.path, 100.5)
    monkeypatch.setattr(storage, 'monotonic', lambda: 100.)
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.)
    if boundary == 'initial':
        def refuse(*args, **kwargs):
            raise exact
        monkeypatch.setattr(storage, 'require_supported_state', refuse)
    else:
        @contextmanager
        def refuse(*args, **kwargs):
            raise exact
            yield
        monkeypatch.setattr(storage, 'read_state', refuse)
    with pytest.raises(UnsupportedState) as caught:
        if boundary == 'initial':
            with database.transaction(ordered=False):
                pytest.fail('Unproven preflight entered body')
        else:
            database._verify_save_database(save_order.database_identity(database.path), 101.)
    assert caught.value is exact


@pytest.mark.parametrize('value', [99., float('nan'), float('inf'), -1., OSError('clock unavailable')])
def test_clock_uncertainty_does_not_become_contention(database, monkeypatch, value):
    readings = iter([100., 100., value])
    def clock():
        current = next(readings)
        if isinstance(current, BaseException):
            raise current
        return current
    monkeypatch.setattr(save_order, 'monotonic', clock)
    with pytest.raises(UnsupportedState) as caught:
        database._verify_save_database(save_order.database_identity(database.path), 101.)
    assert not isinstance(caught.value, preflight._ReadBudgetExpired)
    assert caught.value.reason == 'uncertain_state'


def test_real_file_replacement_at_deadline_stays_an_identity_refusal(database, tmp_path, monkeypatch):
    replacement = storage.Storage.create(tmp_path / 'replacement.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    identity = save_order.database_identity(database.path)
    now, calls = [100.], []
    metadata = Path.lstat
    def replaced(path):
        if path == database.path:
            calls.append(path)
            if len(calls) == 3:
                os.replace(replacement.path, database.path)
                now[0] = 101.
        return metadata(path)
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    monkeypatch.setattr(Path, 'lstat', replaced)
    with pytest.raises(UnsupportedState) as caught:
        database._verify_save_database(identity, 101.)
    assert len(calls) == 3 and now[0] == 101.
    assert caught.value.reason == 'uncertain_state'
    assert not isinstance(caught.value, preflight._ReadBudgetExpired)


def test_sidecar_io_error_at_deadline_is_not_an_expiry_signal(database, monkeypatch):
    now = [100.]
    metadata = Path.stat
    exact = OSError(errno.EIO, 'controlled sidecar failure')
    def unreadable(path, *args, **kwargs):
        if path == Path(str(database.path) + '-wal'):
            now[0] = 101.
            raise exact
        return metadata(path, *args, **kwargs)
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    monkeypatch.setattr(Path, 'stat', unreadable)
    with pytest.raises(UnsupportedState) as caught:
        database._verify_save_database(save_order.database_identity(database.path), 101.)
    assert now[0] == 101. and caught.value.reason == 'unreadable_state'
    assert caught.value.__cause__ is exact


def test_protocol_refusal_at_deadline_is_not_hidden(database, monkeypatch):
    with sqlite3.connect(database.path) as db:
        db.execute("UPDATE meta SET protocol='foreign-protocol'")
    now = [100.]
    protocol = preflight._protocol
    def inspect(*args):
        result = protocol(*args)
        now[0] = 101.
        return result
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    monkeypatch.setattr(preflight, '_protocol', inspect)
    with pytest.raises(UnsupportedState) as caught:
        database._verify_save_database(save_order.database_identity(database.path), 101.)
    assert now[0] == 101.
    assert (caught.value.reason, caught.value.protocol) == ('unknown_protocol', 'foreign-protocol')


@pytest.mark.parametrize('exact', [OSError('close failed'), InterruptedError('close interrupted')])
def test_close_failure_during_expiry_retains_its_original_category(database, monkeypatch, exact):
    connect, handles, closed = sqlite3.connect, [], []
    class Connection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.append(self)
            raise exact
    def opened(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        handles.append(connection)
        return connection
    readings = iter([100., 100., 100., 101.])
    monkeypatch.setattr(save_order, 'monotonic', lambda: next(readings))
    monkeypatch.setattr(sqlite3, 'connect', opened)
    with pytest.raises(type(exact)) as caught:
        database._verify_save_database(save_order.database_identity(database.path), 101.)
    assert caught.value is exact and handles == closed and len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        handles[0].execute('SELECT 1')


def test_expiry_after_registration_never_replays_or_renews_the_request(database, monkeypatch):
    class DecisionClock:
        domain = json.dumps(['native-linux-local-wal-save/2', '12345678-1234-1234-1234-123456789abc',
                             'clock_gettime(CLOCK_MONOTONIC)', 1, 2, 0, 0], separators=(',', ':'))
        def require(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(storage, 'SaveClock', DecisionClock)
    now = [100.]
    monkeypatch.setattr(storage, 'monotonic', lambda: now[0])
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    original_register = save_order.RepositorySave.register
    requests = []
    def registered(ticket, tx):
        original_register(ticket, tx)
        requests.append(ticket)
    monkeypatch.setattr(save_order.RepositorySave, 'register', registered)
    verify, calls = storage.Storage._verify_save_database, []
    def check(native, identity, deadline):
        calls.append(deadline)
        if len(calls) == 2:
            values = iter([100., 100., 100., 101.])
            monkeypatch.setattr(save_order, 'monotonic', lambda: next(values, 101.))
        return verify(native, identity, deadline)
    monkeypatch.setattr(storage.Storage, '_verify_save_database', check)
    bodies = []
    with pytest.raises(KernelError) as caught:
        with database.transaction():
            bodies.append(True)
    assert caught.value.code == 'storage_busy' and bodies == []
    assert len(requests) == 1 and calls == [101., 101., 101.]
    ticket = requests[0]
    assert not ticket.body_entered and not ticket.commit.attempted and not ticket.commit.confirmed
    with sqlite3.connect(database.path) as db:
        assert db.execute('SELECT request,deadline FROM save_admissions').fetchall() == [(ticket.request, 101.)]
    assert not save_order._owners
