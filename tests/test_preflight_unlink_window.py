"""Bounded metadata classification before any SQLite open or business callback.

Controlled stat sequences exercise refusals; real SQLite owns the database and
the successful read. Native unlink races are separate filesystem evidence.
"""
from contextlib import closing, contextmanager
import errno
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from agent_run.storage import preflight, save_order


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'original.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE original(value INTEGER)')
        db.execute('INSERT INTO original VALUES(7)')
        db.commit()
    return path


def zero(**changes):
    return SimpleNamespace(**(dict(st_mode=stat.S_IFREG | 0o600, st_nlink=0,
        st_dev=1, st_ino=2, st_size=12392) | changes))


def observe(monkeypatch, database, suffix, values):
    original, calls = Path.stat, []
    selected = Path(str(database) + suffix)
    def metadata(path, *args, **kwargs):
        if path == selected:
            assert not args and kwargs == {'follow_symlinks': False}
            calls.append(path)
            return values(len(calls), original, path, kwargs)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', metadata)
    return calls


def forbid_connect(monkeypatch):
    def connect(*args, **kwargs):
        pytest.fail('Unresolved metadata must not open SQLite')
    monkeypatch.setattr(sqlite3, 'connect', connect)


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
@pytest.mark.parametrize('absolute', [False, True])
def test_repeated_zero_link_resolves_only_by_absence_before_one_read(database, monkeypatch, record_property,
                                                                   suffix, absolute):
    before, now, events = database.read_bytes(), [100.25], []
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0], raising=False)
    def values(count, original, path, kwargs):
        now[0] += 0.04
        events.append('stat')
        if count <= 3:
            return zero()
        return original(path, **kwargs)  # The real pathname is absent.
    calls = observe(monkeypatch, database, suffix, values)
    original_connect = sqlite3.connect
    connections, timeouts = [], []
    def connect(*args, **kwargs):
        assert len(calls) == 4 and events == ['stat'] * 4
        assert args == (database.as_uri() + '?mode=ro',)
        assert kwargs == dict(uri=True, isolation_level=None, timeout=pytest.approx(0.09 if absolute else 0.84))
        timeouts.append(kwargs['timeout'])
        events.append('connect')
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with preflight.read_state(database, timeout=1, **({'deadline': 100.5} if absolute else {})) as reader:
        assert reader.execute('SELECT value FROM original').fetchone() == (7,)
        assert reader.execute('PRAGMA query_only').fetchone() == (1,)
        assert reader.execute('PRAGMA trusted_schema').fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute('UPDATE original SET value=8')
    assert len(connections) == 1 and database.read_bytes() == before
    record_property('preflight_budget_trace', json.dumps(dict(start=100.25,
        original_deadline=100.5 if absolute else 101.25, observed_at=now[0],
        connect_timeout=timeouts, events=events)))
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
@pytest.mark.parametrize('outcome', ['same', 'new_inode', 'new_device', 'symlink', 'fifo',
    'directory', 'hardlink', 'EACCES', 'EIO', 'ENOTDIR', 'false_ENOENT', 'persistent_zero'])
def test_zero_link_never_adopts_an_existing_or_unreadable_object(database, monkeypatch, suffix, outcome):
    before = database.read_bytes()
    def values(count, original, path, kwargs):
        if count == 1 or outcome == 'persistent_zero':
            return zero()
        if outcome in {'EACCES', 'EIO', 'ENOTDIR'}:
            raise OSError(getattr(errno, outcome), 'Controlled refusal')
        if outcome == 'false_ENOENT':
            raise FileNotFoundError(errno.EIO, 'Exception type is not absence')
        changes = {'same': dict(st_nlink=1), 'new_inode': dict(st_ino=3),
            'new_device': dict(st_dev=3), 'symlink': dict(st_mode=stat.S_IFLNK | 0o777),
            'fifo': dict(st_mode=stat.S_IFIFO | 0o600),
            'directory': dict(st_mode=stat.S_IFDIR | 0o700), 'hardlink': dict(st_nlink=2)}
        return zero(**changes[outcome])
    calls = observe(monkeypatch, database, suffix, values)
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.25)
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState):
        with preflight.read_state(database):
            pytest.fail('Unresolved sidecar yielded a connection')
    assert len(calls) == (33 if outcome == 'persistent_zero' else 2)
    assert database.read_bytes() == before


@pytest.mark.parametrize('stage', ['first', 'repeat', 'absence'])
def test_expired_original_deadline_refuses_even_an_absence_result(database, monkeypatch, stage):
    now = [100.25]
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    def values(count, original, path, kwargs):
        if stage == 'first' or count == 2:
            now[0] = 100.5
        if count == 2 and stage == 'absence':
            return original(path, **kwargs)
        return zero()
    calls = observe(monkeypatch, database, '-wal', values)
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database, timeout=1, deadline=100.5):
            pytest.fail('Expired budget yielded a connection')
    assert len(calls) == (1 if stage == 'first' else 2)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1., 100.])
def test_untrustworthy_or_regressing_clock_cannot_open_database(database, monkeypatch, value):
    readings = iter([100.25, value])
    monkeypatch.setattr(save_order, 'monotonic', lambda: next(readings))
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database):
            pytest.fail('Invalid clock yielded a connection')


@pytest.mark.parametrize('phase', ['start', 'recheck'])
def test_unavailable_clock_refuses_without_opening(database, monkeypatch, phase):
    calls = []
    def clock():
        calls.append(True)
        if phase == 'start' or len(calls) > 1:
            raise OSError(errno.EIO, 'Clock unavailable')
        return 100.25
    monkeypatch.setattr(save_order, 'monotonic', clock)
    observe(monkeypatch, database, '-wal', lambda *_: zero())
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database):
            pytest.fail('Unavailable budget yielded a connection')


@pytest.mark.parametrize('field,value', [('timeout', 0), ('timeout', -1), ('timeout', True),
    ('timeout', float('nan')), ('timeout', float('inf')), ('deadline', float('nan')),
    ('deadline', float('inf')), ('deadline', 100.25)])
def test_invalid_budget_is_not_a_new_wait_allowance(database, monkeypatch, field, value):
    monkeypatch.setattr(save_order, 'monotonic', lambda: 100.25)
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database, **{field: value}):
            pytest.fail('Invalid budget yielded a connection')


@pytest.mark.parametrize('properties', [dict(monotonic=False, adjustable=False),
    dict(monotonic=True, adjustable=True)])
def test_unproven_monotonic_provider_is_refused(database, monkeypatch, properties):
    monkeypatch.setattr(preflight, 'get_clock_info', lambda _: SimpleNamespace(**properties))
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database):
            pytest.fail('Untrusted clock yielded a connection')


@pytest.mark.parametrize('target,fault', [
    (target, fault) for target in ('database', 'directory')
    for fault in ('missing', 'replacement', 'nonregular')
] + [('database', 'zero'), ('database', 'hardlink')])
def test_wait_does_not_adopt_changed_database_or_parent(database, monkeypatch, target, fault):
    original, resolved = Path.stat, []
    selected = Path(str(database) + '-wal')
    watched = database if target == 'database' else database.parent
    def metadata(path, *args, **kwargs):
        if path == selected:
            if not resolved:
                resolved.append(True)
                return zero()
            return original(path, *args, **kwargs)
        value = original(path, *args, **kwargs)
        if path == watched and resolved:
            if fault == 'missing':
                raise FileNotFoundError(errno.ENOENT, 'Original identity disappeared')
            return SimpleNamespace(st_mode=stat.S_IFIFO if fault == 'nonregular' else value.st_mode,
                st_nlink=0 if fault == 'zero' else 2 if fault == 'hardlink' else value.st_nlink,
                st_dev=value.st_dev, st_ino=value.st_ino + (fault == 'replacement'))
        return value
    monkeypatch.setattr(Path, 'stat', metadata)
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState):
        with preflight.read_state(database):
            pytest.fail('Changed identity yielded a connection')


def test_disappearance_does_not_skip_journal_or_protocol_rejection(database, monkeypatch):
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE schema_meta(key TEXT,value TEXT)')
        db.execute('INSERT INTO schema_meta VALUES(?,?)', ('protocol', preflight.LEGACY_PROTOCOL))
        db.commit()
    def values(count, original, path, kwargs):
        return zero() if count <= 3 else original(path, **kwargs)
    calls = observe(monkeypatch, database, '-wal', values)
    with pytest.raises(preflight.UnsupportedState, match='legacy_v6'):
        preflight.require_supported_state(database)
    assert len(calls) == 4
    journal = Path(str(database) + '-journal')
    journal.write_bytes(b'Unrecovered journal')
    calls.clear()
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        preflight.require_supported_state(database)
    assert len(calls) == 4 and journal.read_bytes() == b'Unrecovered journal'


def test_first_stat_exception_type_cannot_turn_io_error_into_absence(database, monkeypatch):
    def values(*args):
        raise FileNotFoundError(errno.EIO, 'Exception type is not absence')
    calls = observe(monkeypatch, database, '-wal', values)
    forbid_connect(monkeypatch)
    with pytest.raises(preflight.UnsupportedState, match='unreadable_state') as caught:
        with preflight.read_state(database):
            pytest.fail('Unproven absence yielded a connection')
    assert len(calls) == 1 and caught.value.__cause__.errno == errno.EIO


@pytest.mark.parametrize('stage', ['connect', 'snapshot'])
def test_expiry_during_original_open_or_snapshot_closes_without_yield(database, monkeypatch, record_property, stage):
    now, connections, statements, timeouts = [100.25], [], [], []
    monkeypatch.setattr(save_order, 'monotonic', lambda: now[0])
    original_connect = sqlite3.connect
    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            cursor = super().execute(sql, *args, **kwargs)
            if sql.startswith('SELECT name FROM sqlite_schema'):
                now[0] = 100.5
            return cursor
    def connect(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        connection = original_connect(*args, **kwargs, factory=Connection)
        connections.append(connection)
        if stage == 'connect':
            now[0] = 100.5
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(preflight.UnsupportedState, match='uncertain_state'):
        with preflight.read_state(database, timeout=1, deadline=100.5):
            pytest.fail('Expired snapshot yielded a connection')
    assert len(connections) == 1
    if stage == 'connect':
        assert not statements
    else:
        assert 'PRAGMA busy_timeout=250' in statements
    observed_statements = list(statements)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
    record_property('preflight_budget_trace', json.dumps(dict(stage=stage, original_deadline=100.5,
        observed_at=now[0], connect_timeout=timeouts, connections=len(connections), yielded=False,
        closed=True, statements=observed_statements)))


def test_storage_reads_and_control_preflight_keep_the_original_absolute_deadline(tmp_path, monkeypatch, record_property):
    from agent_run.storage import sqlite as storage_module
    native = storage_module.Storage.create(tmp_path / 'state.sqlite3',
        namespace=('example/project', str(tmp_path.resolve())))
    deadline, seen = save_order.monotonic() + 0.5, []
    original_support, original_read = storage_module.require_supported_state, storage_module.read_state
    def support(path, **kwargs):
        seen.append(('support', kwargs['deadline']))
        return original_support(path, **kwargs)
    @contextmanager
    def read(path, **kwargs):
        seen.append(('read', kwargs['deadline']))
        with original_read(path, **kwargs) as connection:
            yield connection
    monkeypatch.setattr(storage_module, 'require_supported_state', support)
    monkeypatch.setattr(storage_module, 'read_state', read)
    with native.transaction(write=False, deadline=deadline):
        pass
    with native.transaction(ordered=False, deadline=deadline):
        pass
    assert {kind for kind, _ in seen} == {'support', 'read'}
    assert all(observed == deadline for _, observed in seen)
    record_property('preflight_deadline_trace', json.dumps(dict(original_deadline=deadline, observed=seen)))
