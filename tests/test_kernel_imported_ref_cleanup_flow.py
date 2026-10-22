"""Original RunRoot local-ref duty survives the exact native source cutover."""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Effect, EffectResult
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_cutover import migrate_completed_original_v6

from test_kernel_migration_completed_history import _continue_final
from test_kernel_migration_original_cutover import _HistoricalManagedSourcePort
from test_kernel_migration_original_source import _managed_source


class _IndependentRunRefPort(_HistoricalManagedSourcePort):
    """Synthetic remote refs stay independent of the original local RunBranch.

    Native RemoteEffects pushes an object SHA without updating this local
    ref. Keep that physical boundary even if Final needs run_branch work.
    """

    def execute(self, effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation != 'run_branch':
            return super().execute(effect, authorize)
        observed = self.reconcile(effect)
        if observed.disposition == 'prior':
            if authorize is not None:
                authorize()
            assert effect.after_sha is not None
            self._remote_cas(effect.source_ref, effect.before_sha, effect.after_sha)
        return self.reconcile(effect)

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation != 'run_branch':
            return super().reconcile(effect)
        remote = self.remote_refs().get(effect.source_ref)
        return EffectResult('target' if remote == effect.after_sha else
                            'prior' if remote == effect.before_sha else 'third',
                            commit=None if remote is None else self.git.commit(remote))


@pytest.mark.parametrize('foreign_move', [False, True])
def test_original_run_ref_cleanup_uses_imported_local_version_and_clone_identity(
    tmp_path: Path, git_repo: Path, foreign_move: bool,
) -> None:
    _original_cleanup(tmp_path, git_repo, foreign_move=foreign_move)


def test_original_run_ref_cleanup_keeps_local_and_remote_versions_separate_after_final_repair(
    tmp_path: Path, git_repo: Path,
) -> None:
    _original_cleanup(tmp_path, git_repo, foreign_move=False, repair=True)


def _original_cleanup(tmp_path: Path, git_repo: Path, *, foreign_move: bool, repair: bool = False) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    kernel = migrate_completed_original_v6(
        snapshot, repository='example/project', parent_number=1, nonce='c7' * 16,
        profile_snapshot=paths['profile'], github_snapshot=paths['github'],
        observation_snapshot=paths['observation'], original_report=paths['report'])
    task = TaskKey(managed.repository_root, 'example/project', 1)
    admitted = Kernel.open_task(task)
    assert admitted is not None and admitted.storage.path == kernel.storage.path
    report = json.loads(paths['report'].read_text())
    run_id = report['source_identity']['run_id']
    original_oid = report['integrated_sha']
    run_ref = f'refs/heads/agent-run/{run_id}/run'
    git = LocalGit(managed.repository_root)
    assert git.commit(run_ref).sha == original_oid
    uuid = (managed.repository_root / '.git' / 'agent-run-checkout-id').read_text().strip()
    port = _IndependentRunRefPort.create(tmp_path / 'native-remote')
    port.local_root = managed.root / 'worktrees' / run_id
    port.git = git
    original_execute = port.execute
    local_cleanup: list[Effect] = []
    remote_cleanup: list[Effect] = []
    run_branch: list[Effect] = []
    remote_version: list[str] = []
    foreign_oid = git.commit('refs/heads/main').sha
    assert foreign_oid != original_oid

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation == 'run_branch':
            run_branch.append(effect)
        if effect.operation == 'cleanup_local_ref':
            local_cleanup.append(effect)
            assert effect.source_ref == run_ref
            assert effect.before_sha == effect.payload['expected_head'] == original_oid
            assert effect.payload['source_uuid'] == uuid
            assert git.commit(run_ref).sha == original_oid
            remote_version.append(port.remote_refs()[run_ref])
            if repair:
                assert remote_version[-1] != original_oid
            if foreign_move:
                git.run('update-ref', run_ref, foreign_oid, original_oid)
        if effect.operation == 'cleanup_remote_ref':
            remote_cleanup.append(effect)
            assert effect.before_sha == effect.payload['expected_head'] == remote_version[0]
        return original_execute(effect, authorize)

    port.execute = execute
    _continue_final(admitted, port, paths, reviews=('fail', 'pass') if repair else ('pass',),
        expected_kind='paused' if foreign_move else 'complete')
    assert len(local_cleanup) == 1
    if foreign_move:
        assert git.commit(run_ref).sha == foreign_oid
        status = admitted.status(local_cleanup[0].subject)['resource_cleanup']
        assert status['stage'] == 1 and status['state'] == 'pending' and status['outcome'] == 'third'
        assert port.remote_refs()[run_ref] == original_oid
        assert remote_cleanup == []
    else:
        assert git.run('for-each-ref', '--format=%(refname)', run_ref).stdout == ''
        assert admitted.status(local_cleanup[0].subject)['resource_cleanup']['state'] == 'complete'
        assert len(remote_cleanup) == 1 and run_ref not in port.remote_refs()
    if repair:
        assert len(run_branch) == 1 and run_branch[0].after_sha == remote_version[0]
