"""An absent PR observation is distinct from authority to issue a new POST."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from agent_run.model import DeliveryKind
from agent_run.errors import KernelError
from agent_run.model import ReadWait
from support.offline import Scenario
from support.offline_api import SyntheticGitHub
from test_kernel_exploration import advance_to_effect
from test_kernel_remote_effects import _fixture, _pr_effect


@pytest.mark.parametrize('change', ['append', 'rewrite', 'source'])
def test_absent_original_pr_needs_same_source_and_proven_target_append(tmp_path: Path, monkeypatch, change: str) -> None:
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == 'target'
    effect = _pr_effect(push)
    original = asdict(effect)
    if change == 'append':
        next_head = git.make_commit(push.base.tree, (push.base.sha,), 'External target append')
        ref, before = push.target_ref, push.base.sha
    elif change == 'rewrite':
        next_head = git.make_commit(push.base.tree, (), 'Unrelated target history')
        ref, before = push.target_ref, push.base.sha
    else:
        next_head = git.make_commit(push.candidate.tree, (push.after_sha,), 'External source advance')
        ref, before = push.source_ref, push.after_sha
    git.run('push', f'--force-with-lease={ref}:{before}', 'origin', f'{next_head.sha}:{ref}')
    assert port.reconcile(effect).disposition == ('prior' if change == 'append' else 'third')
    assert port.execute(effect, lambda: pytest.fail('Changed anchors cannot authorize a fresh POST')).disposition == 'third'
    assert asdict(effect) == original
    assert json.loads(state.read_text())['prs'] == []
    assert not any('--method' in json.loads(line) for line in calls.read_text().splitlines())


def test_pr_creation_rechecks_original_anchors_after_authorization(tmp_path: Path, monkeypatch) -> None:
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, lambda: None).disposition == 'target'
    effect = _pr_effect(push)
    authorizations = []

    def changed() -> None:
        authorizations.append(effect.effect_id)
        later = git.make_commit(push.base.tree, (push.base.sha,), 'Changed during authorization')
        git.run('push', 'origin', f'{later.sha}:{push.target_ref}')

    with pytest.raises(KernelError, match='pr_boundary_changed'):
        port.execute(effect, changed)
    assert authorizations == [effect.effect_id]
    assert json.loads(state.read_text())['prs'] == []
    assert not any('--method' in json.loads(line) for line in calls.read_text().splitlines())


def test_public_stop_during_post_authorization_pr_read_prevents_the_first_post(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, 'pr_write')
    effect = held.effect
    authorized, stopped, refused = [], [], []
    read = SyntheticGitHub.list

    def authorize() -> None:
        held.authorize()
        authorized.append(effect.effect_id)

    def stop_during_read(api, endpoint):
        rows = read(api, endpoint)
        if authorized and not stopped and '/pulls?' in endpoint:
            scenario.kernel.control('stop', 'stop-during-extra-pr-read')
            stopped.append(True)
        return rows

    monkeypatch.setattr(SyntheticGitHub, 'list', stop_during_read)
    try:
        scenario.port.execute(effect, authorize)
    except (KernelError, ReadWait) as error:
        refused.append(error)
    assert authorized and stopped and scenario.kernel.status(effect.subject)['stopped']
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == 0
    assert scenario.port.objects.pulls() == []
    assert len(refused) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('sent', 1)


@pytest.mark.parametrize('change', ['source', 'late_pr'])
def test_final_authorization_refuses_create_facts_changed_during_its_own_read(tmp_path: Path, monkeypatch, change: str) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, 'pr_write')
    effect = held.effect
    authorizations, reread, changed, refused = [], [], [], []
    read_prs, read_source = SyntheticGitHub.list, scenario.port.source
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())

    def authorize() -> None:
        held.authorize()
        authorizations.append(effect.effect_id)

    def observe_prs(provider, endpoint):
        rows = read_prs(provider, endpoint)
        if authorizations and '/pulls?' in endpoint:
            reread.append(True)
        return rows

    def change_during_final_read(requirements, parent_number):
        source = read_source(requirements, parent_number)
        if reread and not changed:
            if change == 'source':
                scenario.port.external_remote_append(effect.source_ref, {'other.txt': 'Independent source work\n'}, 'other')
            else:
                api.write(f'repos/{effect.repository}/pulls', 'POST', {
                    'title': effect.payload['title'], 'body': effect.payload['body'],
                    'head': effect.source_ref.removeprefix('refs/heads/'),
                    'base': effect.target_ref.removeprefix('refs/heads/')})
            changed.append(True)
        return source

    monkeypatch.setattr(SyntheticGitHub, 'list', observe_prs)
    monkeypatch.setattr(scenario.port, 'source', change_during_final_read)
    try:
        scenario.port.execute(effect, authorize)
    except (KernelError, ReadWait) as error:
        refused.append(error)
    assert reread and changed
    requests = scenario.port.objects.snapshot()['api_requests']
    # The one allowed late_pr POST is the queued physical request above;
    # the newly authorized operation must issue no second request.
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == (1 if change == 'late_pr' else 0)
    assert not [write for write in scenario.port.writes() if write['operation'] == 'pr_write']
    assert len(refused) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('sent', 1)


def test_late_post_during_authorization_is_read_back_without_another_post(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, 'pr_write', sent=True)
    effect = held.effect
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())

    def delayed() -> None:
        held.authorize()
        # The original physical request becomes visible during the bounded
        # retry's authority check. The provider performs a raw API primitive.
        api.write(f'repos/{effect.repository}/pulls', 'POST', {
            'title': effect.payload['title'], 'body': effect.payload['body'],
            'head': effect.source_ref.removeprefix('refs/heads/'),
            'base': effect.target_ref.removeprefix('refs/heads/')})

    with pytest.raises(KernelError, match='pr_boundary_changed'):
        scenario.port.execute(effect, delayed)
    assert len(scenario.port.objects.pulls()) == 1
    assert scenario.run().kind == 'complete'
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))
        assert tuple(row) == ('confirmed', 1)


@pytest.mark.parametrize('actor', ['offline-publisher', 'foreign-publisher'])
def test_late_original_post_remains_an_external_fact_after_intent_retirement(tmp_path: Path, monkeypatch, actor: str) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, 'pr_write', sent=True)
    effect, kernel = held.effect, scenario.kernel
    with kernel.storage.transaction(write=False) as tx:
        before = dict(tx.one('SELECT payload_id,actor,attempts,pr_number,receipt_id FROM effects WHERE id=?', (effect.effect_id,)))
    scenario.port.external_remote_append(effect.target_ref, {'external.txt': 'Retained external work\n'}, 'append')
    assert scenario.step().kind == 'progress'
    assert kernel.status(effect.subject)['phase'] == 'integration'
    assert kernel.status(effect.subject)['pending_write'] is None
    # The timed-out request can still land. Execute only its physical API POST;
    # neither Core nor the provider is told which business outcome to produce.
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())
    response = api.write(f'repos/{effect.repository}/pulls', 'POST', {
        'title': effect.payload['title'], 'body': effect.payload['body'],
        'head': effect.source_ref.removeprefix('refs/heads/'), 'base': effect.target_ref.removeprefix('refs/heads/')})
    number = json.loads(response.stdout)['number']
    raw = scenario.port.objects.pr(number)
    assert raw is not None
    if actor != 'offline-publisher':
        scenario.port.objects.put_pr({**raw, 'user': {'login': actor}})
    authorizations = []
    execute = scenario.port.execute

    def guarded(work, authorize):
        if work.operation != 'pr_write':
            return execute(work, authorize)
        def current() -> None:
            authorize()
            authorizations.append(work.effect_id)
        return execute(work, current)

    monkeypatch.setattr(scenario.port, 'execute', guarded)
    result = scenario.run()
    assert result.kind == ('complete' if actor == 'offline-publisher' else 'paused')
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == 1
    assert sum(row['method'] == 'PATCH' and row['endpoint'].endswith(f'/pulls/{number}') for row in requests) == (1 if actor == 'offline-publisher' else 0)
    assert bool(authorizations) == (actor == 'offline-publisher')
    assert effect.effect_id not in authorizations
    with kernel.storage.transaction(write=False) as tx:
        row = dict(tx.one('SELECT status,payload_id,actor,attempts,pr_number,receipt_id FROM effects WHERE id=?', (effect.effect_id,)))
    assert row == {'status': 'retired', **before}
    budgets = kernel.status(effect.subject)['budgets']
    assert budgets['requirements']['developments_used'] == budgets['requirements']['reviews_used'] == 1
    assert budgets['integration']['developments_used'] == 0
    if actor == 'offline-publisher':
        assert kernel.status(effect.subject)['pr_number'] == number
        merged = scenario.port.remote_commit(effect.target_ref)
        assert merged is not None
        assert scenario.port.git.run('show', f'{merged.sha}:external.txt').stdout == 'Retained external work\n'
    else:
        assert scenario.port.objects.pr(number) == {**raw, 'user': {'login': actor}}
        assert not any(row['endpoint'].endswith('/merge') for row in requests)
