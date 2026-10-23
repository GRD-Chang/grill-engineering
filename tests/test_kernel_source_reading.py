"""Real fake-gh transport into native Requirements; Kernel owns all decisions."""
import json
import os
from pathlib import Path
import sys

import pytest

from agent_run.adapters.github import GhGitHubReader
from agent_run.errors import GitHubReadError
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.adapters.source import read_requirements


def _reader(root: Path, monkeypatch, *, invalid: bool = False) -> tuple[GhGitHubReader, Path]:
    binary = root / 'bin'
    binary.mkdir()
    responses = root / 'responses.json'
    log = root / 'gh-calls.jsonl'
    def page(nodes, more=False, cursor=None):
        return {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}
    responses.write_text(json.dumps({
        'pull_request': {'number': 17, 'state': 'open', 'title': 'Original PR', 'body': 'Original body\r\n保留原文',
                         'user': {'login': 'actual-publisher'}, 'merged': False, 'merge_commit_sha': None,
                         'head': {'ref': 'source', 'sha': '2' * 40, 'repo': {'full_name': 'outside/fork'}},
                         'base': {'ref': 'main', 'sha': '1' * 40, 'repo': {'full_name': 'example/project'}}},
        'checks': [], 'rulesets': [], 'classic': [],
        'parent': {'number': 1, 'title': 'Original Parent', 'body': 'Original Parent body\r\n保留原文',
                   'subIssues': None if invalid else page([{'number': 9}], True, 'parent-next')},
        'parent-next': {'number': 1, 'title': 'Original Parent', 'body': 'Original Parent body\r\n保留原文',
                        'subIssues': page([{'number': 3}])},
        '9': {'number': 9, 'title': 'Original later ticket', 'body': 'Same original ticket body', 'state': 'OPEN',
              'labels': page([{'name': 'ready-for-agent'}]),
              'blockedBy': page([{'number': 99, 'state': 'OPEN'}], True, 'blocker-next')},
        'blocker-next': {'number': 9, 'title': 'Original later ticket', 'body': 'Same original ticket body', 'state': 'OPEN',
              'labels': page([{'name': 'ready-for-agent'}]), 'blockedBy': page([{'number': 3, 'state': 'CLOSED'}])},
        '3': {'number': 3, 'title': 'Original first ticket', 'body': 'Original body', 'state': 'CLOSED',
              'labels': page([{'name': 'ready-for-agent'}, {'name': 'user-label'}]), 'blockedBy': page([])},
    }))
    script = binary / 'gh'
    script.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
args=sys.argv[1:]
with open(os.environ['SOURCE_TEST_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[:2]==['repo','view']:
    print(json.dumps({'nameWithOwner':'example/project','defaultBranchRef':{'name':'main'}}))
elif args[:2]==['api','graphql']:
    fields=dict(value.split('=',1) for index,value in enumerate(args) if index and args[index-1] in ('-F','-f'))
    key=fields.get('cursor') or ('parent' if fields['number']=='1' else fields['number'])
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: issue=json.load(f)[key]
    print(json.dumps({'data':{'repository':{'issue':issue}}}))
elif args[:1]==['api'] and args[1]=='repos/example/project/commits/main':
    print(json.dumps({'sha':'1111111111111111111111111111111111111111'}))
elif args[:2]==['api','repos/example/project/pulls/17']:
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    pr=data['pull_request']
    with open(os.environ['SOURCE_TEST_LOG']) as f: count=sum(json.loads(line)[:2]==args[:2] for line in f)
    if count>1 and 'pr_head_after' in data: pr['head']['sha']=data['pr_head_after']
    print(json.dumps(pr))
elif args[:1]==['api'] and args[1].startswith('repos/example/project/actions/jobs/'):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    if args[1].endswith('/logs'):
        if data.get('logs_http'):
            print('HTTP '+str(data['logs_http']),file=sys.stderr)
            raise SystemExit(1)
        print(data['job_logs'],end='')
    else:
        print(json.dumps(data['job']))
elif args[:2]==['pr','checks']:
    assert '--required' in args
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps(json.load(f)['checks']))
elif args[:2]==['api','repos/example/project/rulesets']:
    assert '--paginate' in args and '--slurp' in args
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps([json.load(f)['rulesets']]))
elif args[:1]==['api'] and args[1].startswith('repos/example/project/rulesets/'):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps(json.load(f)['ruleset_detail']))
elif args[:2]==['api','repos/example/project/branches/main/protection/required_status_checks/contexts']:
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    if data.get('classic_http'):
        print('HTTP '+str(data['classic_http']),file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(data['classic']))
else:
    raise SystemExit('Unexpected external command: '+repr(args))
''')
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SOURCE_TEST_RESPONSES', str(responses))
    monkeypatch.setenv('SOURCE_TEST_LOG', str(log))
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    return GhGitHubReader('example/project', working_directory=root, runner_identity={'source': 'host'}), log


def test_paginated_source_preserves_native_order_text_and_external_blockers(tmp_path: Path, monkeypatch) -> None:
    reader, log = _reader(tmp_path, monkeypatch)
    result = read_requirements(reader, 1)
    assert result.parent.sub_issue_numbers == (9, 3) and result.parent.sub_issue_order_reliable is True
    assert result.parent.body == 'Original Parent body\r\n保留原文'
    assert tuple(issue.number for issue in result.issues) == (9, 3)
    assert [(blocker.number, blocker.state) for blocker in result.issues[0].blocked_by] == [(99, 'OPEN'), (3, 'CLOSED')]
    assert result.issues[1].labels == frozenset({'ready-for-agent', 'user-label'})
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(args[0] in {'api', 'repo'} for args in calls)
    assert any('cursor=parent-next' in args for args in calls)
    assert any('cursor=blocker-next' in args for args in calls)


def test_public_graph_returns_the_canonical_requirement_values(tmp_path: Path, monkeypatch) -> None:
    from agent_run.model import Blocker, Issue, ParentIssue
    reader, _log = _reader(tmp_path, monkeypatch)
    graph = reader.delivery_graph(1)
    assert isinstance(graph.parent, ParentIssue)
    assert all(isinstance(issue, Issue) for issue in graph.issues.values())
    assert all(isinstance(blocker, Blocker) for issue in graph.issues.values() for blocker in issue.blocked_by)


def test_closed_local_dependency_is_still_decided_by_native_completion_not_reader(tmp_path: Path, monkeypatch) -> None:
    reader, _log = _reader(tmp_path, monkeypatch)
    source = read_requirements(reader, 1)
    base = Commit('base', 'tree')
    jobs = tuple(Delivery(f'ticket:{number}', DeliveryKind.TICKET, number, 'untrusted', base,
        f'refs/heads/ticket-{number}', 'refs/heads/run') for number in source.parent.sub_issue_numbers)
    final = Delivery('final', DeliveryKind.FINAL_RUN, 1, 'untrusted', base, 'refs/heads/run', 'refs/heads/main')
    kernel = Kernel.create(tmp_path / 'kernel.sqlite3', RunSpec('run', 'example/project', 1,
        'untrusted-parent', 'untrusted-graph', (*jobs, final)), requirements=source)
    observed = Kernel(kernel.storage.path).source_status()
    assert observed['frontier'] == []
    assert observed['remaining'] == {'ticket:9': 'blocked_by_incomplete_tickets', 'ticket:3': 'ticket_closed'}
    assert observed['final_subject'] is None
    assert observed['parent_revision'] != 'untrusted-parent' and observed['graph_revision'] != 'untrusted-graph'


def test_invalid_native_graph_is_not_replaced_by_empty_requirements(tmp_path: Path, monkeypatch) -> None:
    reader, _log = _reader(tmp_path, monkeypatch, invalid=True)
    with pytest.raises(GitHubReadError, match='subIssues'):
        read_requirements(reader, 1)
    assert not (tmp_path / 'kernel.sqlite3').exists()


def test_remote_heads_are_independent_of_local_refs_and_missing_is_not_read_failure(tmp_path: Path) -> None:
    from agent_run.adapters.git_objects import LocalGit
    from agent_run.adapters.source import read_remote_heads
    remote = tmp_path / 'origin.git'
    root = tmp_path / 'checkout'
    root.mkdir()
    git = LocalGit(root)
    git.run('init', '--bare', str(remote))
    git.run('init', '-b', 'main')
    git.run('config', '--local', 'user.name', 'Source fixture author')
    git.run('config', '--local', 'user.email', 'source-fixture@example.invalid')
    git.run('config', 'gc.auto', '0')
    (root / 'initial.txt').write_text('Original input\n')
    git.run('add', 'initial.txt')
    git.run('commit', '-m', 'Initial')
    base = git.commit('HEAD')
    git.run('remote', 'add', 'origin', str(remote))
    git.run('update-ref', 'refs/heads/source', base.sha)
    git.run('push', 'origin', 'refs/heads/main', 'refs/heads/source')
    (root / 'remote-only.txt').write_text('Remote append\n')
    git.run('add', 'remote-only.txt')
    git.run('commit', '-m', 'Append remote')
    later = git.commit('HEAD')
    git.run('push', 'origin', 'HEAD:refs/heads/source')
    assert git.commit('refs/heads/source') == base
    before = git.run('show-ref').stdout
    observed = read_remote_heads(root, 'refs/heads/source', 'refs/heads/main', runner_identity={'source': 'host'})
    assert observed == (later.sha, base.sha)
    assert git.run('show-ref').stdout == before
    absent, target = read_remote_heads(root, 'refs/heads/not-created', 'refs/heads/main', runner_identity={'source': 'host'})
    assert absent is None and target == base.sha
    assert read_remote_heads(root, 'refs/heads/source', 'refs/heads/not-created',
                             runner_identity={'source': 'host'}) == (later.sha, None)
    with pytest.raises(GitHubReadError) as failure:
        read_remote_heads(root, 'refs/heads/source', 'refs/heads/main',
                          runner_identity={'source': 'host'}, remote=str(tmp_path / 'missing-remote.git'))
    assert failure.value.code == 'github_read_failed'
    assert git.run('show-ref').stdout == before


def test_pr_observation_preserves_foreign_head_repository_and_original_actor_text(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_pull_request_facts
    reader, _log = _reader(tmp_path, monkeypatch)
    observed = read_pull_request_facts(reader, 17)
    assert observed['head']['repo']['full_name'] == 'outside/fork'
    assert observed['base']['repo']['full_name'] == 'example/project'
    assert observed['user']['login'] == 'actual-publisher'
    assert observed['body'] == 'Original body\r\n保留原文' and observed['title'] == 'Original PR'
    assert observed['head']['sha'] == '2' * 40 and observed['base']['sha'] == '1' * 40


@pytest.mark.parametrize('configured', [False, True], ids=['no-configured-checks', 'unreported-required-context'])
def test_checks_observation_retains_configuration_and_never_invents_none_or_pass(tmp_path: Path, monkeypatch, configured: bool) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    if configured:
        data['rulesets'] = [{'id': 42, 'target': 'branch', 'enforcement': 'active'}]
        data['ruleset_detail'] = {'id': 42, 'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
            'rules': [{'type': 'required_status_checks', 'parameters': {
                'required_status_checks': [{'context': 'not-yet-reported', 'integration_id': None}]}}]}
        data['classic'] = ['classic-ci']
    responses.write_text(json.dumps(data))
    observed = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert observed['required_checks'] == []
    normalized = normalize_required_checks(observed)
    assert normalized.result == ('pending' if configured else 'none')
    assert normalized.pr_number == 17 and normalized.head_sha == '2' * 40
    if configured:
        assert dict(normalized.details) == {'classic-ci': 'pending', 'not-yet-reported': 'pending'}
    assert json.loads(normalized.logs)['job_logs'] == 'not_collected'
    assert observed['pr_before']['head']['sha'] == observed['pr_after']['head']['sha'] == '2' * 40
    if configured:
        assert observed['rulesets'][0]['detail']['rules'][0]['parameters']['required_status_checks'][0]['context'] == 'not-yet-reported'
        assert observed['classic_contexts'] == ['classic-ci']
    else:
        assert observed['rulesets'] == [] and observed['classic_contexts'] == []
    if configured:
        # A configured context with no report differs from a report whose
        # server state is not recognized. Preserve both Original facts.
        data['checks'] = [{'name': 'classic-ci', 'bucket': 'new-server-state'},
                          {'name': 'not-yet-reported', 'bucket': 'pass'}]
        responses.write_text(json.dumps(data))
        uncertain = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
        assert uncertain.result == 'unknown'
        assert dict(uncertain.details) == {'classic-ci': 'unknown', 'not-yet-reported': 'pass'}
        assert json.loads(uncertain.logs)['observation']['required_checks'] == data['checks']
        data['checks'].pop()
        responses.write_text(json.dumps(data))
        unfinished = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
        assert unfinished.result == 'pending'  # Fixed old consumer prioritizes pending over unknown.
        assert dict(unfinished.details) == {'classic-ci': 'unknown', 'not-yet-reported': 'pending'}


def test_check_head_race_and_malformed_check_output_are_errors_not_empty_success(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_required_check_facts
    reader, log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    data['pr_head_after'] = '3' * 40
    responses.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as raced:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert raced.value.code == 'change_pr_head_drift'
    data.pop('pr_head_after')
    data['checks'] = {'not': 'a check array'}
    responses.write_text(json.dumps(data))
    log.unlink()
    with pytest.raises(GitHubReadError) as invalid:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert invalid.value.code == 'github_invalid_response'


@pytest.mark.parametrize('bucket,result', [('pass', 'pass'), ('pending', 'pending'), ('fail', 'fail'), ('new-server-state', 'unknown')])
def test_check_normalizer_uses_original_required_status_not_cli_exit_or_incidental_checks(tmp_path: Path, monkeypatch, bucket: str, result: str) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    data['classic'] = ['actual-required']
    data['checks'] = [{'name': 'actual-required', 'bucket': bucket, 'state': 'original-state', 'description': '原始诊断'},
                      {'name': 'unrelated-check', 'bucket': 'fail', 'link': 'https://github.com/example/project/actions/runs/66/job/999'}]
    responses.write_text(json.dumps(data))
    normalized = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
    assert normalized.result == result
    assert normalized.details == (('actual-required', result),)
    original = json.loads(normalized.logs)['observation']
    assert original['required_checks'][0]['description'] == '原始诊断'
    assert original['required_checks'][1]['name'] == 'unrelated-check'


@pytest.mark.parametrize('http_status', [404, 403], ids=['classic-absent-does-not-erase-ruleset', 'permission-error-is-not-absence'])
def test_classic_protection_absence_and_permission_failure_have_distinct_meanings(tmp_path: Path, monkeypatch, http_status: int) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    data['classic_http'] = http_status
    data['rulesets'] = [{'id': 42, 'target': 'branch', 'enforcement': 'active'}]
    data['ruleset_detail'] = {'id': 42, 'rules': [{'type': 'required_status_checks', 'parameters': {
        'required_status_checks': [{'context': 'still-required', 'integration_id': None}]}}]}
    responses.write_text(json.dumps(data))
    if http_status == 403:
        with pytest.raises(GitHubReadError) as failure:
            read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
        assert failure.value.code == 'github_read_failed'
    else:
        observed = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
        assert observed['classic_protection_absent'] is True and observed['classic_contexts'] is None
        normalized = normalize_required_checks(observed)
        assert normalized.result == 'pending' and normalized.details == (('still-required', 'pending'),)
        data['checks'] = [{'name': 'still-required', 'bucket': 'new-server-state'}]
        responses.write_text(json.dumps(data))
        uncertain = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
        assert uncertain.result == 'unknown' and uncertain.details == (('still-required', 'unknown'),)
        assert json.loads(uncertain.logs)['observation']['classic_protection_absent'] is True


def _failed_job(root: Path, *, logs_http: int = 0) -> None:
    responses = root / 'responses.json'
    data = json.loads(responses.read_text())
    data['classic'] = ['quality']
    data['checks'] = [{'name': 'quality', 'bucket': 'fail', 'state': 'FAILURE', 'workflow': 'CI',
                       'description': 'Original failure', 'link': 'https://github.com/example/project/actions/runs/22/job/33'}]
    data['job'] = {'id': 33, 'head_sha': '2' * 40, 'name': 'quality', 'workflow_name': 'CI',
                   'status': 'completed', 'conclusion': 'failure',
                   'steps': [{'name': 'Original step', 'number': 6, 'status': 'completed', 'conclusion': 'failure'}]}
    data['job_logs'] = 'Original command output\r\n原始失败诊断\n'
    data['logs_http'] = logs_http
    responses.write_text(json.dumps(data))


def test_failed_check_keeps_exact_job_workflow_step_and_original_logs_without_repairability(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, log = _reader(tmp_path, monkeypatch)
    _failed_job(tmp_path)
    policy = tmp_path / 'pyproject.toml'
    policy.write_text('[tool.agent-run.required-checks]\ncode-failure-steps = ["other::irrelevant"]\n')
    unchanged = policy.read_bytes()
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = facts['required_checks'][0]
    assert check['job']['id'] == 33 and check['job']['head_sha'] == '2' * 40
    assert check['job']['workflow_name'] == 'CI'
    assert check['job']['steps'] == [{'name': 'Original step', 'number': 6, 'status': 'completed', 'conclusion': 'failure'}]
    assert check['logs'] == 'Original command output\r\n原始失败诊断\n'
    assert check['logs_truncated'] is False and 'repairability' not in check
    normalized = normalize_required_checks(facts)
    assert normalized.result == 'fail'
    assert json.loads(normalized.logs)['observation']['required_checks'][0]['logs'] == check['logs']
    assert policy.read_bytes() == unchanged
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(args[0] in {'api', 'repo', 'pr'} and '--method' not in args for args in calls)


def test_unavailable_job_logs_do_not_erase_an_exact_failure_or_become_empty_logs(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _log = _reader(tmp_path, monkeypatch)
    _failed_job(tmp_path, logs_http=403)
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = facts['required_checks'][0]
    assert check['job']['head_sha'] == '2' * 40 and 'logs' not in check
    assert 'HTTP 403' in check['logs_unavailable']
    assert normalize_required_checks(facts).result == 'fail'


@pytest.mark.parametrize('damage', ['head', 'job-id', 'not-completed'])
def test_failed_job_evidence_must_match_the_actual_requested_job_and_head(tmp_path: Path, monkeypatch, damage: str) -> None:
    from agent_run.adapters.source import read_required_check_facts
    reader, _log = _reader(tmp_path, monkeypatch)
    _failed_job(tmp_path)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    if damage == 'head':
        data['job']['head_sha'] = '3' * 40
    elif damage == 'job-id':
        data['job']['id'] = 34
    else:
        data['job']['status'] = 'in_progress'
    responses.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as failure:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert failure.value.code == ('github_invalid_response' if damage == 'job-id' else 'github_check_evidence_observation_pending')


def test_non_actions_failure_preserves_the_original_provider_evidence_without_classification(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    data['checks'] = [{'name': 'external-ci', 'bucket': 'fail', 'link': 'https://external.example/check/44',
                      'description': 'Original provider failure'}]
    responses.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = facts['required_checks'][0]
    assert check['description'] == 'Original provider failure' and check['link'] == 'https://external.example/check/44'
    assert 'job' not in check and 'logs' not in check and 'repairability' not in check
    assert normalize_required_checks(facts).result == 'fail'
    assert not any('/actions/jobs/' in ' '.join(json.loads(line)) for line in log.read_text().splitlines())
