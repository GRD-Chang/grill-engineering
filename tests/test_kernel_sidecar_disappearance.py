"""Disappearing SQLite sidecars never authorize a different state file."""
from contextlib import closing
import errno
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from agent_run.storage import preflight
from agent_run.storage.sqlite import Storage


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_unlinked_sidecar_requires_one_absence_check_before_connect(tmp_path, monkeypatch, suffix):
    native = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    writer = sqlite3.connect(native.path)
    writer.execute('PRAGMA user_version=7')
    writer.commit()
    target = Path(str(native.path) + suffix)
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    original_stat, original_connect = Path.stat, sqlite3.connect
    events = []

    def observe(path, *args, **kwargs):
        if path == target:
            assert kwargs.get('follow_symlinks') is False
            events.append('stat')
            if events == ['stat']:
                writer.close()  # Only SQLite checkpoints/removes its own WAL/SHM.
                metadata = os.fstat(descriptor)
                assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 0
                return metadata
            assert events == ['stat', 'stat']
            with pytest.raises(FileNotFoundError) as missing:
                original_stat(path, *args, **kwargs)
            assert missing.value.errno == errno.ENOENT
            raise missing.value
        return original_stat(path, *args, **kwargs)

    def connect(*args, **kwargs):
        assert events == ['stat', 'stat']
        events.append('connect')
        return original_connect(*args, **kwargs)

    try:
        monkeypatch.setattr(Path, 'stat', observe)
        monkeypatch.setattr(sqlite3, 'connect', connect)
        with preflight.read_state(native.path) as reader:
            assert reader.execute('PRAGMA user_version').fetchone()[0] == 7
            assert reader.execute('PRAGMA query_only').fetchone()[0] == 1
            assert reader.execute('PRAGMA trusted_schema').fetchone()[0] == 0
        assert events == ['stat', 'stat', 'connect']
    finally:
        writer.close()
        os.close(descriptor)


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
@pytest.mark.parametrize('outcome', ['same', 'new', 'zero', 'EACCES', 'EIO', 'ENOTDIR', 'false_ENOENT'])
def test_zero_link_sidecar_recheck_cannot_adopt_existing_or_unreadable_object(tmp_path, monkeypatch, suffix, outcome):
    native = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    target = Path(str(native.path) + suffix)
    original = Path.stat
    calls = []

    def observe(path, *args, **kwargs):
        if path != target:
            return original(path, *args, **kwargs)
        assert kwargs.get('follow_symlinks') is False
        calls.append(path)
        if len(calls) == 1:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0, st_size=32768, st_dev=1, st_ino=2)
        assert len(calls) == 2
        if outcome in {'EACCES', 'EIO', 'ENOTDIR'}:
            raise OSError(getattr(errno, outcome), 'controlled access error')
        if outcome == 'false_ENOENT':
            raise FileNotFoundError(errno.EIO, 'exception class alone is insufficient')
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0 if outcome == 'zero' else 1,
                               st_size=32768, st_dev=1, st_ino=3 if outcome == 'new' else 2)

    monkeypatch.setattr(Path, 'stat', observe)
    with pytest.raises(preflight.UnsupportedState):
        with preflight.read_state(native.path):
            pytest.fail('unsafe sidecar yielded a connection')
    assert len(calls) == 2


@pytest.mark.parametrize('target', ['database', 'directory'])
@pytest.mark.parametrize('fault', ['missing', 'replacement', 'nonregular', 'hardlink'])
def test_identity_is_rechecked_after_real_snapshot_before_yield(tmp_path, monkeypatch, target, fault):
    native = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    watched = native.path if target == 'database' else native.path.parent
    original_stat, original_connect = Path.stat, sqlite3.connect
    snapshot = []
    connections = []
    reads_after_snapshot = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if 'SELECT' in sql.upper() and 'SQLITE_SCHEMA' in sql.upper():
                snapshot.append(sql)
            return cursor

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=Connection)
        connections.append(connection)
        return connection

    def observe(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == watched and snapshot:
            reads_after_snapshot.append(path)
            if fault == 'missing':
                raise FileNotFoundError(errno.ENOENT, 'controlled disappearance')
            values = {name: getattr(metadata, name) for name in ('st_mode', 'st_nlink', 'st_dev', 'st_ino')}
            if fault == 'replacement':
                values['st_ino'] += 1
            elif fault == 'nonregular':
                values['st_mode'] = stat.S_IFIFO | 0o600
            elif fault == 'hardlink':
                # Directory nlink is not an identity/security predicate.
                values['st_nlink'] += 1
            return SimpleNamespace(**values)
        return metadata

    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(Path, 'stat', observe)
    if target == 'directory' and fault == 'hardlink':
        with preflight.read_state(native.path):
            assert snapshot and reads_after_snapshot
    else:
        with pytest.raises(preflight.UnsupportedState):
            with preflight.read_state(native.path):
                pytest.fail('identity changed before yield')
    assert snapshot and reads_after_snapshot
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


def test_real_snapshot_precedes_yield_and_keeps_uncheckpointed_wal_visible(tmp_path):
    native = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    with closing(sqlite3.connect(native.path)) as writer:
        writer.execute('PRAGMA user_version=7')
        writer.commit()
        wal = Path(str(native.path) + '-wal')
        assert wal.stat().st_size > 0
        # Read descriptors remain open throughout: do not close an extra main
        # database fd while SQLite may hold POSIX locks on it.
        with native.path.open('rb') as main_bytes, wal.open('rb') as wal_bytes:
            before = (main_bytes.read(), wal_bytes.read())
            with preflight.read_state(native.path) as reader:
                assert reader.execute('PRAGMA user_version').fetchone()[0] == 7
            main_bytes.seek(0)
            wal_bytes.seek(0)
            assert (main_bytes.read(), wal_bytes.read()) == before
        with preflight.read_state(native.path) as reader:
            # A writer commits after yield and before the caller's first query.
            # Only a real database read before yield can retain version 7.
            writer.execute('PRAGMA user_version=8')
            writer.commit()
            assert reader.execute('PRAGMA user_version').fetchone()[0] == 7


@pytest.mark.parametrize(('suffix', 'mode', 'links', 'size'), [
    ('', stat.S_IFREG | 0o600, 0, 4096),
    ('-journal', stat.S_IFREG | 0o600, 0, 0),
    ('-journal', stat.S_IFREG | 0o600, 1, 32),
    ('-wal', stat.S_IFREG | 0o600, 2, 32768),
    ('-shm', stat.S_IFREG | 0o600, 2, 32768),
    ('-wal', stat.S_IFIFO | 0o600, 0, 0),
    ('-shm', stat.S_IFLNK | 0o777, 0, 0),
])
def test_unsafe_objects_never_enter_the_disappearance_exception(tmp_path, monkeypatch, suffix, mode, links, size):
    native = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    target = Path(str(native.path) + suffix)
    original = Path.stat
    calls = []

    def observe(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            return SimpleNamespace(st_mode=mode, st_nlink=links, st_size=size, st_dev=1, st_ino=2)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', observe)
    with pytest.raises(preflight.UnsupportedState):
        with preflight.read_state(native.path):
            pytest.fail('unsafe object yielded a connection')
    assert len(calls) == 1
