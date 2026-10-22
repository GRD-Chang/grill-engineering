"""Real installed workspace facts cross the Core resource boundary unchanged."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import migration_run_ownership
from agent_run_kernel.model import KernelError, ResourceInspection, ResourceWitness, inspection_wire, resource_wire
from test_kernel_idle_prebuilt import idle_copy
from test_kernel_prebuilt_actual_integration_review import installed, partial_actual_workspace


def test_unfinished_resource_uses_the_durable_binding_without_another_getter(idle_copy, monkeypatch):
    kernel, _execution, _runner, _ports, git, effect, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)

    def must_not_rebind(*args):
        raise AssertionError("Resource inspection must use the original intent")

    monkeypatch.setattr(migration_run_ownership, 'prebuilt_workspace_binding', must_not_rebind)
    witness = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert witness.head_sha == h1
    assert witness.prebuilt_workspace == effect.payload['prebuilt_workspace']
    assert witness.prebuilt_workspace['workspace_oid'] == h0
    assert git.run('rev-parse', 'HEAD', cwd=Path(witness.checkout)).stdout.strip() == h0
    assert git.commit(witness.source_ref).sha == h1


def test_stale_retired_workspace_keeps_its_original_resource_binding(idle_copy, monkeypatch):
    kernel, _execution, runner, ports, _git, effect, _h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    ports.requirements = replace(ports.requirements, parent=replace(ports.requirements.parent,
        body=ports.requirements.parent.body + '\nA legitimately changed requirement'))
    assert runner.run_once().kind == 'progress'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT status FROM effects WHERE id=?', (effect.effect_id,))[0] == 'retired'
    witness = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert witness.head_sha == h1 and witness.prebuilt_workspace == effect.payload['prebuilt_workspace']


def test_completed_workspace_retains_exact_h1_without_old_h0_permission(idle_copy):
    kernel, _execution, runner, ports, git, effect, _h0, h1 = installed(idle_copy)
    ports.hold = False
    assert runner.run_once().kind == 'progress'
    witness = next(item for item in kernel.generation_resources() if item.subject == effect.subject)
    assert witness.head_sha == h1 and witness.prebuilt_workspace is None
    assert git.run('rev-parse', 'HEAD', cwd=Path(witness.checkout)).stdout.strip() == h1
    assert next(item for item in ports.inspect(kernel.generation_resources()) if item.resource == witness).disposition == 'clean'


def test_ordinary_resource_wire_keeps_its_existing_material_shape():
    original = dict(subject='ticket:2', generation=1, kind='development', checkout='/private/work/ticket-2',
        owned_root='/private/work', common_git_dir='/private/repo/.git', source_ref='refs/heads/ticket-2', head_sha='1' * 40)
    witness = ResourceWitness(**original)
    assert resource_wire(witness) == original
    assert inspection_wire(ResourceInspection(witness, 'clean', '1' * 40, 'worktree_owned')) == {
        'resource': original, 'disposition': 'clean', 'head_sha': '1' * 40, 'reason': 'worktree_owned'}


def test_lazy_resource_does_not_gain_h0_authority_from_a_clean_head_observation(idle_copy):
    # Core consumer test, not a substitute for the physical authenticator.
    # Use a real unmodified lazy proof and a normal external clean checkout.
    kernel, _execution, _runner, ports, git, effect, h0, h1 = installed(idle_copy, prebuilt=False)
    assert h0 != h1 and effect.payload['prebuilt_workspace']['before_head'] is None
    checkout = Path(effect.payload['checkout'])
    git.run('worktree', 'add', '--detach', str(checkout), h0)
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == h0
    assert git.run('status', '--porcelain', cwd=checkout).stdout == ''
    resources = kernel.generation_resources()
    # Even a clean-head observation cannot grant the absent original H0 right.
    inspections = tuple(replace(item, disposition='clean', head_sha=h0, reason='observed_clean_head')
        if item.resource.subject == effect.subject else item for item in ports.inspect(resources))
    with pytest.raises(KernelError, match='abandon_resource_unproven'):
        kernel.control('abandon', 'reject-lazy-h0-permission', resource_inspections=inspections)
    assert kernel.source_status()['abandonment'] is None
    assert checkout.exists() and git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == h0
