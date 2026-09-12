from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run import git as git_module
from agent_run.git import GitError, GitRepository


def _git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


@pytest.mark.parametrize('interruption', ['none', 'before_restore', 'after_restore', 'external_head'])
def test_repair_rotation_preserves_candidate_before_retiring_completed_source(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    git = GitRepository(git_repo)
    source = 'agent-run-repair/run-rotation/1'
    successor = 'agent-run-repair/run-rotation/1-job-2'
    publication = git.resolve('main')
    checkout = tmp_path / 'run-repair'
    git.prepare_ticket_checkout(branch=source, base_sha=publication, checkout=checkout)
    (checkout / 'follow-up.txt').write_text('new accepted work\n')
    _git(checkout, 'add', 'follow-up.txt')
    _git(checkout, 'commit', '-m', 'chore(run): candidate 2')
    candidate = git.resolve(source)
    later = _git(git_repo, 'commit-tree', git.resolve(f'{candidate}^{{tree}}'), '-p', candidate, '-m', 'outside work')
    real_run = git_module.run_git
    injected = False

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        if command[1] == 'update-ref' and not injected and interruption != 'none':
            injected = True
            if interruption == 'external_head':
                _git(git_repo, 'update-ref', f'refs/heads/{source}', later, candidate)
            elif interruption == 'before_restore':
                raise OSError('interrupted before source restore')
            else:
                result = real_run(command, **kwargs)
                assert result.returncode == 0
                raise OSError('interrupted after source restore')
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    options = dict(checkout=checkout, current_branch=source, next_branch=successor, candidate_sha=candidate, current_publication_sha=publication)
    if interruption == 'external_head':
        with pytest.raises(GitError):
            git.rotate_run_repair_checkout(**options)
        assert git.resolve(source) == later
    else:
        if interruption != 'none':
            with pytest.raises(OSError, match='interrupted'):
                git.rotate_run_repair_checkout(**options)
        git.rotate_run_repair_checkout(**options)
        assert git.resolve(source) == publication
    assert git.resolve(successor) == candidate
    assert git.checkout_head(checkout) == candidate
    assert (checkout / 'follow-up.txt').read_text() == 'new accepted work\n'
