"""Current storage preflight preserves committed data and rejects missing state."""
from contextlib import closing
import errno
from pathlib import Path
import sqlite3

import pytest

from agent_run.storage.preflight import PROTOCOL, UnsupportedState, require_supported_state
from agent_run.storage.sqlite import Storage
from agent_run.errors import KernelError


@pytest.mark.parametrize('exception,code', [
    (sqlite3.OperationalError, code) for code in (
        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_RECOVERY,
        sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_BUSY_TIMEOUT,
        sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_ERROR, None, 1029,
    )
] + [(sqlite3.ProgrammingError, sqlite3.SQLITE_BUSY)])
def test_transaction_classifies_only_supported_native_busy_codes(tmp_path, monkeypatch, exception, code):
    native = Storage.create(tmp_path / 'state.sqlite3')
    error = exception('database is locked' if code not in {5, 261, 517, 773} else 'opaque native detail')
    if code is not None:
        error.sqlite_errorcode = code
    events = []
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                events.append('begin')
                raise error
            return super().execute(sql, parameters)

        def rollback(self):
            events.append('rollback')
            return super().rollback()

        def close(self):
            events.append('close')
            return super().close()

    def open_connection(*args, **kwargs):
        if '?mode=rw' in str(args[0]):
            assert kwargs['timeout'] == 1.0
            return connect(*args, **kwargs, factory=Connection)
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', open_connection)
    busy = exception is sqlite3.OperationalError and code in {5, 261, 517, 773}
    with pytest.raises(KernelError if busy else exception) as failed:
        with native.transaction():
            pytest.fail('A rejected BEGIN must not execute its business body')
    if busy:
        assert failed.value.code == 'storage_busy' and failed.value.__cause__ is error
        assert 'opaque native detail' not in str(failed.value)
    else:
        assert failed.value is error
    assert events == ['begin', 'rollback', 'close']


def _durable_bytes(path: Path) -> dict[str, bytes]:
    # SQLite SHM contains transient reader marks, not a state/history record.
    # Keep the main database and the entire committed WAL in this assertion.
    return {item.name: item.read_bytes() for item in path.parent.iterdir()
            if item.is_file() and not item.name.endswith('-shm')}


def test_latest_producer_state_and_wal_are_not_modified_by_preflight(tmp_path: Path) -> None:
    native = Storage.create(tmp_path / 'latest' / 'kernel.sqlite3')
    with native.transaction(write=False):
        # Normal SQLite maintenance while a reader holds its earlier snapshot
        # leaves the latest producer's actual schema/data in a committed WAL.
        # No old protocol, proof or material is fabricated or altered.
        with closing(sqlite3.connect(native.path)) as maintenance:
            maintenance.execute('VACUUM')
        assert Path(str(native.path) + '-wal').stat().st_size > 0
        before = _durable_bytes(native.path)
        assert require_supported_state(native.path) == PROTOCOL
        assert _durable_bytes(native.path) == before


def test_missing_state_and_orphaned_sidecars_are_not_new_storage(tmp_path: Path) -> None:
    missing = tmp_path / 'not-created' / 'kernel.sqlite3'
    with pytest.raises(UnsupportedState, match='unreadable_state'):
        require_supported_state(missing)
    assert not missing.parent.exists()
    # No fabricated WAL content: this is an empty, abandoned location marker.
    orphan = tmp_path / 'kernel.sqlite3-shm'
    orphan.touch()
    with pytest.raises(UnsupportedState, match='uncertain_state'):
        Storage.create(tmp_path / 'kernel.sqlite3')
    assert orphan.read_bytes() == b''
    assert not (tmp_path / 'kernel.sqlite3').exists()


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_last_sqlite_close_during_sidecar_observation_preserves_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    native = Storage.create(tmp_path / 'kernel.sqlite3')
    writer = sqlite3.connect(native.path)
    writer.execute('PRAGMA user_version=7')
    writer.commit()
    sidecars = [Path(str(native.path) + ending) for ending in ('-wal', '-shm')]
    assert all(path.is_file() for path in sidecars)
    target = Path(str(native.path) + suffix)
    original_stat = Path.stat
    closed = False

    def observed_stat(path: Path, *args, **kwargs):
        nonlocal closed
        if path == target and not closed:
            closed = True
            writer.close()
            with pytest.raises(FileNotFoundError):
                original_stat(target, follow_symlinks=False)
        return original_stat(path, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, 'stat', observed_stat)
        assert require_supported_state(native.path) == PROTOCOL
        assert closed
        writer.close()
        with closing(sqlite3.connect(native.path.as_uri() + '?mode=ro', uri=True)) as reader:
            assert reader.execute('PRAGMA user_version').fetchone()[0] == 7
    finally:
        writer.close()


def test_missing_sidecar_at_metadata_read_is_not_missing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Storage.create(tmp_path / 'kernel.sqlite3')
    writer = sqlite3.connect(native.path)
    writer.execute('PRAGMA user_version=7')
    writer.commit()
    target = Path(str(native.path) + '-shm')
    original_stat = Path.stat
    closed = False

    def observe(path: Path, *args, **kwargs):
        nonlocal closed
        if path == target and not closed:
            writer.close()
            closed = True
            with pytest.raises(FileNotFoundError):
                original_stat(target)
        return original_stat(path, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, 'stat', observe)
        assert require_supported_state(native.path) == PROTOCOL
        assert closed and native.path.is_file()
    finally:
        writer.close()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_sidecar_permission_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    native = Storage.create(tmp_path / 'kernel.sqlite3')
    target = Path(str(native.path) + suffix)
    original_stat = Path.stat

    def inaccessible(path: Path, *args, **kwargs):
        if path == target:
            raise PermissionError(errno.EACCES, 'synthetic sidecar access denied', str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', inaccessible)
    with pytest.raises(UnsupportedState, match='unreadable_state') as refused:
        require_supported_state(native.path)
    assert isinstance(refused.value.__cause__, PermissionError)


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'hardlink', 'nonempty_journal'])
def test_unsafe_sidecar_still_refuses_reading_current_state(tmp_path: Path, kind: str) -> None:
    native = Storage.create(tmp_path / 'kernel.sqlite3')
    # Let SQLite normally clean up sidecars left by the read-only preflight.
    with closing(sqlite3.connect(native.path)) as maintenance:
        maintenance.execute('SELECT 1')
    sidecar = Path(str(native.path) + ('-journal' if kind == 'nonempty_journal' else '-shm'))
    if kind == 'symlink':
        sidecar.symlink_to(tmp_path / 'missing-target')
    elif kind == 'directory':
        sidecar.mkdir()
    elif kind == 'hardlink':
        marker = tmp_path / 'empty-marker'
        marker.touch()
        sidecar.hardlink_to(marker)
    else:
        sidecar.write_bytes(b'not an empty journal')
    with pytest.raises(UnsupportedState, match='uncertain_state'):
        require_supported_state(native.path)
