from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run import git as git_module
from agent_run import github_publish
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.git import GitRepository
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore


def _git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def _completed_ticket(git_repo: Path) -> tuple[GitRepository, StateStore, dict[str, Any], dict[str, Any]]:
    git = GitRepository(git_repo)
    states = StateStore(git_repo / '.agent-run')
    branch = 'agent-run/run-cleanup/ticket-2'
    base = git.resolve('main')
    publication = _git(git_repo, 'commit-tree', git.resolve('main^{tree}'), '-p', base, '-m', 'publication source')
    integrated = _git(git_repo, 'commit-tree', git.resolve('main^{tree}'), '-p', base, '-m', 'separate squash result')
    git.ensure_run_branch(branch, publication)
    job = {'ticket_number': 2, 'ticket_branch': branch, 'phase': 'completed', 'publication_sha': publication, 'integrated_sha': integrated}
    state = {'run_id': 'run-cleanup', 'status': 'completed', 'ticket_jobs': {'2': job}}
    return git, states, state, job


def test_remote_cleanup_preserves_commit_added_at_delete_dispatch(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    branch = job['ticket_branch']
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', str(remote))
    _git(git_repo, 'remote', 'add', 'origin', str(remote))
    _git(git_repo, 'push', 'origin', f'{branch}:refs/heads/{branch}')
    later = _git(git_repo, 'commit-tree', git.resolve('main^{tree}'), '-p', job['publication_sha'], '-m', 'new work')
    _git(git_repo, 'push', 'origin', f'{later}:refs/heads/keep-object')
    real_write = github_publish.run_write_command
    injected = False

    def write(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        if not injected:
            injected = True
            _git(remote, 'update-ref', f'refs/heads/{branch}', later, job['publication_sha'])
        return real_write(command, **kwargs)

    monkeypatch.setattr(github_publish, 'run_write_command', write)
    result = DeliveryCleanupEngine(git=git, states=states, github=GhGitHubPublisher('example/project', git)).complete_ticket(state, job)
    assert injected
    assert result['status'] == 'completed'
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert _git(remote, 'rev-parse', f'refs/heads/{branch}') == later
    assert result['delivery_cleanup']['items'][branch]['expected_head_sha'] == job['publication_sha']


def test_local_cleanup_preserves_commit_added_at_delete_dispatch(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    branch = job['ticket_branch']
    later = _git(git_repo, 'commit-tree', git.resolve('main^{tree}'), '-p', job['publication_sha'], '-m', 'new work')
    real_run = git_module.run_git
    injected = False

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        deleting = command[1:3] in (['branch', '-D'], ['update-ref', '-d'])
        if deleting and not injected:
            injected = True
            _git(git_repo, 'update-ref', f'refs/heads/{branch}', later, job['publication_sha'])
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert injected
    assert result['status'] == 'completed'
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(branch) == later


def test_cleanup_preserves_new_committed_work_and_its_checkout(git_repo: Path) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    branch = job['ticket_branch']
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=branch, base_sha=job['publication_sha'], checkout=checkout)
    (checkout / 'new-work.txt').write_text('new work must survive\n')
    _git(checkout, 'add', 'new-work.txt')
    _git(checkout, 'commit', '-m', 'new work after completion')
    later = git.resolve(branch)

    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)

    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(branch) == later
    assert (checkout / 'new-work.txt').read_text() == 'new work must survive\n'


@pytest.mark.parametrize('kind', ['ticket', 'repair', 'repairs', 'parent', 'run'])
def test_every_completed_cleanup_uses_source_head_instead_of_merge_result(
    git_repo: Path, kind: str,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    branch = job['ticket_branch']
    job['repair_branch'] = branch
    job['parent_branch'] = branch
    state['parent_job'] = job
    state['run_branch'] = branch
    state['run_publication'] = {
        'phase': 'merged', 'parent_closed': True,
        'record': {'pr_head_sha': job['publication_sha']},
        'integrated_sha': job['integrated_sha'],
    }
    engine = DeliveryCleanupEngine(git=git, states=states)
    if kind == 'ticket':
        result = engine.complete_ticket(state, job)
    elif kind == 'repair':
        result = engine.complete_run_repair(state, job)
    elif kind == 'repairs':
        result = engine.complete_run_repairs(state, [job])
    elif kind == 'parent':
        result = engine.complete_parent(state)
    else:
        result = engine.complete_final_run(state)
    assert result['delivery_cleanup']['status'] == 'completed'
    assert result['delivery_cleanup']['items'][branch]['expected_head_sha'] == job['publication_sha']
    assert git.managed_branch_head(branch) is None


@pytest.mark.parametrize('new_work', ['tracked', 'untracked'])
def test_worktree_delete_rechecks_new_unsaved_work(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, new_work: str,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    real_run = git_module.run_git
    injected = False
    filename = 'README.md' if new_work == 'tracked' else 'unsaved.txt'

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        if command[1:3] == ['worktree', 'remove'] and not injected:
            injected = True
            (checkout / filename).write_text('new unsaved work\n')
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert injected
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(job['ticket_branch']) == job['publication_sha']
    assert (checkout / filename).read_text() == 'new unsaved work\n'


def test_local_ref_read_error_does_not_mean_already_deleted(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    real_run = git_module.run_git

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == 'for-each-ref':
            return subprocess.CompletedProcess(command, 128, '', 'ref database unavailable')
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(job['ticket_branch']) == job['publication_sha']


def test_missing_completion_source_head_does_not_authorize_live_ref(git_repo: Path) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    publication = job.pop('publication_sha')
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(job['ticket_branch']) == publication


@pytest.mark.parametrize('outcome', ['missing', 'deleted_response_lost', 'unchanged_response_lost', 'changed_response_lost', 'read_error'])
def test_remote_cleanup_reconciles_unknown_results_against_original_head(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    branch = job['ticket_branch']
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', str(remote))
    _git(git_repo, 'remote', 'add', 'origin', str(remote))
    _git(git_repo, 'push', 'origin', f'{branch}:refs/heads/{branch}')
    later = _git(git_repo, 'commit-tree', git.resolve('main^{tree}'), '-p', job['publication_sha'], '-m', 'later commit')
    _git(git_repo, 'push', 'origin', f'{later}:refs/heads/keep-object')
    if outcome == 'missing':
        _git(remote, 'update-ref', '-d', f'refs/heads/{branch}')
    real_write = github_publish.run_write_command
    real_read = github_publish.run_read_command
    dispatched: list[list[str]] = []

    def write(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        dispatched.append(command)
        if outcome != 'unchanged_response_lost':
            applied = real_write(command, **kwargs)
            assert applied.returncode == 0
        if outcome == 'changed_response_lost':
            _git(remote, 'update-ref', f'refs/heads/{branch}', later)
        return subprocess.CompletedProcess(command, 1, '', 'response lost')

    def read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if outcome == 'read_error':
            return subprocess.CompletedProcess(command, 128, '', 'remote read unavailable')
        return real_read(command, **kwargs)

    monkeypatch.setattr(github_publish, 'run_write_command', write)
    monkeypatch.setattr(github_publish, 'run_read_command', read)
    engine = DeliveryCleanupEngine(git=git, states=states, github=GhGitHubPublisher('example/project', git))
    result = engine.complete_ticket(state, job)
    complete = outcome in {'missing', 'deleted_response_lost'}
    assert result['status'] == 'completed'
    assert result['delivery_cleanup']['status'] == ('completed' if complete else 'cleanup_pending')
    item = result['delivery_cleanup']['items'][branch]
    assert item['expected_head_sha'] == job['publication_sha']
    actual = _git(remote, 'for-each-ref', '--format=%(objectname)', f'refs/heads/{branch}')
    assert actual == ('' if complete else later if outcome == 'changed_response_lost' else job['publication_sha'])
    if outcome in {'missing', 'read_error'}:
        assert not dispatched
    if outcome == 'changed_response_lost':
        assert len(dispatched) == 1
    if outcome == 'unchanged_response_lost':
        # A later retry must not adopt a newly observed head as new authority.
        _git(remote, 'update-ref', f'refs/heads/{branch}', later)
        attempts = len(dispatched)
        resumed = engine.complete_ticket(state, job)
        assert resumed['delivery_cleanup']['status'] == 'cleanup_pending'
        assert len(dispatched) == attempts
        assert _git(remote, 'rev-parse', f'refs/heads/{branch}') == later


def test_local_cleanup_is_idempotent_after_ref_is_already_missing(git_repo: Path) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    _git(git_repo, 'update-ref', '-d', f"refs/heads/{job['ticket_branch']}")
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'completed'


def test_cleanup_preserves_work_committed_between_preflight_and_worktree_removal(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    real_run = git_module.run_git
    commit_result: subprocess.CompletedProcess[str] | None = None

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal commit_result
        if command[1:3] == ['worktree', 'remove'] and commit_result is None:
            (checkout / 'after-preflight.txt').write_text('work after head preflight\n')
            _git(checkout, 'add', 'after-preflight.txt')
            commit_result = subprocess.run(
                ['git', 'commit', '-m', 'new work at cleanup boundary'], cwd=checkout,
                text=True, capture_output=True,
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert commit_result is not None
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert (checkout / 'after-preflight.txt').read_text() == 'work after head preflight\n'
    if commit_result.returncode == 0:
        assert git.resolve(job['ticket_branch']) != job['publication_sha']
    else:
        # A prepared Git ref transaction may reject the late commit; its staged
        # work must then remain available in the original checkout.
        assert 'lock' in commit_result.stderr
        assert git.resolve(job['ticket_branch']) == job['publication_sha']
        assert _git(checkout, 'diff', '--cached', '--name-only') == 'after-preflight.txt'


def test_failed_worktree_removal_releases_git_verification_lock(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    real_run = git_module.run_git

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ['worktree', 'remove']:
            return subprocess.CompletedProcess(command, 1, '', 'worktree busy')
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    (checkout / 'retained.txt').write_text('continue after failed cleanup\n')
    _git(checkout, 'add', 'retained.txt')
    _git(checkout, 'commit', '-m', 'ref lock was released')
    assert git.resolve(job['ticket_branch']) != job['publication_sha']


def test_ref_advancing_before_git_prepare_keeps_its_worktree(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import git_cleanup_lock

    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    real_popen = subprocess.Popen
    injected = False

    def popen(command: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        nonlocal injected
        if '--stdin' in command and not injected:
            injected = True
            (checkout / 'before-prepare.txt').write_text('committed before prepare\n')
            _git(checkout, 'add', 'before-prepare.txt')
            _git(checkout, 'commit', '-m', 'new commit before prepare')
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(git_cleanup_lock.subprocess, 'Popen', popen)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert injected
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert git.resolve(job['ticket_branch']) != job['publication_sha']
    assert (checkout / 'before-prepare.txt').read_text() == 'committed before prepare\n'


def test_cleanup_prepare_timeout_releases_ref_lock_and_reaps_git(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from agent_run import git_cleanup_lock
    from agent_run.git import GitError

    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    marker = git_repo / '.git' / 'hook-prepared'
    hook = git_repo / '.git' / 'hooks' / 'reference-transaction'
    hook.write_text(
        f'#!{sys.executable}\n'
        'import signal, sys\nfrom pathlib import Path\n'
        'if sys.argv[1] == "prepared":\n'
        f'    Path({str(marker)!r}).write_text("prepared")\n'
        '    print("fixture preparation is waiting", flush=True)\n'
        '    signal.pause()\n'
    )
    hook.chmod(0o755)
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        # The deadline advances only after the real hook proves Git has
        # acquired its ref lock. No scheduler-sensitive short sleep is used.
        return 0.0 if calls == 1 or not marker.exists() else 10.0

    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[Any]] = []

    def popen(command: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = real_popen(command, *args, **kwargs)
        if '--stdin' in command:
            processes.append(process)
        return process

    monkeypatch.setattr(git_cleanup_lock, 'monotonic', clock)
    monkeypatch.setattr(git_cleanup_lock.subprocess, 'Popen', popen)
    try:
        with pytest.raises(GitError, match='timed out preparing'):
            git.remove_completed_worktree(checkout, branch=job['ticket_branch'], expected_head_sha=job['publication_sha'])
        assert marker.exists()
        assert processes and all(process.poll() is not None for process in processes)
        assert checkout.exists()
        lock_path = Path(_git(git_repo, 'rev-parse', '--git-path', f"refs/heads/{job['ticket_branch']}.lock"))
        if not lock_path.is_absolute():
            lock_path = git_repo / lock_path
        assert not lock_path.exists()
        head_lock = Path(_git(checkout, 'rev-parse', '--git-path', 'HEAD.lock'))
        assert not head_lock.exists()
    finally:
        hook.unlink()
    _git(git_repo, 'update-ref', f"refs/heads/{job['ticket_branch']}", job['publication_sha'], job['publication_sha'])


@pytest.mark.parametrize('replacement', ['detached', 'same_sha_branch'])
def test_completed_development_cleanup_preserves_changed_checkout_head_identity(
    git_repo: Path, replacement: str,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    if replacement == 'detached':
        _git(checkout, 'checkout', '--detach', job['publication_sha'])
    else:
        _git(checkout, 'switch', '-c', 'maintainer/same-sha')
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert checkout.exists()
    assert git.resolve(job['ticket_branch']) == job['publication_sha']


def test_cleanup_prevents_detach_and_commit_after_head_preflight(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'ticket-2'
    git.prepare_ticket_checkout(branch=job['ticket_branch'], base_sha=job['publication_sha'], checkout=checkout)
    real_run = git_module.run_git
    detached: subprocess.CompletedProcess[str] | None = None
    committed: subprocess.CompletedProcess[str] | None = None

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal detached, committed
        if command[1:3] == ['worktree', 'remove'] and detached is None:
            detached = subprocess.run(['git', 'checkout', '--detach'], cwd=checkout, text=True, capture_output=True)
            (checkout / 'late-detached-work.txt').write_text('preserve this work\n')
            _git(checkout, 'add', 'late-detached-work.txt')
            committed = subprocess.run(['git', 'commit', '-m', 'late detached work'], cwd=checkout, text=True, capture_output=True)
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, 'run_git', run)
    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)
    assert result['delivery_cleanup']['status'] == 'cleanup_pending'
    assert (checkout / 'late-detached-work.txt').read_text() == 'preserve this work\n'
    assert detached is not None and detached.returncode != 0
    assert committed is not None and committed.returncode != 0
    assert _git(checkout, 'symbolic-ref', 'HEAD') == f"refs/heads/{job['ticket_branch']}"


def test_final_run_cleanup_accepts_its_native_detached_publication_checkout(git_repo: Path) -> None:
    git, states, state, job = _completed_ticket(git_repo)
    checkout = states.root / 'worktrees' / state['run_id'] / 'run-publication'
    git.prepare_validation_checkout(head_sha=job['publication_sha'], checkout=checkout)
    state['run_branch'] = job['ticket_branch']
    state['run_publication'] = {
        'phase': 'merged', 'parent_closed': True,
        'record': {'pr_head_sha': job['publication_sha']},
        'integrated_sha': job['integrated_sha'],
    }
    result = DeliveryCleanupEngine(git=git, states=states).complete_final_run(state)
    assert result['delivery_cleanup']['status'] == 'completed'
    assert not checkout.exists()
    assert git.managed_branch_head(job['ticket_branch']) is None
