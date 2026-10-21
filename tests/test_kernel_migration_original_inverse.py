"""Rollback transfers the untouched original authority, never snapshot bytes."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import traceback

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import migration_cutover
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.model import KernelError
from agent_run_kernel.task_location import bind_task, locked_task, read_task, task_path

from test_kernel_migration_original_source import _git_bytes, _managed_source


def test_original_inverse_releases_only_unadvanced_matching_task_authority(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    with closing(sqlite3.connect(snapshot.path)) as db:
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
        protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    task = TaskKey(managed.repository_root, 'example/project', 1)
    nonce = '9d' * 16
    kernel = migration_cutover.migrate_completed_original_v6(
        snapshot, repository='example/project', parent_number=1, nonce=nonce,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'],
        observation_snapshot=paths['observation'], original_report=paths['report'])
    assert read_task(task_path(task)) == (run_id, kernel.storage.path)
    before_git = _git_bytes(managed.repository_root)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT executor FROM runs')[0] is None
        assert all(tx.one(f'SELECT count(*) FROM {name}')[0] == 0
                   for name in ('actions', 'hosts', 'invocations', 'leases'))
    rollback = getattr(migration_cutover, 'rollback_original_v6')
    result = rollback(snapshot, repository='example/project', parent_number=1, nonce=nonce)
    assert result['status'] == 'rolled_back' and result['nonce'] == nonce
    assert result['source_database'] == str(paths['database'])
    assert result['retained_native_database'] == str(kernel.storage.path)
    assert read_task(task_path(task)) is None
    assert _git_bytes(managed.repository_root) == before_git
    assert kernel.storage.path.is_file() and snapshot.path.is_file()
    with closing(sqlite3.connect(paths['database'])) as current, closing(sqlite3.connect(snapshot.path)) as original:
        assert current.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == protocol
        for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = original.execute('PRAGMA table_info(' + quoted + ')').fetchall()
            keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = ' ORDER BY ' + ','.join('"' + key.replace('"', '""') + '"' for key in keys) if keys else ''
            assert current.execute('SELECT * FROM ' + quoted + order).fetchall() == original.execute(
                'SELECT * FROM ' + quoted + order).fetchall()
    with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
        Kernel(kernel.storage.path).claim_executor('stale-native-after-rollback')
    # The unchanged old Task control now accepts a fresh original Action.
    old_environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
        str(tmp_path / 'unchanged-02' / name) for name in ('project/src', 'project/tests'))}
    resumed = subprocess.run([sys.executable, '-c', '''
import json,sys
from pathlib import Path
from agent_run.task_control import TaskControlStore
from agent_run.task_identity import TaskKey
request=json.load(sys.stdin)
control=TaskControlStore(Path(request['state']))
task=TaskKey(Path(request['repository']),'example/project',1)
claim=control.claim_action(task,kind='run',payload={'parent':1})
assert claim.action_id and claim.action
print('original-v6-authority-resumed')
'''], input=json.dumps({'state': str(managed.state_root), 'repository': str(managed.repository_root)}),
        env=old_environment, text=True, capture_output=True, timeout=15)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert resumed.stdout.strip() == 'original-v6-authority-resumed'


@pytest.mark.parametrize('released', [False, True])
def test_original_inverse_recovers_same_nonce_after_real_boundary_exit(
    tmp_path: Path, git_repo: Path, released: bool,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    nonce = 'a1' * 16
    kernel = migration_cutover.migrate_completed_original_v6(
        snapshot, repository='example/project', parent_number=1, nonce=nonce,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'],
        observation_snapshot=paths['observation'], original_report=paths['report'])
    task = TaskKey(managed.repository_root, 'example/project', 1)
    before_git = _git_bytes(managed.repository_root)
    arguments = dict(repository='example/project', parent_number=1, nonce=nonce)
    pid = os.fork()
    if pid == 0:
        original_withdraw, original_retire = migration_cutover._withdraw_original_pointer, migration_cutover._retire_original

        def withdraw(*args, **kwargs):
            original_withdraw(*args, **kwargs)
            if not released:
                os._exit(81)

        def retire(*args, **kwargs):
            original_retire(*args, **kwargs)
            if released and kwargs.get('release'):
                os._exit(82)

        migration_cutover._withdraw_original_pointer = withdraw
        migration_cutover._retire_original = retire
        try:
            migration_cutover.rollback_original_v6(snapshot, **arguments)
        except BaseException:
            (tmp_path / 'inverse-child-error.txt').write_text(traceback.format_exc())
            os._exit(89)
        os._exit(88)
    ready, descriptor = False, None
    try:
        descriptor = os.pidfd_open(pid)
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            ready = bool(selector.select(15))
        assert ready, 'inverse child did not exit at its selected durable boundary'
        waited, status = os.waitpid(pid, 0)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    error = tmp_path / 'inverse-child-error.txt'
    assert waited == pid and os.WIFEXITED(status), status
    assert os.WEXITSTATUS(status) == (82 if released else 81), error.read_text() if error.exists() else status
    assert read_task(task_path(task)) is None
    with closing(sqlite3.connect(paths['database'])) as current, closing(sqlite3.connect(snapshot.path)) as original:
        current_protocol = current.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
        old_protocol = original.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
        assert (current_protocol == old_protocol) is released
    source_before, target_before = paths['database'].read_bytes(), kernel.storage.path.read_bytes()
    with pytest.raises((MigrationRefused, KernelError)):
        migration_cutover.rollback_original_v6(snapshot, **dict(arguments, nonce='a2' * 16))
    assert paths['database'].read_bytes() == source_before
    assert kernel.storage.path.read_bytes() == target_before
    assert read_task(task_path(task)) is None
    assert _git_bytes(managed.repository_root) == before_git
    result = migration_cutover.rollback_original_v6(snapshot, **arguments)
    assert result['status'] == 'rolled_back'
    with closing(sqlite3.connect(paths['database'])) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == old_protocol
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_original_rollback_started'")[0] == 1
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_original_rollback_complete'")[0] == 1
    source_before, target_before = paths['database'].read_bytes(), kernel.storage.path.read_bytes()
    assert migration_cutover.rollback_original_v6(snapshot, **arguments) == result
    assert paths['database'].read_bytes() == source_before
    assert kernel.storage.path.read_bytes() == target_before
    assert read_task(task_path(task)) is None
    assert _git_bytes(managed.repository_root) == before_git


@pytest.mark.parametrize('changed', ['native-advanced', 'foreign-pointer'])
def test_original_inverse_preserves_both_authorities_when_rollback_is_unproved(
    tmp_path: Path, git_repo: Path, changed: str,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    nonce = 'b1' * 16
    kernel = migration_cutover.migrate_completed_original_v6(
        snapshot, repository='example/project', parent_number=1, nonce=nonce,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'],
        observation_snapshot=paths['observation'], original_report=paths['report'])
    task = TaskKey(managed.repository_root, 'example/project', 1)
    if changed == 'native-advanced':
        kernel.claim_executor('actual-native-advance')
    else:
        with locked_task(task) as location:
            bind_task(location, 'foreign-native-run', tmp_path / 'foreign.sqlite3')
    before_pointer = task_path(task).read_bytes()
    before_source, before_native = paths['database'].read_bytes(), kernel.storage.path.read_bytes()
    before_git = _git_bytes(managed.repository_root)
    with pytest.raises(MigrationRefused):
        migration_cutover.rollback_original_v6(snapshot, repository='example/project',
                                                parent_number=1, nonce=nonce)
    assert paths['database'].read_bytes() == before_source
    assert kernel.storage.path.read_bytes() == before_native
    assert task_path(task).read_bytes() == before_pointer
    assert _git_bytes(managed.repository_root) == before_git
    assert snapshot.path.is_file()
