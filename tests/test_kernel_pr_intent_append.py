"""An absent PR observation is distinct from authority to issue a new POST."""

from agent_run.contracts.write_authority import WriteAuthority
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from agent_run.errors import KernelError
from agent_run.model import ReadWait
from support.offline_api import SyntheticGitHub
from support.effect_boundary import advance_to_effect
from support.remote_effects import _fixture, _pr_effect


@pytest.mark.parametrize('change', ['append', 'rewrite', 'source'])
def test_absent_original_pr_needs_same_source_and_proven_target_append(tmp_path: Path, monkeypatch, change: str) -> None:
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, WriteAuthority(lambda: lambda: None)).disposition == 'target'
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
    assert port.execute(effect, WriteAuthority(lambda: lambda: pytest.fail('Changed anchors cannot authorize a fresh POST'))).disposition == 'third'
    assert asdict(effect) == original
    assert json.loads(state.read_text())['prs'] == []
    assert not any('--method' in json.loads(line) for line in calls.read_text().splitlines())


def test_pr_creation_rechecks_original_anchors_after_authorization(tmp_path: Path, monkeypatch) -> None:
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    assert port.execute(push, WriteAuthority(lambda: lambda: None)).disposition == 'target'
    effect = _pr_effect(push)
    preparations, authorizations = [], []

    def changed():
        preparations.append(effect.effect_id)
        later = git.make_commit(push.base.tree, (push.base.sha,), 'Changed during authorization')
        git.run('push', 'origin', f'{later.sha}:{push.target_ref}')
        return lambda: authorizations.append(effect.effect_id)

    with pytest.raises(KernelError, match='pr_boundary_changed'):
        port.execute(effect, WriteAuthority(changed))
    assert preparations == [effect.effect_id] and authorizations == []
    assert json.loads(state.read_text())['prs'] == []
    assert not any('--method' in json.loads(line) for line in calls.read_text().splitlines())


def test_public_stop_during_post_authorization_pr_read_prevents_the_first_post(monkeypatch, ticket_before_pr) -> None:
    scenario = ticket_before_pr
    held = advance_to_effect(scenario, monkeypatch, 'pr_write')
    effect = held.effect
    with scenario.kernel.storage.transaction(write=False) as tx:
        original_intent = tuple(tx.one('SELECT payload_id,actor,before_sha,after_sha FROM effects WHERE id=?', (effect.effect_id,)))
    prepared, authorized, stopped, refused = [], [], [], []
    read = SyntheticGitHub.list

    def prepare():
        final_admission = held.authorize.prepare()
        prepared.append(effect.effect_id)
        def authorize() -> None:
            final_admission()
            authorized.append(effect.effect_id)
        return authorize

    def stop_during_read(api, endpoint):
        rows = read(api, endpoint)
        if prepared and not stopped and '/pulls?' in endpoint:
            scenario.kernel.control('stop', 'stop-during-extra-pr-read')
            stopped.append(True)
        return rows

    monkeypatch.setattr(SyntheticGitHub, 'list', stop_during_read)
    try:
        scenario.port.execute(effect, WriteAuthority(prepare))
    except (KernelError, ReadWait) as error:
        refused.append(error)
    assert prepared and stopped and scenario.kernel.status(effect.subject)['stopped']
    assert authorized == []
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == 0
    assert scenario.port.objects.pulls() == []
    assert len(refused) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        # Stop retires an unstarted request; preparation never dispatched it.
        assert tuple(tx.one('SELECT status,attempts,receipt_id FROM effects WHERE id=?', (effect.effect_id,))) == ('cancelled', 0, None)
        assert tuple(tx.one('SELECT payload_id,actor,before_sha,after_sha FROM effects WHERE id=?', (effect.effect_id,))) == original_intent


@pytest.mark.parametrize('change', ['source', 'late_pr'])
def test_final_authorization_refuses_create_facts_changed_during_its_own_read(monkeypatch, change: str, ticket_before_pr) -> None:
    scenario = ticket_before_pr
    held = advance_to_effect(scenario, monkeypatch, 'pr_write')
    effect = held.effect
    preparations, authorizations, reread, changed, refused = [], [], [], [], []
    read_prs = SyntheticGitHub.list
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())

    def prepare():
        final_admission = held.authorize.prepare()
        preparations.append(effect.effect_id)
        # The slow preparation still has I/O authority, but not dispatch
        # authority. The adapter must reject facts changed before its final
        # original-intent check; final admission performs no second slow read.
        if change == 'source':
            scenario.port.external_remote_append(effect.source_ref, {'other.txt': 'Independent source work\n'}, 'other')
        else:
            api.write(f'repos/{effect.repository}/pulls', 'POST', {
                'title': effect.payload['title'], 'body': effect.payload['body'],
                'head': effect.source_ref.removeprefix('refs/heads/'),
                'base': effect.target_ref.removeprefix('refs/heads/')})
        changed.append(True)
        def authorize() -> None:
            final_admission()
            authorizations.append(effect.effect_id)
        return authorize

    def observe_prs(provider, endpoint):
        rows = read_prs(provider, endpoint)
        if preparations and '/pulls?' in endpoint:
            reread.append(True)
        return rows

    monkeypatch.setattr(SyntheticGitHub, 'list', observe_prs)
    try:
        scenario.port.execute(effect, WriteAuthority(prepare))
    except (KernelError, ReadWait) as error:
        refused.append(error)
    assert reread and changed
    assert preparations == [effect.effect_id] and authorizations == []
    requests = scenario.port.objects.snapshot()['api_requests']
    # The one allowed late_pr POST is the queued physical request above;
    # the newly authorized operation must issue no second request.
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == (1 if change == 'late_pr' else 0)
    assert not [write for write in scenario.port.writes() if write['operation'] == 'pr_write']
    assert len(refused) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('prepared', 0)


def test_late_post_during_authorization_is_read_back_without_another_post(monkeypatch, ticket_before_pr) -> None:
    scenario = ticket_before_pr
    held = advance_to_effect(scenario, monkeypatch, 'pr_write', sent=True)
    effect = held.effect
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())

    def delayed():
        final_admission = held.authorize.prepare()
        # The original physical request becomes visible during the bounded
        # retry's authority check. The provider performs a raw API primitive.
        api.write(f'repos/{effect.repository}/pulls', 'POST', {
            'title': effect.payload['title'], 'body': effect.payload['body'],
            'head': effect.source_ref.removeprefix('refs/heads/'),
            'base': effect.target_ref.removeprefix('refs/heads/')})
        return final_admission

    with pytest.raises(KernelError, match='pr_boundary_changed'):
        scenario.port.execute(effect, WriteAuthority(delayed))
    assert len(scenario.port.objects.pulls()) == 1
    assert scenario.run().kind == 'complete'
    requests = scenario.port.objects.snapshot()['api_requests']
    assert sum(row['method'] == 'POST' and row['endpoint'].endswith('/pulls') for row in requests) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))
        assert tuple(row) == ('confirmed', 1)


@pytest.mark.parametrize('actor', ['offline-publisher', 'foreign-publisher'])
def test_late_original_post_remains_an_external_fact_after_intent_retirement(monkeypatch, actor: str, ticket_before_pr) -> None:
    scenario = ticket_before_pr
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
        def prepare_current():
            final_admission = authorize.prepare()
            def current() -> None:
                final_admission()
                authorizations.append(work.effect_id)
            return current
        return execute(work, WriteAuthority(prepare_current))

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
