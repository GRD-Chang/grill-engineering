"""Explicit checks demand keeps fresh PR/ref facts and later required reads."""
from dataclasses import replace
import json

import pytest

from agent_run.adapters import remote
from agent_run.adapters.remote import RemoteEffects
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.model import Effect, Execution
from support.offline import OfflinePort
from support.remote_effects import _change, _fixture, _publish_pr


def _calls(path, start=0):
    return [json.loads(line) for line in path.read_text().splitlines()[start:]]


@pytest.fixture
def published(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _original, pr = _publish_pr(port, push)
    request = DeliveryRead(push.source_ref, push.target_ref, pr.number, ())
    return port, git, push, pr, state, calls, request


def test_omitting_checks_saves_only_the_seven_check_reads(published, monkeypatch):
    port, _git, _push, pr, _state, calls, request = published
    heads, commands = [], []
    read_heads, run = remote.read_remote_heads, port.git.run

    def observed_heads(*args, **kwargs):
        result = read_heads(*args, **kwargs)
        heads.append(result)
        return result

    def observed_git(*args, **kwargs):
        commands.append(args)
        return run(*args, **kwargs)

    monkeypatch.setattr(remote, 'read_remote_heads', observed_heads)
    monkeypatch.setattr(port.git, 'run', observed_git)
    start = len(_calls(calls))
    complete = port.delivery(request)
    full_calls, full_heads, full_commands = _calls(calls, start), list(heads), list(commands)
    assert complete.checks_requested is True
    assert complete.checks is not None and complete.checks.result == 'none'
    assert complete.pr == pr and complete.pr.state == 'OPEN'
    assert len(full_calls) == 8
    basis = full_calls[0]
    assert basis[:2] == ['api', 'graphql']
    assert [arg.split('=', 1)[1] for arg in basis if arg.startswith(('object0=', 'object1='))] == sorted({pr.head.sha, pr.base.sha})
    assert 'pr=' + str(pr.number) in basis
    query = next(arg for arg in basis if arg.startswith('query='))
    assert all(field in query for field in ('DeliveryBasis', 'viewer { login }', 'pullRequest(number:$pr)', 'tree { oid }'))
    assert full_heads == [(pr.head.sha, pr.base.sha)] * 2

    heads.clear()
    commands.clear()
    start = len(_calls(calls))
    omitted = port.delivery(replace(request, read_checks=False))
    omitted_calls = _calls(calls, start)
    assert omitted == replace(complete, checks=None, checks_requested=False)
    assert heads == full_heads and commands == full_commands
    assert omitted_calls == [basis]
    assert len(full_calls) - len(omitted_calls) == 7
    assert not any('--method' in command for command in full_calls + omitted_calls)


def test_omitted_checks_outage_does_not_hide_the_next_required_failure(published):
    port, _git, _push, pr, state, calls, request = published
    _change(state, rollup_http=403)
    start = len(_calls(calls))
    omitted = port.delivery(replace(request, read_checks=False))
    assert omitted.pr == pr and omitted.source == pr.head and omitted.target == pr.base
    assert omitted.checks is None and omitted.checks_requested is False
    observed = _calls(calls, start)
    assert len(observed) == 1 and any('DeliveryBasis' in arg for arg in observed[0])

    start = len(_calls(calls))
    with pytest.raises(ReadUnavailable) as refused:
        port.delivery(request)
    assert refused.value.code == 'github_read_failed'
    assert any(command[:2] == ['api', 'graphql'] for command in _calls(calls, start))
    assert not any('--method' in command for command in _calls(calls, start))
    _change(state, rollup_http=None)
    recovered = port.delivery(request)
    assert recovered.pr == pr and recovered.checks_requested is True
    assert recovered.checks is not None and recovered.checks.result == 'none'


def test_required_checks_reread_pending_and_failure_at_the_same_head(published):
    port, _git, _push, pr, state, calls, request = published
    _change(state, checks=[{'name': 'quality', 'bucket': 'pass'}])
    first = port.delivery(request)
    assert first.checks is not None and first.checks.result == 'pass'
    assert first.checks_requested is True
    for result in ('pending', 'fail'):
        _change(state, checks=[{'name': 'quality', 'bucket': result}])
        omitted = port.delivery(replace(request, read_checks=False))
        assert omitted.pr == pr and omitted.checks is None and omitted.checks_requested is False
        start = len(_calls(calls))
        current = port.delivery(request)
        assert current.source == first.source == pr.head and current.pr == pr
        assert current.checks_requested is True and current.checks is not None
        assert (current.checks.pr_number, current.checks.head_sha, current.checks.result) == (
            pr.number, pr.head.sha, result)
        assert current.checks.details == (('quality', result),)
        assert current.checks != first.checks
        observed = _calls(calls, start)
        assert any(command[:2] == ['api', 'graphql'] for command in observed)
        assert any('/check-runs?' in command[1] for command in observed if command[0] == 'api')
        assert not any('--method' in command for command in observed)


@pytest.mark.parametrize('changed', ['pr-head', 'remote-ref'])
def test_omitting_checks_still_rejects_pr_or_remote_ref_drift(published, monkeypatch, changed):
    port, git, push, pr, state, calls, request = published
    reads = []
    if changed == 'pr-head':
        document = json.loads(state.read_text())
        document['prs'][0]['head']['sha'] = push.base.sha
        state.write_text(json.dumps(document))
    else:
        later = git.make_commit(push.base.tree, (push.base.sha,), 'Later actual target')
        read_heads = remote.read_remote_heads

        def moved(*args, **kwargs):
            reads.append(True)
            if len(reads) == 2:
                git.run('push', 'origin', later.sha + ':' + push.target_ref)
            return read_heads(*args, **kwargs)

        monkeypatch.setattr(remote, 'read_remote_heads', moved)
    start = len(_calls(calls))
    with pytest.raises(ReadUnavailable) as refused:
        port.delivery(replace(request, read_checks=False))
    assert refused.value.code == 'github_observation_pending'
    if changed == 'remote-ref':
        assert len(reads) == 2
    observed = _calls(calls, start)
    assert observed[0][:2] == ['api', 'graphql'] and 'pr=' + str(pr.number) in observed[0]
    assert any('DeliveryBasis' in arg and 'pullRequest(number:$pr)' in arg for arg in observed[0])
    assert not any('--method' in command or any('CurrentRequiredChecks' in arg for arg in command) for command in observed)


@pytest.mark.parametrize('adapter', ['remote', 'offline'])
@pytest.mark.parametrize('read_checks,check_head', [
    (None, None), (0, None), (1, None), ('false', None), (False, 'a' * 40),
], ids=['none', 'zero', 'one', 'string', 'omitted-exact-head'])
def test_invalid_checks_demand_is_rejected_before_physical_reads(tmp_path, monkeypatch, adapter,
                                                               read_checks, check_head):
    root = tmp_path / 'unopened'
    port = (RemoteEffects(root, 'example/project', runner_identity={'source': 'host'})
            if adapter == 'remote' else OfflinePort(root))

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid checks demand reached physical I/O')

    monkeypatch.setattr(port.git, 'run', forbidden)
    if adapter == 'remote':
        monkeypatch.setattr(remote, 'read_remote_heads', forbidden)
        monkeypatch.setattr(port, '_find_pr', forbidden)
    else:
        monkeypatch.setattr(port, 'pr_by_number', forbidden)
        monkeypatch.setattr(port, 'remote_commit', forbidden)
    request = DeliveryRead('refs/heads/source', 'refs/heads/main', 17, (),
                           check_head=check_head, read_checks=read_checks)
    with pytest.raises(ReadUnavailable) as refused:
        if adapter == 'remote':
            port.delivery(request)
        else:
            port.delivery(request, ('pass',), [])
    assert refused.value.code == 'checks_request_invalid'
    assert not root.exists()


def test_offline_omission_never_advances_the_distinct_head_check_script(tmp_path):
    port = OfflinePort.create(tmp_path / 'offline')
    repository = 'example/project'
    port.objects.bind(repository, {1: 'open'})
    base = port.git.commit('HEAD')
    first = port.git.make_commit(base.tree, (base.sha,), 'First actual source')
    source_ref, target_ref = 'refs/heads/source', 'refs/heads/main'
    assert port.physical.push(source_ref, None, first.sha).returncode == 0
    effect = Effect('checks-pr', 'parent:1', 'pr_write', repository, source_ref, target_ref,
        first.sha, first.sha, first, base, None, 'offline-publisher',
        {'title': 'Original PR', 'body': 'Actual offline PR.', 'prior': None},
        Execution('checks-run', 'checks-owner', 1))
    result = port.execute(effect, WriteAuthority(lambda: lambda: None))
    assert result is not None and result.disposition == 'target'
    assert result.pr is not None and result.pr.state == 'OPEN'
    request = DeliveryRead(source_ref, target_ref, result.pr.number, (base.sha,))
    heads, script = [], ('fail', 'pass')
    omitted = port.delivery(replace(request, read_checks=False), script, heads)
    assert omitted.pr == result.pr and omitted.source == first and omitted.target == base
    assert (base.sha, first.sha) in omitted.ancestry
    assert omitted.checks is None and omitted.checks_requested is False and heads == []

    for name, expected in (('second', 'fail'), ('third', 'pass')):
        source = port.external_remote_append(source_ref, {name + '.txt': name + '\n'}, name)
        before = list(heads)
        omitted = port.delivery(replace(request, read_checks=False), script, heads)
        assert omitted.source == source and omitted.pr is not None and omitted.pr.head == source
        assert omitted.checks is None and omitted.checks_requested is False and heads == before
        required = port.delivery(request, script, heads)
        assert required.checks_requested is True and required.checks is not None
        assert (required.checks.pr_number, required.checks.head_sha, required.checks.result) == (
            result.pr.number, source.sha, expected)
        assert heads == before + [source.sha]
    assert script == ('fail', 'pass')
    assert len(port.writes()) == 1 and port.writes()[0]['operation'] == 'pr_write'
