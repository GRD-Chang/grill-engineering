"""Managed Parent/Ticket links use known identities and real private Git refs."""
import json
import subprocess

import pytest

from agent_run.adapters.github import GhGitHubReader
from agent_run.adapters.remote import RemoteEffects
from agent_run.adapters.source import read_requirements
from agent_run.core.operation_runner import OperationRunner
from support.offline import GraphScenario
from test_kernel_managed_entry_end_to_end import _CODEX, _control_environment, _git
from test_kernel_operation_runner import Ports, settle_agent


@pytest.fixture
def managed_link(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    binary = tmp_path / 'control-bin' / 'gh'
    remote = tmp_path / 'origin.git'
    head = _git(git_repo, 'rev-parse', 'HEAD')
    _git(git_repo, 'push', str(remote), head + ':refs/heads/link-test')
    def command(*arguments):
        return subprocess.run([str(binary), *arguments], env=environment, text=True,
                              capture_output=True, timeout=5)
    return command, remote, state, head


@pytest.mark.parametrize('number,corruption', [(1, None), (2, None), (1, 'id'), (2, 'id'),
                                               (1, 'unknown-id'), (1, 'ref'), (2, 'oid')])
def test_managed_link_accepts_only_known_issue_and_exact_ref_oid(
    managed_link, tmp_path, number, corruption,
):
    command, remote, state, head = managed_link
    issue = command('api', f'repos/example/project/issues/{number}')
    assert issue.returncode == 0
    resource = json.loads(issue.stdout)
    assert resource['number'] == number
    variables = {'issueId': resource['node_id'], 'name': 'link-test', 'oid': head}
    if corruption == 'id': variables['issueId'] += '-foreign'
    if corruption == 'unknown-id': variables['issueId'] = 'issue-node-999'
    if corruption == 'ref': variables['name'] = 'missing-ref'
    if corruption == 'oid': variables['oid'] = '0' * len(head)
    payload = tmp_path / 'link-request.json'
    query = ('mutation($issueId: ID!, $name: String!, $oid: GitObjectID!) '
             '{ createLinkedBranch(input: {issueId: $issueId, name: $name, oid: $oid}) '
             '{ linkedBranch { ref { name target { oid } } } } }')
    payload.write_text(json.dumps({'query': query, 'variables': variables}))
    refs = _git(remote, 'for-each-ref')
    response = command('api', 'graphql', '--method', 'POST', '--input', str(payload))
    links = json.loads(state.read_text()).get('links', [])
    if corruption is None:
        assert response.returncode == 0 and response.stderr == ''
        linked = json.loads(response.stdout)['data']['createLinkedBranch']['linkedBranch']['ref']
        assert linked == {'name': 'link-test', 'target': {'oid': head}}
        assert links == [{'ref': 'link-test', 'sha': head}]
    else:
        assert response.returncode != 0 and 'AssertionError' in response.stderr
        assert links == []
    assert _git(remote, 'for-each-ref') == refs
    calls = [json.loads(line) for line in (tmp_path / 'gh-calls.jsonl').read_text().splitlines()]
    assert sum(args[1] == 'graphql' and 'POST' in args for args in calls) == 1


@pytest.mark.parametrize('lose_final_response', [False, True], ids=['acknowledged', 'response-lost'])
def test_normal_core_final_link_is_unknown_only_after_injected_response_loss(
    managed_link, git_repo, tmp_path, monkeypatch, lose_final_response,
):
    _, remote, state, _ = managed_link
    document = json.loads(state.read_text())
    document['actor'] = 'offline-publisher'
    state.write_text(json.dumps(document))
    reader = GhGitHubReader('example/project', working_directory=git_repo, runner_identity={'source': 'host'})
    scenario = GraphScenario(tmp_path / 'normal-producer', read_requirements(reader, 1))
    ports = Ports(scenario)
    original = ports.execute
    selected = RemoteEffects(scenario.port.git.root, scenario.spec.repository,
        runner_identity=scenario.spec.inputs.runner_identity, remote=scenario.spec.inputs.repository_remote)
    observed = []
    def execute(effect, authority):
        if effect.operation != 'issue_link':
            return original(effect, authority)
        scenario.port.git.run('push', str(remote), effect.before_sha + ':' + effect.source_ref)
        if effect.subject == 'final:1' and lose_final_response:
            document = json.loads(state.read_text())
            document['lose_operation'] = 'issue_link'
            state.write_text(json.dumps(document))
        result = selected.execute(effect, authority)
        observed.append((effect, result))
        return result
    monkeypatch.setattr(ports, 'execute', execute)
    runner = OperationRunner(scenario.kernel, scenario.execution, lambda binding: ports)
    for _ in range(120):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(scenario, runner, step.call)
        assert step.kind not in {'paused', 'waiting'}
        if observed and observed[-1][0].subject == 'final:1':
            break
    else:
        raise AssertionError('Normal producer did not reach the Final link')
    assert [(effect.subject, effect.payload['issue']) for effect, _ in observed] == [('ticket:2', 2), ('final:1', 1)]
    assert observed[0][1].disposition == 'target'
    effect, result = observed[-1]
    assert result.disposition == ('unknown' if lose_final_response else 'target')
    if lose_final_response:
        assert result.raw['write_returncode'] == 1
        assert 'response lost after actual mutation' in result.raw['diagnostic']
    final = scenario.kernel.status('final:1')
    assert final['issue_link']['state'] == ('indeterminate' if lose_final_response else 'linked')
    assert final['phase'] == 'checks' and not final['gates']
    runner.run_once()
    assert scenario.kernel.status('final:1')['phase'] == 'approval'
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))
        assert tuple(row) == ('sent' if lose_final_response else 'confirmed', 1)
    calls = [json.loads(line) for line in (tmp_path / 'gh-calls.jsonl').read_text().splitlines()]
    assert sum(args[1] == 'graphql' and 'POST' in args for args in calls) == 2
    assert len(json.loads(state.read_text())['links']) == 2
