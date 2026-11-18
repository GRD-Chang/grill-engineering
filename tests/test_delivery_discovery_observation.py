"""Discover numbers inside one fresh basis; real Git and synthetic gh CLI."""
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_run.adapters import github_commands, remote_observation as remote
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.errors import GitHubReadError
from support.delivery_packets import rejected_basis
from support.remote_effects import _fixture, _publish_pr, existing_pr
from test_delivery_commit_observation import _damage_basis, _reads


def test_absence_uses_one_packet_and_next_delivery_rediscovers(tmp_path, monkeypatch):
    port, _, push, _, calls = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_checks=False)
    start = len(_reads(calls))
    first = port.delivery(query)
    commands = _reads(calls, start)
    assert first.pr is None and len(commands) == 1
    assert commands[0][:2] == ['api', 'graphql']
    _, pr = _publish_pr(port, push)
    start = len(_reads(calls))
    second = port.delivery(query)
    assert second.pr == pr and second.source == pr.head and second.source != first.source
    commands = _reads(calls, start)
    assert len(commands) == 2 and commands[-1][:2] == ['api', 'repos/example/project/pulls/17']


@pytest.mark.parametrize('owner', ['foreign', 'EXAMPLE'])
def test_discovery_preserves_owner_filter_and_same_owner_ambiguity(tmp_path, monkeypatch, owner):
    port, _, push, state, _, pr = existing_pr(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    other = deepcopy(document['prs'][0])
    other['number'] = 18
    other['head']['repo'] = {'full_name': owner + '/fork'}
    document['prs'].append(other)
    state.write_text(json.dumps(document))
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_checks=False)
    if owner == 'foreign':
        assert port.delivery(query).pr == pr
    else:
        with pytest.raises(ReadUnavailable) as caught:
            port.delivery(query)
        assert caught.value.code == 'github_invalid_response'


@pytest.mark.parametrize('reason', ['more', 'deleted-owner'])
def test_incomplete_discovery_identity_uses_original_rest_collection(tmp_path, monkeypatch, reason):
    port, _, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    if reason == 'deleted-owner':
        document = json.loads(state.read_text())
        document['prs'][0]['head']['repo'] = None
        document['prs'][0]['state'] = 'closed'
        state.write_text(json.dumps(document))
    else:
        _damage_basis(monkeypatch, lambda body: body['data']['repository']['discovery']['pageInfo'].update(hasNextPage=True))
    start = len(_reads(calls))
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, None, (), read_checks=False))
    assert facts.pr.number == pr.number
    assert facts.pr.head_repository == (None if reason == 'deleted-owner' else port.repository)
    commands = _reads(calls, start)
    assert len(commands) == 3 and '--paginate' in commands[1] and '--slurp' in commands[1]
    assert commands[2][:2] == ['api', 'repos/example/project/pulls/17']


@pytest.mark.parametrize('damage', ['null', 'nodes', 'page', 'owner', 'refs', 'number', 'partial'])
def test_malformed_or_partial_discovery_never_becomes_absence_or_fallback(tmp_path, monkeypatch, damage):
    def corrupt(body):
        connection = body['data']['repository']['discovery']
        if damage == 'null': body['data']['repository']['discovery'] = None
        elif damage == 'nodes': connection['nodes'] = [None]
        elif damage == 'page': connection['pageInfo']['hasNextPage'] = 0
        elif damage == 'owner': connection['nodes'][0]['headRepositoryOwner'] = {}
        elif damage == 'refs': connection['nodes'][0]['baseRefName'] = 'foreign'
        elif damage == 'number': connection['nodes'][0]['number'] = True
        else: body['errors'] = [{'message': 'Permission unavailable'}]
    port, query, commands = rejected_basis(tmp_path, monkeypatch, corrupt, discover=True)
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == ('github_read_failed' if damage == 'partial' else 'github_invalid_response')
    assert len(commands) == (3 if damage == 'partial' else 1)
    assert all(command[:3] == ['gh', 'api', 'graphql'] for command in commands)


def test_discovery_row_is_only_a_number_and_pr_details_are_read_afterwards(tmp_path, monkeypatch):
    port, _, push, state, _, _ = existing_pr(tmp_path, monkeypatch)
    def changed_after_packet(body):
        document = json.loads(state.read_text())
        document['prs'][0]['title'] = 'Changed after discovery'
        state.write_text(json.dumps(document))
    _damage_basis(monkeypatch, changed_after_packet)
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, None, (), read_checks=False))
    assert facts.pr.title == 'Changed after discovery'


@pytest.mark.parametrize('boundary', ['compact', 'third-overflow', 'deadline'])
def test_discovery_fallback_keeps_original_attempts_and_deadline(tmp_path, monkeypatch, boundary):
    port = remote.RemoteObservation(tmp_path, "example/project", runner_identity={"source": "host"})
    now, calls = [0.], []
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n)))
    def read(command, **options):
        calls.append((command, options['timeout']))
        if boundary == 'third-overflow' and len(calls) < 3:
            return subprocess.CompletedProcess(command, 1, '', 'HTTP 503')
        if boundary == 'deadline':
            now[0] = 91.
        elif boundary == 'compact' and len(calls) == 2:
            assert not any('discovery:' in arg for arg in command)
            body = {'data': {'repository': {'id': 'repo', 'nameWithOwner': port.repository}, 'viewer': {'login': 'publisher'}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), '')
        elif boundary == 'compact' and len(calls) == 3:
            assert '--paginate' in command and '--slurp' in command
            return subprocess.CompletedProcess(command, 0, '[[]]', '')
        raise github_commands.ReadOutputOverflow('github_invalid_response', 'Controlled complete-output capacity')
    monkeypatch.setattr(github_commands, '_run_bounded_command', read)
    if boundary == 'compact':
        assert port._delivery_basis(None, None, None, ('refs/heads/run', 'refs/heads/main')) == ({}, None, 'publisher')
    else:
        with pytest.raises(GitHubReadError) as caught:
            port._delivery_basis(None, None, None, ('refs/heads/run', 'refs/heads/main'))
        assert caught.value.code == 'github_read_failed'
    assert len(calls) == (1 if boundary == 'deadline' else 3)
    assert all(timeout <= 30 for _, timeout in calls)
