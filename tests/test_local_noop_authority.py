"""Actual local no-write outcomes still require fresh admission.

These adapter contracts supply explicit Effects. The scoped Core consumers own
receipt/charge transitions; the three delivery journeys own their real wiring.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.write_authority import WriteAuthority
from agent_run.errors import KernelError
from support.remote_effects import publication


def no_write_effect(git_repo, tmp_path, operation):
    git, local, original = publication(git_repo, tmp_path)
    effect = replace(original, operation=operation, payload={**original.payload,
        'source_sha': original.base.sha, 'reuse_sha': original.base.sha,
        'reviewed_commit': original.base.sha, 'expected_head': original.base.sha,
        'branch_ref': original.source_ref})
    if operation in {'validation_cleanup', 'cleanup_worktree'}:
        git.run('worktree', 'remove', effect.payload['checkout'])
    return git, local, effect


@pytest.mark.parametrize('operation', ['integrate', 'candidate', 'publication_commit',
    'validation_cleanup', 'cleanup_worktree', 'cleanup_local_ref'])
def test_original_no_write_result_requires_fresh_admission(git_repo, tmp_path, operation):
    git, local, effect = no_write_effect(git_repo, tmp_path, operation)
    observed = local.reconcile(effect)
    assert observed.disposition == 'target'
    if operation == 'candidate':
        assert observed.raw['no_change'] is True and observed.commit.sha == effect.before_sha
    elif operation in {'integrate', 'publication_commit'}:
        assert observed.commit == effect.base
    def revoked():
        raise KernelError('executor_fenced')
    with pytest.raises(KernelError, match='executor_fenced'):
        local.execute(effect, WriteAuthority(lambda: revoked))
    admissions = []
    assert local.execute(effect, WriteAuthority(lambda: lambda: admissions.append(effect.effect_id))) == observed
    assert admissions == [effect.effect_id]
    assert local.reconcile(effect) == observed
    assert git.commit('HEAD') == effect.base
    if operation == 'cleanup_local_ref':
        for field in ('owned_root', 'common_git_dir'):
            foreign = replace(effect, payload={**effect.payload, field: str(tmp_path / 'foreign')})
            assert local.execute(foreign, WriteAuthority(lambda: lambda: pytest.fail('foreign resource cannot dispatch'))).disposition == 'third'


def test_noop_integration_preserves_dirty_work_added_during_admission(git_repo, tmp_path):
    _git, local, effect = no_write_effect(git_repo, tmp_path, 'integrate')
    changed = Path(effect.payload['checkout']) / 'concurrent-work.txt'
    def prepare():
        changed.write_text('Preserve this work.\n')
        return lambda: pytest.fail('changed checkout must never dispatch')
    with pytest.raises(KernelError, match='local_effect_boundary_changed'):
        local.execute(effect, WriteAuthority(prepare))
    assert changed.read_text() == 'Preserve this work.\n'
