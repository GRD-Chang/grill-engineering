"""Same managed clone, with real unchanged-v6 local Host exit evidence."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.task_identity import TaskKey
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_completed import convert_completed_ticket_v6
from agent_run_kernel.migration_records import V6RecordReader

from test_kernel_migration_completed_history import _assert_original_records, _completed_source


def _managed_source(tmp_path: Path, git_repo: Path) -> tuple[ManagedWorkspace, dict[str, Path]]:
    managed = ManagedWorkspace.for_repository('example/project')
    managed.root.mkdir(parents=True)
    git_repo.rename(managed.repository_root)
    managed.state_root.mkdir()
    remote = 'https://github.com/example/project.git'
    subprocess.run(['git', 'remote', 'add', 'origin', remote], cwd=managed.repository_root,
                   check=True, capture_output=True, timeout=10)
    (managed.root / '.agent-run-workspace.json').write_text(json.dumps({
        'repository': managed.repository, 'remote_url': remote}) + '\n')
    assert managed.open_path() == managed.repository_root
    paths = _completed_source(tmp_path, managed.repository_root, 2,
                              state_root=managed.state_root, controlled=True)
    return managed, paths


def _git_bytes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_existing_verified_completion_preserves_original_managed_git(tmp_path: Path, git_repo: Path) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    with closing(sqlite3.connect(snapshot.path)) as db:
        db.row_factory = sqlite3.Row
        task = db.execute('SELECT * FROM control_tasks').fetchone()
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
        assert task['task_id'] == TaskKey(managed.repository_root, 'example/project', 1).fingerprint
        assert task['run_id'] == run_id and task['run_state_dir'] == str(managed.state_root)
        executor = db.execute('SELECT * FROM control_executors').fetchone()
        assert executor['status'] == 'exited' and executor['exited_at']
        assert executor['pid'] > 0 and executor['process_start_token']
        action = db.execute('SELECT * FROM control_actions').fetchone()
        assert action['status'] == 'completed' and action['application_observed'] == 1
        assert action['completed_at'] and action['run_id'] == run_id
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        original_controls = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                             for name in ('control_tasks', 'control_actions', 'control_executors',
                                          'control_workers', 'control_action_history', 'control_payloads')}
    before = _git_bytes(managed.repository_root)
    original_database = paths['database'].read_bytes()
    destination = managed.state_root / run_id / 'kernel.sqlite3'
    destination.parent.mkdir(parents=True)
    final = managed.root / 'worktrees' / run_id / 'run-publication'
    kernel = convert_completed_ticket_v6(
        snapshot, destination, git_store=managed.repository_root / '.git',
        repository_root=managed.repository_root, final_checkout=final,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'],
        observation_snapshot=paths['observation'], original_report=paths['report'],
        existing_verified=True)
    assert _git_bytes(managed.repository_root) == before
    assert paths['database'].read_bytes() == original_database
    assert not final.exists()
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT repository_root FROM runs')[0] == str(managed.repository_root)
        assert tx.one("SELECT phase FROM subjects WHERE id='ticket:2'")[0] == 'complete'
        assert tx.one('SELECT executor FROM runs')[0] is None
        for table in ('actions', 'hosts', 'leases', 'retired_executors', 'invocations', 'worker_activity'):
            assert tx.one(f'SELECT count(*) FROM {table}')[0] == 0
    reader = V6RecordReader(kernel)
    for table, count in original_controls.items():
        assert reader.tables()[table]['count'] == count
    _assert_original_records(snapshot, reader)


def test_existing_verified_completion_refuses_replaced_original_uuid(tmp_path: Path, git_repo: Path) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    with closing(sqlite3.connect(snapshot.path)) as db:
        run_id, uuid = db.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchone()
    marker = managed.repository_root / '.git/agent-run-checkout-id'
    replacement = '0' * 32 if uuid != '0' * 32 else '1' * 32
    marker.write_text(replacement + '\n', encoding='ascii')
    before = _git_bytes(managed.repository_root)
    original_database = paths['database'].read_bytes()
    destination = managed.state_root / run_id / 'kernel.sqlite3'
    destination.parent.mkdir()
    with pytest.raises(MigrationRefused, match='source_existing_identity'):
        convert_completed_ticket_v6(
            snapshot, destination, git_store=managed.repository_root / '.git',
            repository_root=managed.repository_root,
            final_checkout=managed.root / 'worktrees' / run_id / 'run-publication',
            profile_snapshot=paths['profile'], github_snapshot=paths['github'],
            observation_snapshot=paths['observation'], original_report=paths['report'],
            existing_verified=True)
    assert not destination.exists()
    assert _git_bytes(managed.repository_root) == before
    assert paths['database'].read_bytes() == original_database
