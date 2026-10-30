"""Merged Delivery reuses tree facts while REST still proves original parent order."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from agent_run.adapters import remote
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.errors import GitHubReadError
from support.remote_effects import _fixture, _publish_pr
from test_delivery_commit_observation import _damage_basis, _objects, _reads


def _merged(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _, pr = _publish_pr(port, push)
    merge = replace(push, effect_id='original-merge', operation='merge', pr_number=pr.number,
        payload={'method': 'merge', 'parents': [push.base.sha, push.after_sha],
                 'commit_message': 'Original ordered merge'})
    result = port.execute(merge, WriteAuthority(lambda: lambda: None))
    assert result.disposition == 'target' and result.commit is not None
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    return port, git, push, state, calls, pr, result.commit, query


def test_merged_delivery_reuses_trees_but_reads_original_parent_receipt_and_both_refs(tmp_path, monkeypatch):
    port, _, push, _, calls, pr, merged, query = _merged(tmp_path, monkeypatch)
    start, refs, original = len(_reads(calls)), [], remote.read_remote_heads

    def read_refs(*args, **options):
        refs.append(1)
        return original(*args, **options)

    monkeypatch.setattr(remote, 'read_remote_heads', read_refs)
    facts = port.delivery(query)
    assert facts.source == pr.head and facts.target == merged
    assert facts.pr.state == 'MERGED' and facts.pr.merged == merged and facts.pr.base == push.base
    assert facts.checks is None and len(refs) == 2
    commands = _reads(calls, start)
    assert _objects(commands) == {merged.sha: 1}, 'Original ordered parents still require their exact REST read'
    assert len(commands) == 2 and commands[0][:2] == ['api', 'graphql']
    assert any('parents(first:100)' in value for value in commands[0])
    assert not any('--method' in command for command in commands)


def test_parent_projection_order_never_selects_the_merged_pr_base(tmp_path, monkeypatch):
    port, _, push, _, calls, _, merged, query = _merged(tmp_path, monkeypatch)
    projected = []

    def reverse_parents(body):
        nodes = body['data']['repository']['pullRequest']['mergeCommit']['parents']['nodes']
        assert len(nodes) == 2 and nodes[0]['oid'] == push.base.sha
        nodes.reverse()
        projected.append([node['oid'] for node in nodes])

    _damage_basis(monkeypatch, reverse_parents)
    start = len(_reads(calls))
    facts = port.delivery(query)
    assert projected == [[push.after_sha, push.base.sha]]
    assert facts.pr.base == push.base and facts.pr.merged == merged
    assert _objects(_reads(calls, start)) == {merged.sha: 1}


def test_missing_first_parent_tree_uses_exact_rest_fallback_without_guessing(tmp_path, monkeypatch):
    port, _, push, _, calls, _, merged, query = _merged(tmp_path, monkeypatch)

    def subset(body):
        parents = body['data']['repository']['pullRequest']['mergeCommit']['parents']
        parents['nodes'] = [node for node in parents['nodes'] if node['oid'] != push.base.sha]

    _damage_basis(monkeypatch, subset)
    start = len(_reads(calls))
    facts = port.delivery(query)
    assert facts.pr.base == push.base and facts.pr.merged == merged
    commands = _reads(calls, start)
    assert _objects(commands) == {merged.sha: 1, push.base.sha: 1}
    assert not any('--method' in command for command in commands)


@pytest.mark.parametrize('duplicate', ['ref-parent', 'parent'])
def test_same_oid_tree_conflicts_in_merged_projection_never_supply_facts(tmp_path, monkeypatch, duplicate):
    port, _, _, _, calls, _, _, query = _merged(tmp_path, monkeypatch)

    def conflict(body):
        merge = body['data']['repository']['pullRequest']['mergeCommit']
        if duplicate == 'ref-parent':
            merge['parents']['nodes'][1]['tree']['oid'] = 'a' * 40
        else:
            node = merge['parents']['nodes'][0]
            merge['parents']['nodes'].append({**node, 'tree': {'oid': 'a' * 40}})

    _damage_basis(monkeypatch, conflict)
    start = len(_reads(calls))
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == 'github_invalid_response'
    commands = _reads(calls, start)
    assert len(commands) == 1 and not any('--method' in command for command in commands)


def test_projected_original_parent_tree_still_matches_the_exact_local_object(tmp_path, monkeypatch):
    port, _, push, _, calls, _, merged, query = _merged(tmp_path, monkeypatch)

    def wrong_tree(body):
        nodes = body['data']['repository']['pullRequest']['mergeCommit']['parents']['nodes']
        parent, = [node for node in nodes if node['oid'] == push.base.sha]
        parent['tree']['oid'] = 'a' * 40

    _damage_basis(monkeypatch, wrong_tree)
    start = len(_reads(calls))
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == 'github_observation_pending'
    assert _objects(_reads(calls, start)) == {merged.sha: 1}


def test_deleted_source_does_not_use_an_unordered_parent_tree_for_pr_head(tmp_path, monkeypatch):
    port, git, _, _, calls, pr, merged, query = _merged(tmp_path, monkeypatch)
    git.run('push', 'origin', ':' + query.source_ref)

    def wrong_second_parent_tree(body):
        nodes = body['data']['repository']['pullRequest']['mergeCommit']['parents']['nodes']
        parent, = [node for node in nodes if node['oid'] == pr.head.sha]
        assert parent['tree']['oid'] == pr.head.tree
        parent['tree']['oid'] = 'a' * 40

    _damage_basis(monkeypatch, wrong_second_parent_tree)
    start = len(_reads(calls))
    facts = port.delivery(query)
    assert facts.source is None and facts.target == merged
    assert facts.pr.head == pr.head and facts.pr.merged == merged
    assert _objects(_reads(calls, start)) == {merged.sha: 1, pr.head.sha: 1}


@pytest.mark.parametrize('damage', ['missing-node', 'short-oid', 'missing-tree'])
def test_incomplete_or_malformed_parent_tree_is_not_silently_skipped(tmp_path, monkeypatch, damage):
    port, _, _, _, calls, _, _, query = _merged(tmp_path, monkeypatch)
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None))

    def broken(body):
        nodes = body['data']['repository']['pullRequest']['mergeCommit']['parents']['nodes']
        if damage == 'missing-node':
            nodes[0] = None
        elif damage == 'short-oid':
            nodes[0]['oid'] = nodes[0]['oid'][:8]
        else:
            nodes[0].pop('tree')

    _damage_basis(monkeypatch, broken)
    start = len(_reads(calls))
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == ('github_read_failed' if damage == 'missing-node' else 'github_invalid_response')
    commands = _reads(calls, start)
    assert len(commands) == (3 if damage == 'missing-node' else 1)
    assert not any('--method' in command for command in commands)


def test_new_observation_does_not_reuse_a_previous_parent_tree(tmp_path, monkeypatch):
    port, _, push, _, calls, _, merged, query = _merged(tmp_path, monkeypatch)
    assert port.delivery(query).pr.base == push.base

    def subset(body):
        parents = body['data']['repository']['pullRequest']['mergeCommit']['parents']
        parents['nodes'] = [node for node in parents['nodes'] if node['oid'] != push.base.sha]

    _damage_basis(monkeypatch, subset)
    original, requested = port._api, []

    def unavailable(endpoint, *args, **options):
        requested.append(endpoint)
        if endpoint.endswith('/git/commits/' + push.base.sha):
            raise GitHubReadError('github_read_failed', 'Original parent is now unavailable')
        return original(endpoint, *args, **options)

    monkeypatch.setattr(port, '_api', unavailable)
    start = len(_reads(calls))
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == 'github_read_failed'
    assert requested == [f'repos/{port.repository}/git/commits/{merged.sha}',
                         f'repos/{port.repository}/git/commits/{push.base.sha}']
    assert _reads(calls, start)[0][:2] == ['api', 'graphql']
