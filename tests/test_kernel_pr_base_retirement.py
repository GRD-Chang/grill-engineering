"""Retirement proof, not an arbitrary CLOSED flag, permits stale-base closeout."""
from support.core_operations import prepare_scripted_agent
from dataclasses import replace

import pytest

from agent_run.errors import KernelError
from support.offline_api import SyntheticGitHub
from support.control_scenarios import _parent_requeue, _retirement


@pytest.mark.parametrize('reuse_confirmed', [False, True])
def test_explicit_requeue_consumes_its_proven_closed_pr_with_an_ancestor_base(tmp_path, monkeypatch, reuse_confirmed):
    scenario, old, replacement, world = _parent_requeue(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert kernel.control('requeue', 'original-requeue', subject=subject, replacement=replacement, world=world) == 'pending'
    assert scenario.runner().run_once().kind == 'progress'
    row, payload, receipt = _retirement(scenario)
    assert receipt['disposition'] == 'target' and receipt['pr']['state'] == 'CLOSED'
    assert receipt['pr']['base']['sha'] == old.target.sha
    assert receipt['raw']['write_returncode'] == 0 and row['attempts'] == 1
    assert payload['retirement_intent']['control_action'] == 'original-requeue'
    action = 'original-requeue'
    if reuse_confirmed:
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, body=scenario.requirements.parent.body + '\nAnother explicit revision.'))
        world = scenario.world()
        assert scenario.runner().run_once().reason == 'requeue_scope_changed'
        assert _retirement(scenario)[0]['status'] == 'confirmed'
        action = 'requeue-proven-closure'
        assert kernel.control('requeue', action, subject=subject, replacement=replacement, world=world) == 'pending'
    for _ in range(8):
        if kernel.status(subject)['generation'] == 2:
            break
        step = scenario.runner().run_once()
        assert step.kind == 'progress', step
    assert kernel.status(subject)['generation'] == 2
    assert kernel.control('requeue', action, subject=subject, replacement=replacement, world=world) == 'applied'
    after, retained, confirmed = _retirement(scenario)
    assert after['id'] == row['id'] and after['attempts'] == 1 and after['status'] == 'confirmed'
    assert retained == payload and confirmed == receipt
    assert sum(w['operation'] == 'retire_pr' for w in scenario.writes()) == 2  # One comment and one close.
    assert scenario.run().kind == 'approval'
    kernel.control('approve', 'approve-new-generation', subject=subject, world=scenario.world())
    assert scenario.run().kind == 'complete'


@pytest.mark.parametrize('foreign', ['unowned_close', 'actor', 'diverged_base', 'missing_ancestry', 'foreign_close'])
def test_closed_ancestor_base_never_replaces_original_retirement_proof(tmp_path, monkeypatch, foreign):
    scenario, old, replacement, world = _parent_requeue(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert old.pr is not None
    if foreign == 'unowned_close':
        scenario.port.external_pr_transition(old.pr.number, 'CLOSED', 'another-maintainer')
        with pytest.raises(KernelError, match='retirement_pr_changed'):
            kernel.control('requeue', 'foreign-closed-pr', subject=subject,
                           replacement=replacement, world=scenario.world())
        assert kernel.status(subject)['generation'] == 1
        assert not any(w['operation'] == 'retire_pr' for w in scenario.writes())
        return

    assert kernel.control('requeue', 'owned-requeue', subject=subject, replacement=replacement, world=world) == 'pending'
    assert prepare_scripted_agent(kernel, scenario.runner().run_once()).kind == 'progress'
    original, payload, receipt = _retirement(scenario)
    assert original['attempts'] == 1 and receipt['disposition'] == 'target'
    action = 'owned-requeue'
    if foreign == 'foreign_close':
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, body=scenario.requirements.parent.body + '\nLatest requirement.'))
        scenario.world()
        assert prepare_scripted_agent(kernel, scenario.runner().run_once()).reason == 'requeue_scope_changed'
        assert _retirement(scenario)[0]['status'] == 'confirmed'
        scenario.port.external_pr_transition(old.pr.number, 'OPEN', 'another-maintainer')
        scenario.port.external_pr_transition(old.pr.number, 'CLOSED', 'another-maintainer')
        world = scenario.world()
        action = 'recheck-the-original-retirement'
        assert kernel.control('requeue', action, subject=subject, replacement=replacement, world=world) == 'pending'
    elif foreign == 'actor':
        record = scenario.port.objects.pr(old.pr.number)
        scenario.port.objects.put_pr({**record, 'user': {'login': 'different-publisher'}})
    elif foreign == 'diverged_base':
        other = scenario.port.git.make_commit(old.target.tree, (), 'An unrelated base history')
        assert not scenario.port.git.is_ancestor(other.sha, world.target.sha)
        scenario.port.physical.push('refs/heads/unrelated-base', None, other.sha)
        read = SyntheticGitHub.object

        def diverged(api, endpoint):
            value = read(api, endpoint)
            if endpoint.startswith(f'repos/{scenario.spec.repository}/pulls/') and value.get('number') == old.pr.number:
                return {**value, 'base': {**value['base'], 'sha': other.sha}}
            return value

        monkeypatch.setattr(SyntheticGitHub, 'object', diverged)
    elif foreign == 'missing_ancestry':
        make_ports = scenario.make_ports

        def without_proof(binding):
            ports = make_ports(binding)
            read = ports.delivery

            def physical(request):
                facts = read(request)
                return replace(facts, ancestry=tuple(edge for edge in facts.ancestry
                    if edge != (old.target.sha, world.target.sha)))

            ports.delivery = physical
            return ports

        monkeypatch.setattr(scenario, 'make_ports', without_proof)
    for _ in range(8):
        if 'requeue_retirement_changed' in kernel.status(subject)['gates']:
            break
        step = prepare_scripted_agent(kernel, scenario.runner().run_once())
        assert step.call is None
    assert kernel.status(subject)['generation'] == 1
    assert 'requeue_retirement_changed' in kernel.status(subject)['gates']
    assert kernel.control('requeue', action, subject=subject, replacement=replacement, world=world) == 'superseded'
    retained, retained_payload, retained_receipt = _retirement(scenario)
    assert retained['id'] == original['id'] and retained['attempts'] == 1
    assert retained_payload == payload and retained_receipt == receipt
    assert sum(w['operation'] == 'retire_pr' for w in scenario.writes()) == 2
