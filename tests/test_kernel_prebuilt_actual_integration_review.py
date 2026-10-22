"""Real fresh source producer, installed binding and Git; no prepared DB edits."""
from pathlib import Path
from dataclasses import replace

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.migration_run_install import install_imported_facts
from agent_run_kernel.migration_run_ownership import prebuilt_workspace_binding
from agent_run_kernel.model import KernelError
from agent_run_kernel.operation_runner import DeliveryFacts
from test_kernel_idle_prebuilt import idle_copy
from test_kernel_remote_effects import EffectBoundary


@pytest.fixture(autouse=True)
def unchanged_source_bytes(idle_copy):
    snapshot = idle_copy[0]
    before = snapshot.source.read_bytes(), snapshot.path.read_bytes()
    yield
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes()) == before


class RealPorts:
    def __init__(self, git, facts, owned):
        self.git, self.facts = git, facts
        self.requirements = facts.requirements
        self.local = LocalEffects(git, owned_root=owned)
        self.hold = True
        self.effects = []
        self.authorizations = []
        self.before_authorize = None

    def source(self, parent):
        assert parent == self.facts.requirements.parent.number
        return self.requirements

    def delivery(self, request):
        def commit(ref):
            value = self.git.run('for-each-ref', '--format=%(objectname)', ref).stdout.strip()
            return None if not value else self.git.commit(value)
        return DeliveryFacts(commit(request.target_ref), commit(request.source_ref))

    def inspect(self, resources):
        return self.local.inspect_resources(resources)

    def execute(self, effect, authorize):
        assert effect.operation in {'workspace', 'inspect_resources', 'abandon_worktree'}
        self.effects.append(effect)
        if self.hold:
            raise EffectBoundary(effect, authorize)
        def guarded():
            self.authorizations.append(effect.effect_id)
            if self.before_authorize is not None:
                self.before_authorize()
            authorize()
        return self.local.execute(effect, guarded)

    def reconcile(self, effect):
        return self.local.reconcile(effect)


def installed(idle_copy, *, prebuilt=True, equal=False):
    snapshot, options, git, target, h0 = idle_copy
    if prebuilt:
        git.run('worktree', 'add', '--detach', str(options['checkout']), h0)
    facts = read_initial_import(snapshot, **options)
    kernel = install_imported_facts(facts, target)
    subject = 'ticket:2'
    if not equal:
        (git.root / 'dependency.txt').write_text('Dependency-visible H1\n')
        git.run('add', 'dependency.txt')
        git.run('commit', '-m', 'Normal Run head advancement after import')
        h1 = git.commit('HEAD').sha
        git.run('update-ref', kernel.delivery(subject).target_ref, h1, h0)
    else:
        h1 = h0
    execution = kernel.claim_executor('independent-real-source-review')
    ports = RealPorts(git, facts, options['checkout'].parent)
    runner = kernel.operation_runner(execution, lambda binding: ports)
    for _ in range(8):
        try:
            step = runner.run_once()
        except EffectBoundary as boundary:
            original = boundary.effect
            break
        assert step.call is None
    else:
        raise AssertionError('Normal Core runner did not prepare the imported workspace')
    assert original.before_sha == h1
    assert original.payload['prebuilt_workspace']['workspace_oid'] == h0
    with kernel.storage.transaction(write=False) as tx:
        certified = prebuilt_workspace_binding(kernel.storage, tx, tx.one('SELECT * FROM runs'),
                                               tx.one('SELECT * FROM subjects WHERE id=?', (subject,)))
    assert certified == original.payload['prebuilt_workspace']
    return kernel, execution, runner, ports, git, original, h0, h1


@pytest.mark.parametrize('prebuilt,equal', [(True, False), (True, True), (False, False)])
def test_actual_source_binding_dispatches_to_dependency_visible_head(idle_copy, prebuilt, equal):
    kernel, execution, runner, ports, git, effect, h0, h1 = installed(idle_copy, prebuilt=prebuilt, equal=equal)
    assert (h0 == h1) == equal and ports.reconcile(effect).disposition == 'prior'
    ports.hold = False
    step = runner.run_once()
    assert step.call is None and step.kind == 'progress'
    checkout = Path(effect.payload['checkout'])
    assert git.commit(effect.source_ref).sha == h1
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == h1
    assert len(ports.authorizations) == (1 if equal else 2)
    if not equal:
        assert (checkout / 'dependency.txt').read_text() == 'Dependency-visible H1\n'
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts,before_sha FROM effects WHERE id=?', (effect.effect_id,))) == ('observed', 1, h1)


def test_prepared_zero_dispatch_complete_physical_target_is_not_execution(idle_copy):
    kernel, execution, runner, ports, git, effect, h0, h1 = installed(idle_copy)
    # Normal external physical changes, never a prepared database mutation.
    git.run('update-ref', effect.source_ref, h1, '0' * len(h1))
    git.run('switch', '--detach', '--no-overwrite-ignore', h1, cwd=Path(effect.payload['checkout']))
    assert ports.reconcile(effect).disposition == 'target'
    ports.hold = False
    step = runner.run_once()
    assert (step.kind, step.reason) == ('paused', 'write_not_dispatched')
    assert ports.authorizations == []
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts,receipt_id FROM effects WHERE id=?', (effect.effect_id,))) == ('prepared', 0, None)
        assert not tx.rows('SELECT id FROM attempts')


def partial_actual_workspace(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, effect, h0, h1 = installed(idle_copy)
    ports.hold = False
    actual = LocalGit.run

    def interrupt(self, *arguments, **kwargs):
        if arguments[:1] == ('switch',) and kwargs.get('cwd') == Path(effect.payload['checkout']):
            raise OSError('Actual ref CAS completed; checkout switch not started')
        return actual(self, *arguments, **kwargs)

    with monkeypatch.context() as interruption:
        interruption.setattr(LocalGit, 'run', interrupt)
        assert runner.run_once().reason == 'original_write_readback_required'
    assert git.commit(effect.source_ref).sha == h1
    assert git.run('rev-parse', 'HEAD', cwd=Path(effect.payload['checkout'])).stdout.strip() == h0
    assert ports.reconcile(effect).disposition == 'prior'
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('sent', 1)
    return kernel, execution, runner, ports, git, effect, h0, h1


def test_original_sent_partial_real_binding_converges_without_reallocation(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, effect, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    assert runner.run_once().kind == 'progress'
    # Prior receipt only permits normal existing authority to rearm the same
    # original intent; a second operation performs the remaining physical step.
    assert runner.run_once().kind == 'progress'
    assert ports.reconcile(effect).disposition == 'target'
    assert git.commit(effect.source_ref).sha == h1
    with kernel.storage.transaction(write=False) as tx:
        rows = tx.all("SELECT id,before_sha FROM effects WHERE subject=? AND operation='workspace'", (effect.subject,))
        assert [tuple(row) for row in rows] == [(effect.effect_id, h1)]


def test_partial_prebuilt_original_witness_permits_explicit_abandon_cleanup(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, effect, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    resources = kernel.generation_resources()
    inspections = ports.inspect(resources)
    try:
        result = kernel.control('abandon', 'abandon-original-prebuilt-partial', resource_inspections=inspections)
    except KernelError as error:
        pytest.fail(f'Normal authenticated H0 checkout / H1 ref cannot enter explicit Abandon: {error.code}; {inspections!r}')
    assert result == 'pending'
    assert Path(effect.payload['checkout']).exists() and git.commit(effect.source_ref).sha == h1
    assert kernel.source_status()['abandonment']['status'] == 'pending'


def test_real_public_stop_fences_the_second_workspace_write(idle_copy):
    kernel, execution, runner, ports, git, effect, h0, h1 = installed(idle_copy)
    ports.hold = False
    def stop_before_second_authorization():
        if len(ports.authorizations) == 2:
            kernel.control('stop', 'actual-stop-between-two-workspace-writes')
    ports.before_authorize = stop_before_second_authorization
    step = runner.run_once()
    assert (step.kind, step.reason) == ('paused', 'executor_fenced')
    assert len(ports.authorizations) == 2
    assert git.commit(effect.source_ref).sha == h1
    assert git.run('rev-parse', 'HEAD', cwd=Path(effect.payload['checkout'])).stdout.strip() == h0
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts,before_sha FROM effects WHERE id=?', (effect.effect_id,))) == ('sent', 1, h1)
        assert not tx.rows('SELECT id FROM attempts')


def test_real_source_change_retains_first_binding_and_h1_in_replacement(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, original, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    ports.requirements = replace(ports.requirements, parent=replace(ports.requirements.parent,
        body=ports.requirements.parent.body + '\nA legitimate new native requirement'))
    assert runner.run_once().kind == 'progress'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT status FROM effects WHERE id=?', (original.effect_id,))[0] == 'retired'
        assert not tx.rows('SELECT id FROM attempts')
    (git.root / 'later.txt').write_text('Later Run head must not replace original H1\n')
    git.run('add', 'later.txt')
    git.run('commit', '-m', 'Normal later Run head after stale retirement')
    later = git.commit('HEAD').sha
    git.run('update-ref', original.target_ref, later, h1)
    import agent_run_kernel.migration_run_ownership as ownership
    actual_getter = ownership.prebuilt_workspace_binding
    calls = []
    def observed_getter(*args):
        calls.append(True)
        return actual_getter(*args)
    monkeypatch.setattr(ownership, 'prebuilt_workspace_binding', observed_getter)
    ports.hold = True
    with pytest.raises(EffectBoundary) as boundary:
        runner.run_once()
    replacement = boundary.value.effect
    assert calls == []
    assert replacement.effect_id != original.effect_id
    assert replacement.before_sha == h1 != later
    assert replacement.payload['prebuilt_workspace'] == original.payload['prebuilt_workspace']
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (original.effect_id,))) == ('retired', 1)
