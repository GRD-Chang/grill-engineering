"""Combined observation contracts over private real Git and the existing fake gh."""
from dataclasses import replace
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_run.adapters import github, github_commands, remote
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from support.remote_effects import _fixture, existing_pr


def _query(command):
    return next((value[6:] for value in command if value.startswith('query=')), '')


def _reads(path, start=0):
    return [json.loads(line) for line in path.read_text().splitlines()[start:]]


def _no_backoff(monkeypatch):
    monkeypatch.setattr(github, 'time', SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None))


@pytest.mark.parametrize('failure', ['malformed', 'scoped-nonzero'])
def test_complete_source_returns_before_delivery_failure(tmp_path, monkeypatch, failure):
    port, _, push, _, calls, pr = existing_pr(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    original = github_commands._run_bounded_command
    start = len(_reads(calls))
    _no_backoff(monkeypatch)

    def response(command, **options):
        result = original(command, **options)
        selected = _query(command)
        if 'DeliveryBasis' in selected:
            envelope = json.loads(result.stdout)
            combined = 'SourceSnapshotDeliveryBasis' in selected
            if failure == 'malformed':
                envelope['data']['delivery' if combined else 'repository']['pullRequest']['title'] = 42
            else:
                envelope['errors'] = [{'message': 'Delivery permission unavailable',
                    'path': ['delivery' if combined else 'repository', 'pullRequest']}]
                result.returncode = 1
            result.stdout = json.dumps(envelope)
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.body == 'Whole requirement'
    assert len(_reads(calls, start)) == 1
    with pytest.raises(ReadUnavailable) as caught:
        port.complete_observation(query, prepared)
    assert caught.value.code == ('github_invalid_response' if failure == 'malformed' else 'github_read_failed')
    observed = _reads(calls, start)
    assert len(observed) == (1 if failure == 'malformed' else 3)
    assert not any('--method' in command for command in observed)


def test_same_oid_with_contradictory_projected_trees_is_rejected(tmp_path, monkeypatch):
    port, _, push, _, _ = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False, read_checks=False)
    original = github_commands._run_bounded_command

    def response(command, **options):
        result = original(command, **options)
        if 'SourceSnapshotDeliveryBasis' in _query(command):
            envelope = json.loads(result.stdout)
            delivery = envelope['data']['delivery']
            assert delivery['sourceRef']['target']['oid'] == delivery['targetRef']['target']['oid']
            delivery['targetRef']['target']['tree']['oid'] = 'f' * 40
            result.stdout = json.dumps(envelope)
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.number == 1
    with pytest.raises(ReadUnavailable) as caught:
        port.complete_observation(query, prepared)
    assert caught.value.code == 'github_invalid_response'


def test_short_name_fallback_discards_projected_facts_and_rereads(tmp_path, monkeypatch):
    port, _, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    original = github_commands._run_bounded_command
    start = len(_reads(calls))

    def response(command, **options):
        result = original(command, **options)
        if 'SourceSnapshotDeliveryBasis' in _query(command):
            envelope = json.loads(result.stdout)
            envelope['data']['delivery']['sourceRef']['prefix'] = 'refs/tags/'
            envelope['data']['delivery']['pullRequest']['title'] = 42
            result.stdout = json.dumps(envelope)
            document = json.loads(state.read_text())
            document['prs'][0]['title'] = 'Fresh exact-ref PR'
            state.write_text(json.dumps(document))
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    facts = port.complete_observation(query, prepared)
    assert facts.source == pr.head and facts.target == pr.base
    assert facts.pr.title == 'Fresh exact-ref PR'
    observed = _reads(calls, start)
    assert len(observed) == 2 and all('DeliveryBasis' in _query(command) for command in observed)


def test_source_pagination_retry_discards_basis_and_preserves_two_delivery_attempts(tmp_path, monkeypatch):
    port, _, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document['source_graph'] = {
        'parent': {'number': 1, 'title': 'Parent', 'body': 'Original Source', 'sub_issues': [2]},
        'issues': {'2': {'number': 2, 'title': 'Ticket', 'body': 'Ticket body',
                        'state': 'OPEN', 'labels': ['ready-for-agent']}}}
    state.write_text(json.dumps(document))
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    original = github_commands._run_bounded_command
    deliveries = []
    start = len(_reads(calls))
    _no_backoff(monkeypatch)

    def response(command, **options):
        selected = _query(command)
        result = original(command, **options)
        if 'SourceSnapshotDeliveryBasis' in selected:
            envelope = json.loads(result.stdout)
            labels = envelope['data']['repository']['issue']['subIssues']['nodes'][0]['labels']
            labels.update(totalCount=2, pageInfo={'hasNextPage': True, 'endCursor': 'second-label'})
            envelope['data']['delivery']['pullRequest']['title'] = 42
            result.stdout = json.dumps(envelope)
        elif 'SourceTicket' in selected:
            document = json.loads(state.read_text())
            document['source_graph']['parent']['body'] = 'Fresh complete Source'
            document['prs'][0]['title'] = 'Fresh Delivery after Source retry'
            state.write_text(json.dumps(document))
            return subprocess.CompletedProcess(command, 1, '', 'HTTP 503 Source continuation unavailable')
        elif 'DeliveryBasis' in selected:
            deliveries.append(1)
            if len(deliveries) == 1:
                return subprocess.CompletedProcess(command, 1, '', 'HTTP 503 Delivery temporarily unavailable')
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.body == 'Fresh complete Source'
    assert prepared.source.issues[0].labels == frozenset({'ready-for-agent'})
    facts = port.complete_observation(query, prepared)
    assert facts.pr.title == 'Fresh Delivery after Source retry'
    assert len(deliveries) == 2
    queries = [_query(command) for command in _reads(calls, start)]
    assert sum('SourceSnapshotDeliveryBasis' in value for value in queries) == 1
    assert sum('query SourceSnapshot(' in value for value in queries) == 1


def test_changed_core_demand_discards_old_delivery_error(tmp_path, monkeypatch):
    port, _, push, _, calls, pr = existing_pr(tmp_path, monkeypatch)
    speculative = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    actual = replace(speculative, pr_number=None, read_pr=False)
    original = github_commands._run_bounded_command
    start = len(_reads(calls))

    def response(command, **options):
        result = original(command, **options)
        if 'SourceSnapshotDeliveryBasis' in _query(command):
            envelope = json.loads(result.stdout)
            envelope['errors'] = [{'message': 'Unavailable speculative PR', 'path': ['delivery', 'pullRequest']}]
            result.stdout, result.returncode = json.dumps(envelope), 1
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, speculative)
    facts = port.complete_observation(actual, prepared)
    assert facts.source == pr.head and facts.target == pr.base and facts.pr is None
    observed = _reads(calls, start)
    assert len(observed) == 2 and 'pullRequest(' not in _query(observed[-1])


def test_closing_refs_cover_changes_after_required_checks_complete(tmp_path, monkeypatch):
    port, git, push, _, _, pr = existing_pr(tmp_path, monkeypatch)
    later = git.make_commit(pr.head.tree, (pr.head.sha,), 'Concurrent update after checks')
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, ())
    checked = []
    original = remote.read_required_check_facts

    def checks(*args, **kwargs):
        result = original(*args, **kwargs)
        checked.append(1)
        git.run('push', 'origin', later.sha + ':' + push.source_ref)
        return result

    monkeypatch.setattr(remote, 'read_required_check_facts', checks)
    prepared = port.source_observation(1, query)
    with pytest.raises(ReadUnavailable) as caught:
        port.complete_observation(query, prepared)
    assert checked == [1]
    assert caught.value.code == 'github_observation_pending'


def test_joint_capacity_overflow_splits_two_individually_legal_materials(tmp_path, monkeypatch):
    port, _, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document['source_body'] = 'S' * 600_000
    document['prs'][0]['body'] = 'P' * 600_000
    state.write_text(json.dumps(document))
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (), read_checks=False)
    start = len(_reads(calls))
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.body == document['source_body']
    facts = port.complete_observation(query, prepared)
    assert facts.pr.body == document['prs'][0]['body']
    observed = _reads(calls, start)
    assert len(observed) == 3
    assert 'SourceSnapshotDeliveryBasis' in _query(observed[0])
    assert 'query SourceSnapshot(' in _query(observed[1])
    assert 'query DeliveryBasis(' in _query(observed[2])


def test_delivery_completion_keeps_source_absolute_deadline(tmp_path, monkeypatch):
    port, _, push, _, _ = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False, read_checks=False)
    now = [100.25]
    observed = []
    original = github_commands._run_bounded_command
    monkeypatch.setattr(github, 'monotonic', lambda: now[0])
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda _: None))

    def response(command, **options):
        observed.append((command, options['timeout']))
        result = original(command, **options)
        if 'SourceSnapshotDeliveryBasis' in _query(command):
            now[0] = 170.25
        elif command[:2] == ['git', 'ls-remote']:
            now[0] = 190.26
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.number == 1
    with pytest.raises(ReadUnavailable) as caught:
        port.complete_observation(query, prepared)
    assert caught.value.code == 'github_read_failed'
    assert [timeout for _, timeout in observed] == [30, 20]


@pytest.mark.parametrize('observation', ['absent', 'appeared', 'target-null'])
def test_qualified_ref_absence_is_distinct_from_unavailable_target(tmp_path, monkeypatch, observation):
    port, git, push, _, _ = _fixture(tmp_path, monkeypatch)
    source_ref, target_ref = ((push.source_ref, push.target_ref) if observation == 'target-null' else
                             ('refs/heads/missing-source', 'refs/heads/missing-target'))
    query = DeliveryRead(source_ref, target_ref, None, (), read_pr=False, read_checks=False)
    original = github_commands._run_bounded_command
    read_refs = remote.read_remote_heads
    observed_refs = []

    def response(command, **options):
        result = original(command, **options)
        if observation == 'target-null' and 'SourceSnapshotDeliveryBasis' in _query(command):
            envelope = json.loads(result.stdout)
            envelope['data']['delivery']['sourceRef']['target'] = None
            result.stdout = json.dumps(envelope)
        return result

    def refs(*args, **kwargs):
        result = read_refs(*args, **kwargs)
        observed_refs.append(result)
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    monkeypatch.setattr(remote, 'read_remote_heads', refs)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.body == 'Whole requirement'
    if observation == 'appeared':
        git.run('push', 'origin', push.base.sha + ':' + source_ref)
    if observation == 'absent':
        facts = port.complete_observation(query, prepared)
        assert facts.source is None and facts.target is None and facts.pr is None
        assert observed_refs == [(None, None), (None, None)]
    else:
        with pytest.raises(ReadUnavailable) as caught:
            port.complete_observation(query, prepared)
        assert caught.value.code == ('github_observation_pending' if observation == 'appeared'
                                     else 'github_read_failed')


@pytest.mark.parametrize('failure', ['unpathed', 'invalid-root', 'invalid-complete-output'])
def test_global_failure_discards_partial_source_and_leaves_two_source_attempts(tmp_path, monkeypatch, failure):
    port, _, push, state, calls = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False, read_checks=False)
    original = github_commands._run_bounded_command
    source_retries = []
    _no_backoff(monkeypatch)

    def response(command, **options):
        result = original(command, **options)
        selected = _query(command)
        if 'SourceSnapshotDeliveryBasis' in selected:
            envelope = json.loads(result.stdout)
            envelope['data']['repository']['issue']['body'] = 'Unusable partial Source'
            envelope['errors'] = [{'message': 'Global GraphQL error without a path'}]
            result.stdout, result.returncode = json.dumps(envelope), 1
            document = json.loads(state.read_text())
            document['source_body'] = 'Fresh Source after global failure'
            state.write_text(json.dumps(document))
            if failure == 'invalid-root':
                result.stdout, result.returncode = '{"data":42}', 0
            elif failure == 'invalid-complete-output':
                raise remote.GitHubReadError('github_invalid_response', 'Incomplete structured stdout')
        elif 'query SourceSnapshot(' in selected:
            source_retries.append(1)
            if len(source_retries) == 1:
                return subprocess.CompletedProcess(command, 1, '', 'HTTP 503 Source temporarily unavailable')
        return result

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    assert prepared.source.parent.body == 'Fresh Source after global failure'
    assert len(source_retries) == 2
    observed = _reads(calls)
    assert len(observed) == 3
    assert sum('SourceSnapshotDeliveryBasis' in _query(command) for command in observed) == 1
    facts = port.complete_observation(query, prepared)
    assert facts.source == push.base and facts.target == push.base
    assert len(_reads(calls)) == 4


def test_final_refs_cover_original_app_actor_resolution(tmp_path, monkeypatch):
    port, git, push, _, _ = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False, read_checks=False)
    port.identity = {'source': 'app', 'app_profile': {'app_id': '123', 'installation_id': '456',
        'private_key_path': str(tmp_path / 'synthetic-key')}, 'app_read_only': False,
        'app_current_repository_only': True}
    # Isolate App credential minting only; original resolver dispatch and real
    # Git ref/object reads remain. This is not real App authentication evidence.
    original = remote.read_remote_heads
    calls = []
    def refs(*args, **options):
        options['runner_identity'] = {'source': 'host'}
        return original(*args, **options)
    def actor(profile):
        calls.append(profile)
        git.run('push', 'origin', push.after_sha + ':' + push.source_ref)
        return 'original-app[bot]'
    monkeypatch.setattr(remote, 'read_remote_heads', refs)
    monkeypatch.setattr('agent_run.adapters.auth.app.resolve_app_login', actor)
    with pytest.raises(remote.GitHubReadError, match='Remote refs changed'):
        port._finish_delivery(query, (push.base.sha, push.base.sha),
            {push.base.sha: push.base}, None, None)
    assert len(calls) == 1
    assert (calls[0].app_id, calls[0].installation_id, calls[0].private_key_path) == (
        '123', '456', tmp_path / 'synthetic-key')


@pytest.mark.parametrize('output', ['valid', 'oversized-valid-tail', 'invalid-utf8', 'missing-eof'])
@pytest.mark.parametrize('returncode', [0, 1])
def test_combined_source_requires_complete_original_stdout(tmp_path, monkeypatch, output, returncode):
    port, _, push, _, calls = _fixture(tmp_path, monkeypatch)
    query = DeliveryRead(push.source_ref, push.target_ref, None, (), read_pr=False, read_checks=False)
    bounded = github_commands._run_bounded_command
    capture = github_commands.capture_command
    replaying = [False]
    _no_backoff(monkeypatch)

    def captured(process, **options):
        result = capture(process, **options)
        if replaying[0] and output == 'missing-eof':
            # The actual child was drained and reaped. Remove only its stdout
            # EOF evidence to exercise the incomplete-capture boundary.
            assert process.poll() == result.returncode == returncode
            assert options['stdout'].eof
            options['stdout'].eof = False
        return result

    def response(command, **options):
        result = bounded(command, **options)
        if 'SourceSnapshotDeliveryBasis' not in _query(command):
            return result
        envelope = json.loads(result.stdout)
        envelope['data']['repository']['issue']['body'] = 'Joint response body'
        envelope['errors'] = [{'message': 'Delivery branch unavailable', 'path': ['delivery', 'sourceRef']}]
        packet = json.dumps(envelope).encode()
        if output == 'invalid-utf8':
            packet = packet.replace(b'Joint response body', b'Joint \xff response body')
        prefix = github_commands.MAX_COMMAND_OUTPUT_BYTES if output == 'oversized-valid-tail' else 0
        # Build the large prefix in the child, keeping argv small. Its complete
        # legal JSON tail alone must never establish complete Source material.
        program = f"import sys;sys.stdout.buffer.write(b' ' * {prefix} + {packet!r});raise SystemExit({returncode})"
        replaying[0] = True
        try:
            return bounded([sys.executable, '-I', '-c', program], **options)
        finally:
            replaying[0] = False

    monkeypatch.setattr(github_commands, 'capture_command', captured)
    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    prepared = port.source_observation(1, query)
    if output == 'valid':
        assert prepared.source.parent.body == 'Joint response body'
        assert prepared.physical.observation.delivery_packet is not None
        assert len(_reads(calls)) == 1
    else:
        assert prepared.source.parent.body == 'Whole requirement'
        assert prepared.physical.observation.delivery_packet is None
        observed = _reads(calls)
        assert len(observed) == 2
        assert 'SourceSnapshotDeliveryBasis' in _query(observed[0])
        assert 'query SourceSnapshot(' in _query(observed[-1])
