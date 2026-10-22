"""Existing copied-returned API transport, with unchanged v6 producers."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_active import read_copied_returned_import, convert_returned_role_v6
from agent_run_kernel.migration_checkout import seal_checkout
from support.v6_returned_source import returned_managed_source
from test_kernel_migration_original_source import _git_bytes


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
def test_copied_returned_facts_keep_private_transport_and_original_untouched(tmp_path, git_repo, role):
    managed, paths = returned_managed_source(tmp_path, git_repo, role, segmentless_events=True)
    report = json.loads(paths['report'].read_bytes())
    run_id = report['run_id']
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    snapshot = capture_v6(paths['database'], tmp_path / 'source.sqlite3')
    with closing(sqlite3.connect(snapshot.path)) as db:
        db.row_factory = sqlite3.Row
        inv = db.execute('SELECT * FROM work_invocations WHERE attempt_id=?', (report['pending']['attempt_id'],)).fetchone()
        completed = {key: inv[key] for key in ('started_at', 'ended_at', 'status')}
        assert db.execute('SELECT count(*) FROM work_invocation_segments').fetchone()[0] == 0
        completed.update(semantic_attempt=report['pending'], execution_segments=[])
        base = db.execute('SELECT value_sha FROM run_condition_base').fetchone()[0]
    work = managed.state_root / 'worktrees' / run_id / 'ticket-2'
    archive = tmp_path / 'checkout.tar'
    archive_sha, _ = seal_checkout(work, managed.repository_root, archive)
    original_git = LocalGit(managed.repository_root)
    head = original_git.run('rev-parse', 'HEAD', cwd=work).stdout.strip()
    status = original_git.run('status', '--porcelain', cwd=work).stdout.strip().splitlines()
    observation = tmp_path / 'observation.json'
    observation.write_text(json.dumps({
        'backend': 'synchronous scripted Agent; completed event and returned result, no OS process proof',
        'source_sha256': snapshot.sha256, 'profile_sha256': hashlib.sha256(paths['profile'].read_bytes()).hexdigest(),
        'fixture_sha256': hashlib.sha256(paths['github'].read_bytes()).hexdigest(),
        'run_id': run_id, 'role': role, 'job_phase': report['phase'], 'pending_attempt': report['pending'],
        'completed_invocation': completed, 'ticket_checkout_head': head, 'ticket_checkout_status': status,
        'checkout_archive_sha256': archive_sha,
    }))
    private = tmp_path / 'private'
    private.mkdir()
    root, checkout, final, target = private / 'repo', private / 'work' / 'ticket-2', private / 'work' / 'final', private / 'kernel.sqlite3'
    original_git.run('clone', '--no-hardlinks', str(managed.repository_root), str(root))
    git = LocalGit(root)
    git.run('update-ref', 'refs/heads/' + report['run_branch'], base)
    git.run('update-ref', 'refs/heads/' + report['ticket_branch'], head)
    checkout.parent.mkdir()
    git.run('worktree', 'add', '--detach', str(checkout), head)
    with tarfile.open(archive) as saved:
        for member in saved:
            assert member.isfile() and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
            stream = saved.extractfile(member)
            assert stream is not None
            path = checkout / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stream.read())
    private_before = _git_bytes(root), git.run('rev-parse', 'HEAD', cwd=checkout).stdout, git.run('status', '--porcelain', cwd=checkout).stdout
    args = dict(repository_root=root, checkout=checkout, final_checkout=final,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'], observation_snapshot=observation,
        remote_source_sha=base, remote_target_sha=base,
        checkout_archive=archive if role == 'development' else None)
    facts = read_copied_returned_import(snapshot, target, **args)
    subject = facts.subjects[0]
    assert facts.source.mode == 'copied' and facts.source.external_readproof is None
    assert facts.spec.repository_root == str(root)
    assert subject.kind == 'returned' and subject.attempts[-1].role.value == role
    assert subject.resources[0].common_git_dir == root / '.git'
    assert subject.resources[0].before_head == (head + '\n').encode('ascii')
    assert subject.resources[0].checkout_index_sha256 is None
    assert subject.resources[0].checkout_archive_sha256 == (archive_sha if role == 'development' else None)
    assert not target.exists()
    assert private_before == (_git_bytes(root), git.run('rev-parse', 'HEAD', cwd=checkout).stdout,
                              git.run('status', '--porcelain', cwd=checkout).stdout)
    kernel = convert_returned_role_v6(snapshot, target, **args)
    from agent_run_kernel.migration_records import V6RecordReader
    from test_kernel_migration_completed_history import _assert_original_records
    _assert_original_records(snapshot, V6RecordReader(kernel))
    assert kernel.status('ticket:2')['phase'] == {'development': 'development', 'review': 'review', 'publication': 'publish'}[role]
    assert git.run('rev-parse', 'refs/heads/' + report['ticket_branch']).stdout.strip() == base
    if role != 'development':
        assert git.run('rev-parse', f'refs/agent-run-kernel-migration/{run_id}/ticket-2').stdout.strip() == head
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    assert before == (paths['database'].read_bytes(), _git_bytes(managed.repository_root))
