"""The offline tool discovers original evidence without user-authored exports."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.task_identity import TaskKey
from agent_run_kernel.task_location import read_task, task_path
from agent_run_kernel.migration_records import V6RecordReader
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.kernel import Kernel

from test_kernel_migration_original_source import _git_bytes, _managed_source
from test_kernel_migration_completed_history import _assert_original_records


def test_offline_inspection_collects_original_sql_profile_and_git_without_cutover(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    before_git, source_bytes = _git_bytes(managed.repository_root), paths['database'].read_bytes()
    profile_bytes = paths['profile'].read_bytes()
    destination = tmp_path / 'offline-evidence'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    observed = subprocess.run([
        sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--inspect',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(destination),
    ], env=environment, text=True, capture_output=True, timeout=20)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    run_id = json.loads(paths['profile'].read_text())['run_id']
    task = TaskKey(managed.repository_root, 'example/project', 1)
    assert result['status'] == 'inspection_only'
    assert result['source'] == {'path': str(paths['database']), 'run_id': run_id,
                                'sha256': hashlib.sha256(source_bytes).hexdigest(),
                                'task_fingerprint': task.fingerprint}
    assert result['external_status'] == 'not_read'
    assert result['profile']['source_path'] == str(paths['profile'])
    assert result['profile']['sha256'] == hashlib.sha256(profile_bytes).hexdigest()
    assert (destination / 'source.sqlite3').read_bytes() == source_bytes
    assert (destination / 'profile.json').read_bytes() == profile_bytes
    assert json.loads((destination / 'inspection.json').read_bytes()) == result
    assert result['git']['run_ref'] == f'refs/heads/agent-run/{run_id}/run'
    assert result['git']['run_head'] == json.loads(paths['report'].read_text())['integrated_sha']
    assert _git_bytes(managed.repository_root) == before_git
    assert paths['database'].read_bytes() == source_bytes
    assert paths['profile'].read_bytes() == profile_bytes
    assert read_task(task_path(task)) is None
    assert not (managed.state_root / run_id / 'kernel.sqlite3').exists()


@pytest.mark.parametrize('invalid', ['repository', 'managed-workspace'])
def test_offline_inspection_refuses_invalid_identity_without_traceback_or_evidence(
    tmp_path: Path, invalid: str,
) -> None:
    repository = '.invalid/project' if invalid == 'repository' else 'example/project'
    if invalid == 'managed-workspace':
        managed = ManagedWorkspace.for_repository(repository)
        managed.state_root.mkdir(parents=True)
        managed.repository_root.mkdir()
        (managed.repository_root / '.git').mkdir()
    destination = tmp_path / 'refused-evidence'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    observed = subprocess.run([
        sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--inspect',
        '--repo', repository, '--parent', '1', '--evidence-dir', str(destination),
    ], env=environment, text=True, capture_output=True, timeout=10)
    assert 'Traceback' not in observed.stderr
    if invalid == 'repository':
        assert observed.returncode == 2 and 'owner/repository' in observed.stderr
    else:
        assert observed.returncode == 1
        assert json.loads(observed.stdout)['status'] == 'refused'
    assert not destination.exists()


@pytest.mark.parametrize('mode', ['resume', 'rollback'])
def test_offline_recovery_requires_its_original_operation_before_any_mutation(
    tmp_path: Path, mode: str,
) -> None:
    destination = tmp_path / 'missing-operation'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    observed = subprocess.run([
        sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--' + mode,
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(destination),
    ], env=environment, text=True, capture_output=True, timeout=10)
    assert observed.returncode == 1, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    assert result['status'] == 'refused' and result['code'] == 'source_operation_required'
    assert 'Traceback' not in observed.stderr
    assert not destination.exists()
    managed = ManagedWorkspace.for_repository('example/project')
    assert not managed.root.exists()
    assert read_task(task_path(TaskKey(managed.repository_root, 'example/project', 1))) is None


def test_offline_apply_reconciles_real_old_dispatched_close_without_replay(
    tmp_path: Path, git_repo: Path, *, current_pr_base: bool = False,
    default_observation: str = 'original',
) -> None:
    # The unchanged GhGitHubPublisher must produce the ID through its real
    # command adapter. No SQL field or old returned ownership is rewritten.
    managed, paths = _managed_source(tmp_path, git_repo, actual_github_close=True)
    with closing(sqlite3.connect(paths['database'])) as source:
        run_id, source_status = source.execute('SELECT run_id,status FROM runs').fetchone()
        assert source_status == 'waiting_external'
        assert source.execute('SELECT count(*) FROM work_job_ticket_close_ownership').fetchone()[0] == 0
        actor, binding, sent = source.execute(
            'SELECT value_actor,value_intent_binding,value_dispatch_attempted FROM work_job_ticket_close_intent').fetchone()
        assert sent == 1 and actor == 'fixture-publisher'
        assert source.execute("SELECT count(*) FROM work_evidence_materials WHERE body LIKE '%Unsupported Change Job record field: created_at%'").fetchone()[0] > 0
        assert source.execute('PRAGMA foreign_key_check').fetchall() == []
    service = json.loads((tmp_path / 'close-service.json').read_text())
    event_id = service['events'][0]['id']
    assert service['state'] == 'CLOSED' and type(event_id) is int and event_id == 4101
    if current_pr_base:
        # REST returns the current target ref after the exact proved merge.
        # Keep original SQL, Review base and managed Git untouched.
        service['pr_base_sha'] = json.loads(paths['report'].read_text())['integrated_sha']
        (tmp_path / 'close-service.json').write_text(json.dumps(service))
    if default_observation != 'original':
        external = json.loads(paths['github'].read_text())
        original_default = external['default_head_sha']
        tree = subprocess.run(['git', '-C', str(managed.repository_root), 'rev-parse',
                               original_default + '^{tree}'], check=True, capture_output=True, text=True).stdout.strip()
        commit = ['git', '-C', str(managed.repository_root), 'commit-tree', tree]
        if default_observation == 'append':
            commit.extend(['-p', original_default])
        observed_default = ('f' * 40 if default_observation == 'missing' else subprocess.run(
            commit, input='External default change\n', check=True,
            capture_output=True, text=True).stdout.strip())
        service['remote_default_sha'] = observed_default
        (tmp_path / 'close-service.json').write_text(json.dumps(service))
    assert 'publisher-close-intent:' in service['comments'][0]['body']
    commands = [json.loads(line) for line in (tmp_path / 'close-service-calls.jsonl').read_text().splitlines()]
    assert sum(arguments[:3] == ['issue', 'close', '2'] for arguments in commands) == 1
    source_bytes, before_git = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    destination = tmp_path / 'numeric-event-evidence'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                   'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
                   'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
                   'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
                   'V6_CLOSE_REAL_GIT': shutil.which('git')}
    observed = subprocess.run([
        sys.executable, '-m', 'agent_run_kernel.migration_cutover',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(destination),
    ], env=environment, text=True, capture_output=True, timeout=20)
    if default_observation in {'rewrite', 'missing'}:
        assert observed.returncode == 1, observed.stdout + observed.stderr
        failure = json.loads(observed.stdout)
        expected = ('source_external_default_object' if default_observation == 'missing'
                    else 'source_external_default_ancestry')
        assert failure['status'] == 'refused' and failure['code'] == expected
        assert paths['database'].read_bytes() == source_bytes
        assert _git_bytes(managed.repository_root) == before_git
        assert not (managed.state_root / run_id / 'kernel.sqlite3').exists()
        assert read_task(task_path(TaskKey(managed.repository_root, 'example/project', 1))) is None
        return
    assert observed.returncode == 0, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    assert result['status'] == 'migrated' and result['source']['run_id'] == run_id
    with closing(sqlite3.connect(destination / 'source.sqlite3')) as captured:
        assert captured.execute('SELECT status FROM runs').fetchone()[0] == 'waiting_external'
        assert captured.execute('SELECT count(*) FROM work_job_ticket_close_ownership').fetchone()[0] == 0
    kernel = Kernel(Path(result['target']['database']))
    assert kernel.status('ticket:2')['phase'] == 'complete'
    reader = V6RecordReader(kernel)
    assert reader.row('runs', [run_id])['status'] == 'waiting_external'
    _assert_original_records(capture_v6(destination / 'source.sqlite3', tmp_path / 'record-diff.sqlite3'), reader)
    with kernel.storage.transaction(write=False) as tx:
        closed = tx.one("SELECT receipt_id FROM effects WHERE operation='close'")
        receipt = json.loads(tx.get(closed['receipt_id'], 'ticket:2'))
        assert receipt['raw']['event']['id'] == event_id
        assert receipt['raw']['event']['actor']['login'] == actor
        assert receipt['raw']['close_intent']['intent_binding'] == binding
        assert tx.one("SELECT count(*) FROM resource_cleanup WHERE subject='ticket:2' AND state='pending'")[0] == 1
        assert tx.one("SELECT count(*) FROM effects WHERE operation='workspace'")[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    commands = [json.loads(line) for line in (tmp_path / 'close-service-calls.jsonl').read_text().splitlines()]
    assert sum(arguments[:3] == ['issue', 'close', '2'] for arguments in commands) == 1
    assert (destination / 'source.sqlite3').read_bytes() == source_bytes
    assert _git_bytes(managed.repository_root) == before_git
    if current_pr_base:
        proof = json.loads((destination / 'external-readproof.json').read_text())
        assert proof['pr_before']['base']['sha'] == service['pr_base_sha']
        assert proof['pr_after']['base']['sha'] == service['pr_base_sha']
        with closing(sqlite3.connect(destination / 'source.sqlite3')) as captured:
            historical_base = captured.execute('SELECT value_sha FROM run_condition_base').fetchone()[0]
        assert historical_base != service['pr_base_sha']
        if default_observation != 'original':
            assert proof['remote_heads']['refs/heads/main'] == observed_default
            assert observed_default != historical_base
            assert subprocess.run(['git', '-C', str(managed.repository_root), 'rev-parse', 'refs/heads/main'],
                                  check=True, capture_output=True, text=True).stdout.strip() == historical_base
        with kernel.storage.transaction(write=False) as tx:
            assert {row['base_sha'] for row in tx.rows(
                "SELECT base_sha FROM attempts WHERE role='review'")} == {historical_base}
            saved = tx.one("SELECT material_id FROM history WHERE kind='v6_external_readproof'")
            assert json.loads(tx.get(saved['material_id'], 'ticket:2')) == proof


def test_offline_apply_accepts_proved_merge_as_current_pr_base_without_rewriting_review(
    tmp_path: Path, git_repo: Path,
) -> None:
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(
        tmp_path, git_repo, current_pr_base=True)


def test_offline_default_remote_append_preserves_historical_local_ownership(
    tmp_path: Path, git_repo: Path,
) -> None:
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(
        tmp_path, git_repo, current_pr_base=True, default_observation='append')


@pytest.mark.parametrize('observation', ['rewrite', 'missing'])
def test_offline_default_remote_requires_available_descendant_commit(
    tmp_path: Path, git_repo: Path, observation: str,
) -> None:
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(
        tmp_path, git_repo, current_pr_base=True, default_observation=observation)


def test_offline_cli_resumes_then_returns_unadvanced_original_authority(
    tmp_path: Path, git_repo: Path,
) -> None:
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(tmp_path, git_repo)
    managed = ManagedWorkspace.for_repository('example/project')
    destination = tmp_path / 'numeric-event-evidence'
    operation = json.loads((destination / 'operation.json').read_text())
    inspection = json.loads((destination / 'inspection.json').read_text())
    run_id = inspection['source']['run_id']
    source = managed.state_root / 'state.sqlite3'
    target = managed.state_root / run_id / 'kernel.sqlite3'
    task = TaskKey(managed.repository_root, 'example/project', 1)
    before = source.read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    arguments = [sys.executable, '-m', 'agent_run_kernel.migration_cutover',
                 '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(destination)]
    resumed = subprocess.run(arguments + ['--resume'], env=environment, text=True, capture_output=True, timeout=20)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)['nonce'] == operation['nonce']
    assert (source.read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)) == before
    rolled_back = subprocess.run(arguments + ['--rollback'], env=environment, text=True, capture_output=True, timeout=20)
    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    result = json.loads(rolled_back.stdout)
    assert result == {'status': 'rolled_back', 'nonce': operation['nonce'],
                      'source_database': str(source), 'retained_native_database': str(target)}
    assert read_task(task_path(task)) is None and target.is_file()
    assert _git_bytes(managed.repository_root) == before[2]
    with closing(sqlite3.connect(source)) as restored, closing(sqlite3.connect(destination / 'source.sqlite3')) as sealed:
        assert restored.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == 'sqlite-work-records-v6'
        for (table,) in sealed.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if table != 'schema_meta':
                columns = sealed.execute(f'PRAGMA table_info("{table}")').fetchall()
                keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
                assert keys
                order = ','.join('"' + key + '"' for key in keys)
                assert restored.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall() == sealed.execute(
                    f'SELECT * FROM "{table}" ORDER BY {order}').fetchall()
    commands = [json.loads(line) for line in (tmp_path / 'close-service-calls.jsonl').read_text().splitlines()]
    assert sum(arguments[:3] == ['issue', 'close', '2'] for arguments in commands) == 1


def test_offline_cli_advanced_target_preserves_authority_and_names_rollforward(
    tmp_path: Path, git_repo: Path,
) -> None:
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(tmp_path, git_repo)
    managed = ManagedWorkspace.for_repository('example/project')
    evidence = tmp_path / 'numeric-event-evidence'
    run_id = json.loads((evidence / 'inspection.json').read_text())['source']['run_id']
    native_path = managed.state_root / run_id / 'kernel.sqlite3'
    Kernel(native_path).claim_executor('migration-advanced-cli-executor')
    source = managed.state_root / 'state.sqlite3'
    task = TaskKey(managed.repository_root, 'example/project', 1)
    before = source.read_bytes(), native_path.read_bytes(), _git_bytes(managed.repository_root), read_task(task_path(task))
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    observed = subprocess.run([
        sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--rollback',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(evidence),
    ], env=environment, text=True, capture_output=True, timeout=20)
    assert observed.returncode == 1, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    assert result['status'] == 'refused' and result['code'] == 'native_original_cutover_advanced'
    assert result['next_action'] == 'rollforward'
    assert result['retained_native_database'] == str(native_path)
    assert result['source_database'] == str(source)
    assert result['evidence_dir'] == str(evidence)
    assert (source.read_bytes(), native_path.read_bytes(), _git_bytes(managed.repository_root), read_task(task_path(task))) == before
