"""One SQLite fault factory; native test scopes are explicit private capabilities."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

import pytest

from agent_run.runtime.process_identity import _process_start_token


def _private_read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError('Fault scope is not a bounded private file')
        return os.read(descriptor, 16385)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class FaultScope:
    manifest: Path
    values: dict

    @classmethod
    def create(cls, directory: Path, database: Path, run: str, action: str):
        directory.mkdir(mode=0o700)
        database = database.resolve(strict=True)
        info = database.stat()
        values = dict(database=str(database), device=info.st_dev, inode=info.st_ino,
                      run=run, action=action, owner=os.getpid(),
                      owner_start=_process_start_token(os.getpid()), nonce=secrets.token_hex(32))
        manifest = directory / 'scope.json'
        payload = json.dumps(values, sort_keys=True).encode()
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
        scope = cls(manifest, values)
        scope.set_active(False)
        return scope, hashlib.sha256(payload).hexdigest()

    @classmethod
    def load(cls, manifest: Path, digest: str, database: Path, run: str, action: str):
        directory = manifest.parent.lstat()
        if (not manifest.is_absolute() or '..' in manifest.parts
                or not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or directory.st_mode & 0o077):
            raise ValueError('Fault scope directory is not private')
        payload = _private_read(manifest)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('Fault scope digest changed')
        values = json.loads(payload)
        if set(values) != {'database', 'device', 'inode', 'run', 'action', 'owner', 'owner_start', 'nonce'}:
            raise ValueError('Unknown fault scope structure')
        if (any(type(values[key]) is not int or values[key] <= 0 for key in ('device', 'inode', 'owner'))
                or any(not isinstance(values[key], str) or not values[key] for key in ('database', 'run', 'action', 'owner_start', 'nonce'))
                or len(values['nonce']) != 64 or any(c not in '0123456789abcdef' for c in values['nonce'])):
            raise ValueError('Invalid fault scope identity')
        scope = cls(manifest, values)
        scope.check(database, run, action)
        scope.active()  # Missing/malformed controls are errors, not silent disarming.
        return scope

    def check(self, database: Path, run: str, action: str):
        if (not database.is_absolute() or str(database) != self.values['database']
                or run != self.values['run'] or action != self.values['action']):
            raise ValueError('Fault scope does not own this database/Run/Action')
        info = database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (self.values['device'], self.values['inode'])):
            raise ValueError('Fault database identity changed')

    def active(self):
        value = json.loads(_private_read(self.manifest.with_suffix('.state.json')))
        if (set(value) != {'nonce', 'active'} or value['nonce'] != self.values['nonce']
                or type(value['active']) is not bool):
            raise ValueError('Invalid fault activation receipt')
        return value['active']

    def set_active(self, value: bool):
        if (type(value) is not bool or os.getpid() != self.values['owner']
                or _process_start_token(os.getpid()) != self.values['owner_start']):
            raise ValueError('Only the original test owner may switch the fault')
        temporary = self.manifest.with_suffix('.new')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(nonce=self.values['nonce'], active=value), stream)
        os.replace(temporary, self.manifest.with_suffix('.state.json'))

    def record(self, event: str, code=None):
        path = self.manifest.with_suffix('.events.jsonl')
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 65536):
                raise ValueError('Fault audit is unavailable or full')
            os.write(descriptor, (json.dumps(dict(event=event, sqlite_code=code, pid=os.getpid())) + '\n').encode())
        finally:
            os.close(descriptor)


@contextmanager
def sqlite_fault(monkeypatch, database: Path, fault: str, *, scope: FaultScope | None = None):
    """Original FULL/COMMIT factory; a native scope only permits commit-interrupt."""
    if fault not in {'full', 'commit-interrupt'} or scope is not None and fault != 'commit-interrupt':
        raise ValueError('Unsupported native fault')
    if scope is not None:
        scope.check(database, scope.values['run'], scope.values['action'])
    connect = sqlite3.connect
    connections, failures = [], []
    closed = set()

    def enabled():
        return scope is None or scope.active()

    class Connection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.add(id(self))

        def execute(self, *args, **kwargs):
            try:
                return super().execute(*args, **kwargs)
            except sqlite3.OperationalError as error:
                failures.append(error)
                raise

        def commit(self):
            interrupt = fault == 'commit-interrupt' and self.in_transaction and self.total_changes and enabled()
            if interrupt:
                self.set_progress_handler(lambda: 1, 1)
            try:
                return super().commit()
            except sqlite3.Error as error:
                failures.append(error)
                if scope is not None:
                    try:
                        scope.record('commit_failed', getattr(error, 'sqlite_errorcode', None))
                    except Exception as unavailable:
                        error.add_note('Fault audit unavailable: ' + type(unavailable).__name__)
                raise
            finally:
                if interrupt:
                    self.set_progress_handler(None, 0)

    def limited(path, *args, **kwargs):
        if str(path).split('?', 1)[0] not in {str(database), database.as_uri()}:
            return connect(path, *args, **kwargs)
        if scope is not None:
            scope.check(database, scope.values['run'], scope.values['action'])
        connection = connect(path, *args, factory=Connection, **kwargs)
        connections.append(connection)
        try:
            if scope is not None:
                if connection.execute('SELECT id FROM runs').fetchall() != [(scope.values['run'],)]:
                    raise ValueError('Fault connection belongs to another Run')
                scope.check(database, scope.values['run'], scope.values['action'])
            if fault == 'full' and 'mode=ro' not in str(path):
                pages = connection.execute('PRAGMA page_count').fetchone()[0]
                assert connection.execute(f'PRAGMA max_page_count={pages}').fetchone()[0] == pages
        except BaseException:
            connection.close()
            raise
        return connection

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', limited)
            yield failures
    finally:
        assert connections
        if scope is not None:
            # Host connections may belong to already-joined Worker threads.
            # Check successful close receipts without disabling SQLite's
            # thread-affinity guard or querying them from the wrong thread.
            assert all(id(connection) in closed for connection in connections)
        else:
            for connection in connections:
                with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                    connection.execute('SELECT 1')
