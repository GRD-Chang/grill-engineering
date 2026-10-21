"""A real old dispatched-close checkpoint retains its exact Ticket resources."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from agent_run.task_identity import TaskKey
from agent_run_kernel import Effect, EffectResult, Kernel
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.migration_records import V6RecordReader

from test_kernel_imported_ref_cleanup_flow import _IndependentRunRefPort
from test_kernel_migration_completed_history import _continue_final
from test_kernel_migration_original_source import _managed_source


class _OriginalTicketPort(_IndependentRunRefPort):
    """Match Executor's per-effect root for the old Ticket and native Final."""

    def execute(self, effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation in {'cleanup_worktree', 'cleanup_local_ref'}:
            return LocalEffects(self.git, owned_root=Path(effect.payload['owned_root'])).execute(
                effect, authorize or (lambda: None))
        return super().execute(effect, authorize)

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation in {'cleanup_worktree', 'cleanup_local_ref'}:
            return LocalEffects(self.git, owned_root=Path(effect.payload['owned_root'])).reconcile(effect)
        return super().reconcile(effect)


def test_dispatched_close_import_retires_original_ticket_resources_before_native_final(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo, actual_github_close=True)
    report = json.loads(paths['report'].read_text())
    run_id = report['source_identity']['run_id']
    prior_pr = report['source_identity']['pull_request']
    publication_oid = prior_pr['head_sha']
    ticket_ref = 'refs/heads/' + prior_pr['branch']
    checkout = managed.state_root / 'worktrees' / run_id / 'ticket-2'
    git = LocalGit(managed.repository_root)
    assert checkout.is_dir() and git.commit(ticket_ref).sha == publication_oid
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == publication_oid
    evidence = tmp_path / 'actual-original-evidence'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                   'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
                   'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
                   'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
                   'V6_CLOSE_REAL_GIT': shutil.which('git')}
    observed = subprocess.run([sys.executable, '-m', 'agent_run_kernel.migration_cutover',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(evidence)],
        env=environment, text=True, capture_output=True, timeout=20)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    assert result['status'] == 'migrated'
    kernel = Kernel.open_task(TaskKey(managed.repository_root, 'example/project', 1))
    assert kernel is not None and str(kernel.storage.path) == result['target']['database']
    reader = V6RecordReader(kernel)
    assert reader.row('runs', [run_id])['status'] == 'waiting_external'
    assert reader.tables()['work_job_ticket_close_ownership']['count'] == 0
    assert reader.row('work_jobs', [run_id, 'ticket:2:generation:1'])['stage'] == 'merged'
    with kernel.storage.transaction(write=False) as tx:
        close = tx.one("SELECT receipt_id FROM effects WHERE subject='ticket:2' AND operation='close'")
        imported_close = json.loads(tx.get(close['receipt_id'], 'ticket:2'))
        assert imported_close['raw']['event']['id'] == 4101
        assert not tx.rows("SELECT id FROM effects WHERE operation='workspace'")
        assert not tx.rows('SELECT id FROM invocations')
        origin = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_original_cutover'")
        uuid = json.loads(tx.get(origin['material_id'], origin['subject']))['source_uuid']
    port = _OriginalTicketPort.create(tmp_path / 'independent-native-remote')
    port.local_root = managed.root / 'worktrees' / run_id
    port.git = git
    original_execute = port.execute
    ticket_cleanups: list[str] = []

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.subject == 'ticket:2':
            assert effect.operation in {'cleanup_worktree', 'cleanup_local_ref', 'cleanup_remote_ref'}
            ticket_cleanups.append(effect.operation)
            assert effect.before_sha == effect.payload['expected_head'] == publication_oid
            if effect.operation != 'cleanup_remote_ref':
                assert effect.payload.get('source_uuid') == uuid
                assert effect.payload['checkout'] == str(checkout)
                assert effect.payload['owned_root'] == str(checkout.parent)
        return original_execute(effect, authorize)

    port.execute = execute
    _continue_final(kernel, port, paths, imported_close=imported_close, retire_ticket=True)
    assert ticket_cleanups == ['cleanup_worktree', 'cleanup_local_ref', 'cleanup_remote_ref']
    assert not checkout.exists() and git.run('for-each-ref', '--format=%(refname)', ticket_ref).stdout == ''
    assert ticket_ref not in port.remote_refs()
    assert kernel.status('ticket:2')['resource_cleanup']['state'] == 'complete'
    assert reader.row('runs', [run_id])['status'] == 'waiting_external'
    assert reader.tables()['work_job_ticket_close_ownership']['count'] == 0
    commands = [json.loads(line) for line in (tmp_path / 'close-service-calls.jsonl').read_text().splitlines()]
    assert sum(arguments[:3] == ['issue', 'close', '2'] for arguments in commands) == 1
