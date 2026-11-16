"""Fixture hints resolve through the normal atomic Task/Run producer."""
from dataclasses import replace
import json

import pytest

from agent_run.errors import AgentProfileError
from agent_run.model import Commit, Delivery, DeliveryKind, Policy, Profile, RunSpec
from agent_run.storage.sqlite import Storage
from support.offline import fixture_inputs
from support.repository_fixture import create_kernel


@pytest.mark.parametrize('effort', [None, 'high', 'not-a-reasoning-effort'])
def test_repository_fixture_resolves_only_default_effort_hints(tmp_path, effort):
    policy = Policy()
    if effort is not None:
        policy = replace(policy, development_profile=Profile('explicit-model', effort))
    delivery = Delivery('parent', DeliveryKind.PARENT_ONLY, 1, 'revision', Commit('base', 'tree'),
                        'refs/heads/work', 'refs/heads/main')
    spec = RunSpec('fixture-profile', 'example/project', 1, 'parent', 'graph', (delivery,), policy)
    path = tmp_path / 'state' / 'kernel.sqlite3'
    if effort == 'not-a-reasoning-effort':
        with pytest.raises(AgentProfileError):
            create_kernel(path, spec)
        # The explicit fixture creates the repository authority first; invalid
        # hints must not publish a Task, Run, revision or execution beneath it.
        with Storage.metadata(path).transaction(write=False) as tx:
            for table in ('tasks', 'runs', 'profile_revisions', 'invocations', 'hosts'):
                assert tx.one('SELECT count(*) FROM ' + table)[0] == 0
        return
    kernel = create_kernel(path, spec)
    with kernel.storage.transaction(write=False) as tx:
        revision = tx.one('SELECT material_id,subject FROM profile_revisions WHERE run_id=? AND id=1',
                          (kernel.storage.run_id,))
        frozen = json.loads(tx.get(revision['material_id'], revision['subject']))['profiles']
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0
    defaults = fixture_inputs().profiles['profiles']
    for role in ('development', 'review', 'publication'):
        hint = getattr(policy, role + '_profile')
        assert frozen[role]['model'] == hint.model
        assert frozen[role]['reasoning_effort'] == (
            defaults[role]['reasoning_effort'] if hint.effort == Profile().effort else hint.effort)
