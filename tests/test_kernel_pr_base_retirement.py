"""Retirement proof, not an arbitrary CLOSED flag, permits stale-base closeout."""
from dataclasses import replace

import pytest

from agent_run.errors import KernelError
from support.control_scenarios import _parent_requeue, _retirement


def test_explicit_requeue_consumes_its_proven_closed_pr_with_an_ancestor_base(tmp_path, monkeypatch):
    scenario, old, replacement, world = _parent_requeue(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert kernel.control('requeue', 'original-requeue', subject=subject, replacement=replacement, world=world) == 'pending'
    assert scenario.runner().run_once().kind == 'progress'
    row, payload, receipt = _retirement(scenario)
    assert receipt['disposition'] == 'target' and receipt['pr']['state'] == 'CLOSED'
    assert receipt['pr']['base']['sha'] == old.target.sha
    assert receipt['raw']['write_returncode'] == 0 and row['attempts'] == 1
    assert payload['retirement_intent']['control_action'] == 'original-requeue'
    # The original receipt is observed but unconsumed when GitHub advances its
    # reported base. That receipt's base (not the new scope target) owns the edge.
    from support.offline_api import SyntheticGitHub
    read = SyntheticGitHub.object
    def current_base(api, endpoint):
        value = read(api, endpoint)
        if endpoint.startswith(f'repos/{scenario.spec.repository}/pulls/') and value.get('number') == old.pr.number:
            return {**value, 'base': {**value['base'], 'sha': world.target.sha}}
        return value
    monkeypatch.setattr(SyntheticGitHub, 'object', current_base)
    observed = scenario.world()
    edge = (receipt['pr']['base']['sha'], observed.pr.base.sha)
    assert row['status'] == 'observed' and edge[0] != edge[1] and edge in observed.ancestry
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one('SELECT * FROM runs WHERE id=?', (kernel.storage.run_id,))
        job = kernel._job(tx, subject)
        assert kernel._retirement_current(tx, run, job, row, observed)
        assert not kernel._retirement_current(tx, run, job, row,
            replace(observed, ancestry=tuple(pair for pair in observed.ancestry if pair != edge)))
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


@pytest.mark.parametrize('foreign', [None, 'unowned_close', 'actor', 'diverged_base', 'missing_ancestry'])
def test_closed_ancestor_base_requires_the_original_recorded_retirement(foreign):
    from agent_run.model import Commit, World
    from support.retirement_record import BASE, HEAD, SUBJECT, confirmed_retirement
    with confirmed_retirement() as (db, kernel, closed, _receipt):
        target = Commit('e' * 40, 'f' * 40)
        world = World('r', 'g', 'r', target, HEAD, pr=closed, ancestry=((BASE.sha, target.sha),))
        if foreign == 'unowned_close':
            db.execute("DELETE FROM effects WHERE run_id='A'")
        elif foreign == 'actor':
            world = replace(world, pr=replace(closed, actor='another-publisher'))
        elif foreign == 'diverged_base':
            world = replace(world, pr=replace(closed, base=Commit('1' * 40, '2' * 40)))
        elif foreign == 'missing_ancestry':
            world = replace(world, ancestry=())
        before = tuple(db.iterdump())
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            job = kernel._job(tx, SUBJECT)
            if foreign is None:
                assert kernel._recorded_retirement(tx, run, job, world)['id'] == 'retirement'
            else:
                with pytest.raises(KernelError, match='retirement_pr_changed'):
                    kernel._recorded_retirement(tx, run, job, world)
        assert tuple(db.iterdump()) == before
