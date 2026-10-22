"""Authenticate real unfinished source resources without reconstructing Effects."""
from pathlib import Path

import pytest

from agent_run_kernel import migration_run_ownership as ownership
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import KernelError
from test_kernel_idle_prebuilt import idle_copy
from test_kernel_prebuilt_actual_integration_review import installed, partial_actual_workspace


@pytest.fixture(autouse=True)
def unchanged_source_bytes(idle_copy):
    snapshot = idle_copy[0]
    before = snapshot.source.read_bytes(), snapshot.path.read_bytes()
    yield
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes()) == before


def test_real_partial_resource_authenticates_original_h0_and_keeps_h1(idle_copy, monkeypatch):
    kernel, _execution, runner, _ports, git, effect, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    resource = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert resource.head_sha == h1 != h0
    assert resource.prebuilt_workspace == effect.payload['prebuilt_workspace']
    calls = []
    guarded = LocalGit(git.root, lambda: calls.append('write authority'))
    guarded.env = git.env.copy()
    before = git.run('show-ref').stdout, git.run('worktree', 'list', '--porcelain').stdout
    assert ownership.authenticate_prebuilt_resource(guarded, resource) == resource.prebuilt_workspace
    assert calls == []
    assert (git.run('show-ref').stdout, git.run('worktree', 'list', '--porcelain').stdout) == before
    assert git.run('rev-parse', 'HEAD', cwd=Path(resource.checkout)).stdout.strip() == h0
    assert runner.run_once().kind == 'progress'
    assert runner.run_once().kind == 'progress'
    completed = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert completed.head_sha == h1 and completed.prebuilt_workspace is None
    assert ownership.authenticate_prebuilt_resource(guarded, completed) is None
    assert calls == []


def test_explicit_dirty_resource_identity_survives_normal_worktree_then_ref_retirement(idle_copy, monkeypatch):
    kernel, _execution, _runner, _ports, git, effect, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    resource = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    checkout = Path(resource.checkout)
    dirty = checkout / 'uncommitted.txt'
    dirty.write_text('Preserve until explicit discard')
    with pytest.raises(KernelError, match='foreign_local_checkout'):
        ownership.authenticate_prebuilt_resource(git, resource)
    assert ownership.authenticate_prebuilt_resource(git, resource, allow_dirty=True) == resource.prebuilt_workspace
    assert dirty.read_text() == 'Preserve until explicit discard'
    # Normal explicit-discard physical steps; no proof or database is edited.
    git.run('worktree', 'remove', '--force', str(checkout))
    assert git.commit(resource.source_ref).sha == h1
    assert ownership.authenticate_prebuilt_resource(git, resource) == resource.prebuilt_workspace
    git.run('update-ref', '-d', resource.source_ref, h1)
    assert ownership.authenticate_prebuilt_resource(git, resource) == resource.prebuilt_workspace
    assert not checkout.exists()


def test_lazy_resource_does_not_adopt_an_unexpected_old_head(idle_copy):
    kernel, _execution, _runner, _ports, git, effect, h0, h1 = installed(idle_copy, prebuilt=False)
    resource = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert resource.prebuilt_workspace['before_head'] is None and resource.head_sha == h1 != h0
    assert ownership.authenticate_prebuilt_resource(git, resource) == resource.prebuilt_workspace
    git.run('worktree', 'add', '--detach', resource.checkout, h0)
    with pytest.raises(KernelError, match='foreign_local_checkout'):
        ownership.authenticate_prebuilt_resource(git, resource)
    git.run('switch', '--detach', h1, cwd=Path(resource.checkout))
    assert ownership.authenticate_prebuilt_resource(git, resource) == resource.prebuilt_workspace


def test_resource_registration_read_failure_never_establishes_absence(idle_copy, monkeypatch):
    kernel, _execution, _runner, _ports, git, effect, _h0, _h1 = installed(idle_copy)
    resource = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    actual = LocalGit.run
    def failed(reader, *arguments, **options):
        if arguments[:2] == ('worktree', 'list'):
            raise KernelError('git_operation_failed', 'Synthetic registration read failure')
        return actual(reader, *arguments, **options)
    monkeypatch.setattr(LocalGit, 'run', failed)
    with pytest.raises(KernelError, match='local_read_failed'):
        ownership.authenticate_prebuilt_resource(git, resource)
