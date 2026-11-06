"""A PR's reported base is evidence, not the live target-ref authority."""
from dataclasses import replace
import json

import pytest

from agent_run.contracts.write_authority import WriteAuthority
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind
from support.run_scoped_kernel import domain_scenario
from support.offline_api import SyntheticGitHub
from support.remote_effects import _fixture, _publish_pr


def test_ticket_ci_repair_finishes_when_the_pr_reports_an_ancestor_base(tmp_path, monkeypatch, domain_scenario):
    scenario = domain_scenario(tmp_path / 'scenario', DeliveryKind.TICKET, checks=('fail', 'none'))
    kernel, subject = scenario.kernel, scenario.delivery.key
    for _ in range(160):
        status = kernel.status(subject)
        if status['purpose'] == 'ci' and status['phase'] == 'ready_publication':
            break
        scenario.step()
    else:
        raise AssertionError('The original Ticket did not finish its CI Development')
    before = kernel.status(subject)
    old_target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert old_target is not None
    newer = scenario.port.external_append(scenario.delivery.target_ref,
        {'sibling.txt': 'An independently completed sibling target.\n'}, 'sibling-target')
    object_read = SyntheticGitHub.object

    def stale_base(api, endpoint):
        data = object_read(api, endpoint)
        if (endpoint.startswith(f'repos/{scenario.spec.repository}/pulls/')
                and data.get('state') == 'open' and not data.get('merged')):
            return {**data, 'base': {**data['base'], 'sha': old_target.sha}}
        return data

    monkeypatch.setattr(SyntheticGitHub, 'object', stale_base)
    result = scenario.run()
    assert result.kind == 'complete', result
    after = kernel.status(subject)
    assert after['generation'] == before['generation']
    assert after['budgets']['ci'] == before['budgets']['ci']
    assert after['budgets']['requirements'] == before['budgets']['requirements']
    assert sum(c['role'] == 'development' and c['purpose'] == 'ci' for c in scenario.role_calls) == 1
    head = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert head is not None and scenario.port.git.is_ancestor(newer.sha, head.sha)
    assert sum(w['operation'] == 'merge' for w in scenario.writes()) == 1
    assert kernel.source_status()['completions'][0]['subject'] == subject


@pytest.mark.parametrize('metadata_converges', [False, True])
def test_pr_edit_uses_the_exact_live_intent_base_without_rewriting_the_reported_base(tmp_path, monkeypatch, metadata_converges):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    original, pr = _publish_pr(port, push)
    newer = git.make_commit(push.base.tree, (push.base.sha,), 'A legitimate target append')
    git.run('push', 'origin', newer.sha + ':' + push.target_ref)
    effect = replace(original, effect_id='current-target-edit', pr_number=pr.number, base=newer,
        payload={'title': 'Current whole Ticket', 'body': 'Updated original description',
                 'prior': {'title': pr.title, 'body': pr.body}})
    admissions = []

    def prepare():
        if metadata_converges:
            document = json.loads(state.read_text())
            document['prs'][0]['base']['sha'] = newer.sha
            state.write_text(json.dumps(document))
        return lambda: admissions.append(effect.effect_id)

    observed = port.execute(effect, WriteAuthority(prepare))
    assert observed.disposition == 'target'
    assert observed.pr is not None
    assert observed.pr.base == (newer if metadata_converges else pr.base)
    assert pr.base == push.base
    assert admissions == [effect.effect_id]
    assert observed.pr.title == effect.payload['title']
    writes = [json.loads(line) for line in calls.read_text().splitlines() if '--method' in json.loads(line)]
    assert len(writes) == 2 and 'PATCH' in writes[-1]


def test_delivery_reads_real_refs_and_preserves_the_reported_pr_base(tmp_path, monkeypatch):
    port, git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    _effect, pr = _publish_pr(port, push)
    newer = git.make_commit(push.base.tree, (push.base.sha,), 'Target ref advanced')
    git.run('push', 'origin', newer.sha + ':' + push.target_ref)
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, (), push.after_sha))
    assert facts.source == pr.head and facts.target == newer
    assert facts.pr is not None and facts.pr.base == push.base
    assert (push.base.sha, newer.sha) in facts.ancestry
    assert facts.checks is not None and facts.checks.result == 'none'


def test_delivery_accepts_github_repository_spelling_without_rewriting_the_observation(tmp_path, monkeypatch):
    # GitHub preserves spelling while the Runner stores a lowercase key.
    api_repository = 'Example-Owner/Greeting-Fixture'
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch,
        repository='example-owner/greeting-fixture')
    _effect, published = _publish_pr(port, push)
    document = json.loads(state.read_text())
    document['prs'][0]['base']['repo']['full_name'] = api_repository
    document['prs'][0]['head']['repo']['full_name'] = api_repository
    state.write_text(json.dumps(document))
    writes_before = [line for line in calls.read_text().splitlines() if '--method' in line]

    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref,
                                     published.number, (), push.after_sha))

    assert facts.pr is not None
    assert facts.pr.repository == facts.pr.head_repository == api_repository
    assert facts.source == published.head and facts.target == published.base
    assert facts.checks is not None and facts.checks.result == 'none'
    assert [line for line in calls.read_text().splitlines() if '--method' in line] == writes_before


@pytest.mark.parametrize('foreign', ['repository', 'target_ref', 'head', 'actor', 'base', 'missing_target'])
def test_an_ancestor_base_does_not_authorize_foreign_or_missing_pr_facts(tmp_path, monkeypatch, foreign):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    original, pr = _publish_pr(port, push)
    newer = git.make_commit(push.base.tree, (push.base.sha,), 'Current target')
    git.run('push', 'origin', newer.sha + ':' + push.target_ref)
    document = json.loads(state.read_text())
    record = document['prs'][0]
    if foreign == 'repository':
        record['base']['repo']['full_name'] = 'foreign/project'
    elif foreign == 'target_ref':
        record['base']['ref'] = 'unrelated-target'
    elif foreign == 'head':
        record['head']['sha'] = push.base.sha
    elif foreign == 'actor':
        record['user']['login'] = 'foreign-publisher'
    elif foreign == 'base':
        unrelated = git.make_commit(push.base.tree, (), 'An unrelated history')
        git.run('push', 'origin', unrelated.sha + ':refs/heads/unrelated')
        record['base']['sha'] = unrelated.sha
    elif foreign == 'missing_target':
        git.run('push', 'origin', ':' + push.target_ref)
    state.write_text(json.dumps(document))
    effect = replace(original, effect_id='current-intent', pr_number=pr.number, base=newer,
        payload={'title': 'Changed description', 'body': 'Original operation updated',
                 'prior': {'title': pr.title, 'body': pr.body}})
    admissions = []
    result = port.execute(effect, WriteAuthority(lambda: lambda: admissions.append('write')))
    assert result.disposition == 'third'
    assert admissions == []
    writes = [json.loads(line) for line in calls.read_text().splitlines() if '--method' in json.loads(line)]
    assert len(writes) == 1 and 'POST' in writes[0]


def test_parent_requires_fresh_review_and_approval_for_the_live_target(tmp_path, monkeypatch, domain_scenario):
    scenario = domain_scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == 'approval'
    kernel, subject = scenario.kernel, scenario.delivery.key
    old = scenario.world()
    assert old.target is not None
    newer = scenario.port.external_append(scenario.delivery.target_ref,
        {'external.txt': 'A new default-branch change.\n'}, 'default-target-advanced')
    original_read = SyntheticGitHub.object

    def stale(api, endpoint):
        value = original_read(api, endpoint)
        if endpoint.startswith(f'repos/{scenario.spec.repository}/pulls/') and value.get('state') == 'open':
            return {**value, 'base': {**value['base'], 'sha': old.target.sha}}
        return value

    monkeypatch.setattr(SyntheticGitHub, 'object', stale)
    world = scenario.world()
    assert world.target == newer and world.pr is not None and world.pr.base == old.target
    assert kernel.control('approve', 'old-review-cannot-approve-new-base', subject=subject, world=world) == 'updating'
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM grants WHERE subject=?', (subject,))
        assert tx.one('SELECT final_action FROM runs')[0] is None
    assert not any(w['operation'] == 'merge' for w in scenario.writes())
    reviews = scenario.review_count
    assert scenario.run().kind == 'approval'
    assert scenario.review_count == reviews + 1
    current = scenario.world()
    assert current.target == newer and current.pr is not None and current.pr.base == old.target
    kernel.control('approve', 'fresh-target-approval', subject=subject, world=current)
    assert scenario.run().kind == 'complete'
    merged = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert merged is not None
    assert scenario.port.git.parents(merged.sha) == (newer.sha, current.pr.head.sha)


def test_delivery_rejects_refs_that_change_during_the_observation(tmp_path, monkeypatch):
    from agent_run.adapters import remote
    port, git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    later = git.make_commit(push.base.tree, (push.base.sha,), 'Changed during physical read')
    read = remote.read_remote_heads
    observations = []

    def changed(*args, **kwargs):
        observations.append('read')
        if len(observations) == 2:
            git.run('push', 'origin', later.sha + ':' + push.target_ref)
        return read(*args, **kwargs)

    monkeypatch.setattr(remote, 'read_remote_heads', changed)
    with pytest.raises(ReadUnavailable, match='Remote refs changed'):
        port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, (), push.after_sha))
    assert len(observations) == 2


def test_merge_rechecks_the_original_target_after_slow_prepare(tmp_path, monkeypatch):
    port, git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    merge = replace(push, effect_id='original-merge', operation='merge', pr_number=pr.number,
        payload={'method': 'squash', 'parents': [push.base.sha], 'commit_message': pr.title + '\n\n' + pr.body})
    assert port.reconcile(merge).disposition == 'prior'
    later = git.make_commit(push.base.tree, (push.base.sha,), 'Target changed during preparation')
    admissions = []

    def prepare():
        git.run('push', 'origin', later.sha + ':' + push.target_ref)
        return lambda: admissions.append('write')

    with pytest.raises(KernelError, match='operation_boundary_changed'):
        port.execute(merge, WriteAuthority(prepare))
    assert git.run('ls-remote', 'origin', push.target_ref).stdout.strip() == later.sha + '\t' + push.target_ref
    assert admissions == []
    writes = [json.loads(line) for line in calls.read_text().splitlines() if '--method' in json.loads(line)]
    assert len(writes) == 1 and not any(call[1].endswith('/merge') for call in writes)


@pytest.mark.parametrize('foreign', ['actor', 'parents', 'tree'])
def test_merged_receipt_still_requires_the_original_actor_parents_and_tree(tmp_path, monkeypatch, foreign):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    merge = replace(push, effect_id='original-merge', operation='merge', pr_number=pr.number,
        payload={'method': 'merge', 'parents': [push.base.sha, push.after_sha],
                 'commit_message': 'Reviewed exact merge'})
    result = port.execute(merge, WriteAuthority(lambda: lambda: None))
    assert result.disposition == 'target' and result.commit is not None
    document = json.loads(state.read_text())
    record = document['prs'][0]
    if foreign == 'actor':
        record['merged_by'] = {'login': 'foreign-merger'}
    else:
        git.run('fetch', 'origin', result.commit.sha)
        parents = (result.commit.sha,) if foreign == 'parents' else (push.base.sha, push.after_sha)
        tree = result.commit.tree if foreign == 'parents' else push.base.tree
        other = git.make_commit(tree, parents, 'A different actual Git object')
        git.run('push', 'origin', other.sha + ':refs/heads/foreign-merge')
        record['merge_commit_sha'] = other.sha
    state.write_text(json.dumps(document))
    readback = port.execute(merge, WriteAuthority(lambda: lambda: pytest.fail('A foreign receipt grants no write')))
    assert readback.disposition == 'third'
    writes = [json.loads(line) for line in calls.read_text().splitlines() if '--method' in json.loads(line)]
    assert sum(call[1].endswith('/merge') for call in writes) == 1
