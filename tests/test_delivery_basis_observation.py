"""Fresh Delivery basis batching; real private Git and synthetic gh, no native Host."""
from dataclasses import replace
from types import SimpleNamespace
import subprocess
import json

import pytest

from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.adapters import github_commands, remote_observation as remote
from agent_run.errors import GitHubReadError
from support.delivery_packets import rejected_basis
from support.remote_effects import _fixture, existing_pr


@pytest.mark.parametrize('mode,expected', [('known', 1), ('discovery', 2), ('without-pr', 1)])
def test_delivery_basis_uses_one_packet_without_removing_ref_or_check_boundaries(tmp_path, monkeypatch, mode, expected):
    port, _, push, _, calls, pr = existing_pr(tmp_path, monkeypatch)
    start = len(calls.read_text().splitlines())
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number if mode == 'known' else None,
                         (), read_pr=mode != 'without-pr', read_checks=False)
    refs, original = [], remote.read_remote_heads
    def read_refs(*args, **kwargs):
        refs.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr('agent_run.adapters.remote_observation.read_remote_heads', read_refs)
    facts = port.delivery(query)
    assert facts.source == pr.head and facts.target == pr.base and facts.publisher_actor == 'publisher'
    assert facts.pr == (None if mode == 'without-pr' else pr) and facts.checks is None
    commands = [json.loads(line) for line in calls.read_text().splitlines()[start:]]
    assert len(commands) == expected and len(refs) == 2
    assert sum(command[:2] == ['api', 'graphql'] for command in commands) == 1
    assert not any('--method' in command for command in commands)


def test_base_snapshot_may_lag_live_target_without_becoming_its_tip(tmp_path, monkeypatch):
    port, git, push, _, calls, pr = existing_pr(tmp_path, monkeypatch)
    later = git.make_commit(pr.base.tree, (pr.base.sha,), 'Later independent base')
    git.run('push', 'origin', later.sha + ':' + push.target_ref)
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False))
    assert facts.target == later and facts.pr.base == pr.base
    assert (pr.base.sha, later.sha) in facts.ancestry
    assert facts.pr.base != facts.target


@pytest.mark.parametrize('repository', [None, 'foreign/project'])
def test_closed_fork_or_deleted_head_does_not_become_selected_repository(tmp_path, monkeypatch, repository):
    port, _, push, state, _, pr = existing_pr(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document['prs'][0]['state'] = 'closed'
    document['prs'][0]['head']['repo'] = None if repository is None else {'full_name': repository}
    state.write_text(json.dumps(document))
    facts = port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False))
    assert facts.pr.head_repository == repository and facts.pr.head == pr.head
    assert facts.pr.state == 'CLOSED'


@pytest.mark.parametrize('part', ['repository', 'object', 'viewer', 'pr', 'base-repository', 'merge-object', 'partial-errors'])
def test_incomplete_basis_never_supplies_facts_or_writes(tmp_path, monkeypatch, part):
    def missing(body):
        data = body['data']
        if part == 'repository': data['repository'] = None
        elif part == 'viewer': data['viewer'] = None
        elif part == 'pr': data['repository']['pullRequest'] = None
        elif part == 'base-repository': data['repository']['pullRequest']['baseRepository'] = None
        elif part == 'merge-object': data['repository']['pullRequest'].update(state='MERGED', mergeCommit=None)
        elif part == 'object': data['repository']['object0'] = None
        else: body['errors'] = [{'message': 'Controlled incomplete permission response'}]
    port, query, commands = rejected_basis(tmp_path, monkeypatch, missing)
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == 'github_read_failed'
    assert len(commands) == 3 and not any('--method' in command for command in commands)


def test_same_head_checks_and_both_pr_envelopes_are_fresh_on_next_delivery(tmp_path, monkeypatch):
    port, _, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, ())
    first = port.delivery(query)
    document = json.loads(state.read_text())
    document['checks'] = [{'name': 'quality', 'bucket': 'pass'}]
    state.write_text(json.dumps(document))
    start = len(calls.read_text().splitlines())
    second = port.delivery(query)
    assert second.source == first.source and second.checks != first.checks
    commands = [json.loads(line) for line in calls.read_text().splitlines()[start:]]
    assert sum(command[:2] == ['api', 'repos/example/project/pulls/17'] for command in commands) == 2
    assert sum('CurrentRequiredChecks' in arg for command in commands for arg in command) == 1


def test_app_keeps_original_resolver_and_does_not_query_viewer(tmp_path, monkeypatch):
    port, _, _, _, calls = _fixture(tmp_path, monkeypatch)
    port.identity = {'source': 'app', 'app_profile': {'app_id': '123', 'installation_id': '456',
        'private_key_path': str(tmp_path / 'synthetic-key')}, 'app_read_only': False, 'app_current_repository_only': True}
    # This component contract replaces only App credentials and ref transport;
    # it makes no claim about real App authentication or a native Host.
    monkeypatch.setattr(remote, 'read_remote_heads', lambda *a, **k: (None, None))
    monkeypatch.setattr(remote, 'runner_options', lambda _: {})
    resolved = []
    monkeypatch.setattr('agent_run.adapters.auth.app.resolve_app_login', lambda profile: resolved.append(profile) or 'original-app[bot]')
    facts = port.delivery(DeliveryRead('refs/heads/missing-source', 'refs/heads/missing-target', None, (), read_pr=False))
    assert facts.publisher_actor == 'original-app[bot]' and len(resolved) == 1
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(commands) == 1 and not any('viewer' in arg for arg in commands[0])


@pytest.mark.parametrize('boundary', ['third-overflow', 'compact-single-overflow', 'compact-malformed', 'deadline', 'cancelled'])
def test_basis_fallback_shares_attempt_and_absolute_deadline_budget(tmp_path, monkeypatch, boundary):
    port = remote.RemoteObservation(tmp_path, "example/project", runner_identity={"source": "host"})
    now, calls = [0.], []
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n)))
    if boundary == 'cancelled': port.cancelled = lambda: True
    def read(command, **options):
        calls.append((command, options['timeout']))
        if boundary == 'deadline':
            now[0] = 91.
            return subprocess.CompletedProcess(command, 0, '{}', '')
        if boundary == 'third-overflow' and len(calls) <= 2:
            return subprocess.CompletedProcess(command, 1, '', 'HTTP 503')
        if boundary == 'compact-malformed' and len(calls) == 2:
            data = {'data': {'repository': {'id': 'repo', 'nameWithOwner': port.repository}, 'viewer': {'login': 'publisher'}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(data), '')
        if boundary == 'compact-malformed' and len(calls) == 3:
            data = {'number': 17, 'title': 'Original', 'body': 'Original', 'state': 'open', 'merged': False,
                    'head': {'sha': 'a'*40, 'ref': 12}}
            return subprocess.CompletedProcess(command, 0, json.dumps(data), '')
        raise github_commands.ReadOutputOverflow('github_invalid_response', 'Controlled capacity evidence')
    monkeypatch.setattr(github_commands, '_run_bounded_command', read)
    expected = InterruptedError if boundary == 'cancelled' else GitHubReadError
    with pytest.raises(expected) as caught:
        port._delivery_basis(None, None, 17)
    if boundary != 'cancelled':
        assert caught.value.code == ('github_invalid_response' if boundary in {'compact-single-overflow', 'compact-malformed'} else 'github_read_failed')
    assert len(calls) == {'third-overflow': 3, 'compact-single-overflow': 2, 'compact-malformed': 3, 'deadline': 1, 'cancelled': 0}[boundary]
    assert all(timeout <= 30 for _, timeout in calls)


@pytest.mark.parametrize('oversized', [False, True], ids=['aggregate-only', 'single-pr'])
def test_real_capacity_fallback_does_not_truncate_or_regrant_budget(tmp_path, monkeypatch, oversized):
    from support.github_delivery import delivery_basis
    port, git, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document['prs'][0]['body'] = ''
    fields = {'query': 'viewer { login }', 'pr': str(pr.number),
              **{'object' + str(i): sha for i, sha in enumerate(sorted({pr.head.sha, pr.base.sha}))}}
    projected = delivery_basis(document, fields, lambda *args: git.run(*args).stdout,
                               repository=port.repository, actor='publisher')
    packet_size = len(json.dumps(projected).encode())
    body = 'B' * (github_commands.MAX_COMMAND_OUTPUT_BYTES + 1 if oversized else
                  github_commands.MAX_COMMAND_OUTPUT_BYTES - packet_size + 1)
    document['prs'][0]['body'] = body
    assert oversized or len(json.dumps(document['prs'][0]).encode()) < github_commands.MAX_COMMAND_OUTPUT_BYTES
    state.write_text(json.dumps(document))
    start = len(calls.read_text().splitlines())
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    if oversized:
        with pytest.raises(ReadUnavailable) as caught:
            port.delivery(query)
        assert caught.value.code == 'github_invalid_response'
    else:
        assert port.delivery(query).pr.body == body
    commands = [json.loads(line) for line in calls.read_text().splitlines()[start:]]
    assert len(commands) == 3  # Two projections and the compact PR endpoint, not extra retries.
    assert commands[-1][:2] == ['api', 'repos/example/project/pulls/17']
    assert not any('--method' in command for command in commands)


def test_refs_change_after_basis_is_rejected_before_delivery_returns(tmp_path, monkeypatch):
    port, git, push, _, _, pr = existing_pr(tmp_path, monkeypatch)
    later = git.make_commit(pr.head.tree, (pr.head.sha,), 'Concurrent source replacement')
    original = port._delivery_basis
    def basis(*args):
        result = original(*args)
        git.run('push', 'origin', later.sha + ':' + push.source_ref)
        return result
    monkeypatch.setattr(port, '_delivery_basis', basis)
    with pytest.raises(ReadUnavailable, match='Remote refs changed'):
        port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False))


def test_merged_delivery_still_rejects_a_malformed_original_parent_receipt(tmp_path, monkeypatch):
    from agent_run.contracts.write_authority import WriteAuthority
    port, _, push, _, _, pr = existing_pr(tmp_path, monkeypatch)
    merge = replace(push, effect_id='merge', operation='merge', pr_number=pr.number,
        payload={'method': 'merge', 'parents': [push.base.sha, push.after_sha], 'commit_message': 'Original merge'})
    result = port.execute(merge, WriteAuthority(lambda: lambda: None))
    assert result.disposition == 'target'
    api = port.object
    def bad_parents(endpoint, *args, **kwargs):
        data = api(endpoint, *args, **kwargs)
        if endpoint.endswith('/git/commits/' + result.commit.sha):
            data['parents'] = [{'sha': push.base.sha[:8]}]
        return data
    monkeypatch.setattr(port, 'object', bad_parents)
    with pytest.raises(ValueError, match='invalid commit identity'):
        port.delivery(DeliveryRead(push.source_ref, push.target_ref, pr.number, ()))


@pytest.mark.parametrize('damage', ['repository-type', 'pr-repository-type', 'missing-data', 'errors-type'])
def test_complete_malformed_basis_is_rejected_without_retry(tmp_path, monkeypatch, damage):
    def malformed(body):
        if damage == 'repository-type': body['data']['repository']['nameWithOwner'] = 42
        elif damage == 'pr-repository-type': body['data']['repository']['pullRequest']['repository']['nameWithOwner'] = 42
        elif damage == 'missing-data': body.pop('data')
        else: body['errors'] = {'message': 'Invalid envelope'}
    port, query, commands = rejected_basis(tmp_path, monkeypatch, malformed)
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == 'github_invalid_response'
    assert len(commands) == 1
