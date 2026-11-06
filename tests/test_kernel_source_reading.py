"""Real fake-gh transport into native Requirements; Kernel owns all decisions."""

from support.repository_fixture import create_kernel
import json
import os
from pathlib import Path

import pytest

from agent_run.errors import GitHubReadError
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.adapters.source import read_requirements
from support.remote_effects import _reader


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


def test_reader_returns_the_canonical_requirement_values(tmp_path: Path, monkeypatch) -> None:
    from agent_run.model import Blocker, Issue, ParentIssue
    reader, _log = _reader(tmp_path, monkeypatch)
    source = reader.requirements(1)
    assert isinstance(source.parent, ParentIssue)
    assert all(isinstance(issue, Issue) for issue in source.issues)
    assert all(isinstance(blocker, Blocker) for issue in source.issues for blocker in issue.blocked_by)


def test_closed_local_dependency_is_still_decided_by_native_completion_not_reader(tmp_path: Path, monkeypatch) -> None:
    reader, _log = _reader(tmp_path, monkeypatch)
    source = read_requirements(reader, 1)
    base = Commit('base', 'tree')
    jobs = tuple(Delivery(f'ticket:{number}', DeliveryKind.TICKET, number, 'untrusted', base,
        f'refs/heads/ticket-{number}', 'refs/heads/run') for number in source.parent.sub_issue_numbers)
    final = Delivery('final', DeliveryKind.FINAL_RUN, 1, 'untrusted', base, 'refs/heads/run', 'refs/heads/main')
    kernel = create_kernel(tmp_path / 'kernel.sqlite3', RunSpec('run', 'example/project', 1,
        'untrusted-parent', 'untrusted-graph', (*jobs, final)), requirements=source)
    observed = Kernel(kernel.storage.path, run_id=kernel.storage.run_id).source_status()
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
        recorded = json.loads(uncertain.logs)['observation']['required_checks']
        assert [item['name'] for item in recorded] == ['classic-ci', 'not-yet-reported']
        assert [item['original']['conclusion'] for item in recorded] == ['new-server-state', 'SUCCESS']
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
    from agent_run.adapters import github_commands
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, log = _reader(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(github_commands.time, 'sleep', delays.append)
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
    observations = 2 if http_status == 404 else 1
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(call[:2] == ['api', 'repos/example/project/branches/main/protection/required_status_checks']
               for call in calls) == 3 * observations
    assert len(delays) == 2 * observations
    assert all(1 <= delay <= 1.25 for delay in delays[::2])
    assert all(2 <= delay <= 2.25 for delay in delays[1::2])


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


def test_large_job_logs_keep_the_original_diagnostic_tail(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.github_commands import MAX_COMMAND_OUTPUT_BYTES
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _log = _reader(tmp_path, monkeypatch)
    _failed_job(tmp_path)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['job_logs'] = 'discarded-prefix\n' + 'L' * (MAX_COMMAND_OUTPUT_BYTES + 1024) + '\nOriginal end'
    path.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = facts['required_checks'][0]
    assert check['logs'] == 'L' * 65536 and check['logs_truncated'] is True
    assert 'logs_unavailable' not in check and normalize_required_checks(facts).result == 'fail'


def test_unavailable_job_logs_do_not_erase_an_exact_failure_or_become_empty_logs(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters import github_commands
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, log = _reader(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(github_commands.time, 'sleep', delays.append)
    _failed_job(tmp_path, logs_http=403)
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = facts['required_checks'][0]
    assert check['job']['head_sha'] == '2' * 40 and 'logs' not in check
    assert 'HTTP 403' in check['logs_unavailable']
    assert normalize_required_checks(facts).result == 'fail'
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(call[:2] == ['api', 'repos/example/project/actions/jobs/33/logs'] for call in calls) == 3
    assert len(delays) == 2 and 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25


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


def test_app_bound_required_check_uses_original_app_identity(tmp_path: Path, monkeypatch) -> None:
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['checks'] = [{'name': 'quality', 'bucket': 'pass'}]
    data['rulesets'] = [{'id': 42, 'target': 'branch', 'enforcement': 'active'}]
    data['ruleset_detail'] = {'id': 42, 'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH']}},
        'rules': [{'type': 'required_status_checks', 'parameters': {
            'required_status_checks': [{'context': 'quality', 'integration_id': 1001}]}}]}
    path.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    check = normalize_required_checks(facts)
    assert check.result == 'pass' and check.details == (('quality', 'pass'),)
    assert facts['required_checks'][0]['app_id'] == 1001
    assert facts['required_checks'][0]['head_sha'] == '2' * 40


def _current_run(*, app=1001, state='SUCCESS', head='2' * 40, identity='current-check', suite='current-suite'):
    return {'__typename': 'CheckRun', 'id': identity, 'databaseId': 999 if identity == 'old-higher-999' else 2 if identity in {'other-app', 'wrong-app-high-999'} else 1, 'name': 'quality', 'isRequired': True,
            'status': 'QUEUED' if state == 'QUEUED' else 'COMPLETED', 'conclusion': state,
            'detailsUrl': 'https://example.invalid/check', 'title': 'Original check evidence',
            'checkSuite': {'id': suite, 'databaseId': 102 if app == 1002 or suite == 'new-suite' else 101, 'commit': {'oid': head},
                           'app': None if app is None else {'databaseId': app}, 'workflowRun': None}}


def _rollup_page(nodes, *, more=False, cursor=None):
    return {'data': {'repository': {'pullRequest': {'number': 17, 'headRefOid': '2' * 40,
        'commits': {'nodes': [{'commit': {'oid': '2' * 40, 'statusCheckRollup': {'contexts': {
            'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}]}}}}}


def _bound_reader(tmp_path, monkeypatch, pages, *, classic=False):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['rollup_pages'] = pages
    if classic:
        data['classic'] = ['quality']
        data['classic_checks'] = [{'context': 'quality', 'app_id': 1001}]
    else:
        data['rulesets'] = [{'id': 42, 'target': 'branch', 'enforcement': 'active'}]
        data['ruleset_detail'] = {'id': 42, 'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH']}},
            'rules': [{'type': 'required_status_checks', 'parameters': {
                'required_status_checks': [{'context': 'quality', 'integration_id': 1001}]}}]}
    path.write_text(json.dumps(data))
    return reader, log, path


@pytest.mark.parametrize('case,expected', [
    ('matching', 'pass'), ('wrong-app', 'pending'), ('status-context', 'pending'),
    ('current-pending', 'pending'), ('current-failed', 'fail'), ('not-required', 'pending'),
    ('same-name-apps', 'pass'), ('ambiguous-suites', 'unknown'), ('old-pass-new-pending', 'pending'),
    ('old-pass-new-fail', 'fail'), ('unknown-conclusion', 'unknown'), ('no-report', 'pending'),
])
def test_bound_check_requires_current_matching_check_run(tmp_path, monkeypatch, case, expected):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    nodes = [_current_run()]
    if case == 'wrong-app': nodes = [_current_run(app=1002)]
    elif case == 'status-context': nodes = [{'__typename': 'StatusContext', 'id': 'status',
        'context': 'quality', 'isRequired': True, 'state': 'SUCCESS', 'targetUrl': None}]
    elif case == 'current-pending': nodes = [_current_run(state='QUEUED')]
    elif case == 'current-failed': nodes = [_current_run(state='FAILURE')]
    elif case == 'not-required': nodes[0]['isRequired'] = False
    elif case == 'same-name-apps': nodes += [_current_run(app=1002, state='FAILURE', identity='other-app')]
    elif case in {'ambiguous-suites', 'old-pass-new-pending', 'old-pass-new-fail'}:
        nodes = [_current_run(identity='old-higher-999', suite='old-suite'),
                 _current_run(identity='new-lower-1', suite='new-suite', state={
                     'ambiguous-suites': 'SUCCESS', 'old-pass-new-pending': 'QUEUED',
                     'old-pass-new-fail': 'FAILURE'}[case])]
    elif case == 'unknown-conclusion': nodes = [_current_run(state='FUTURE_CONCLUSION')]
    elif case == 'no-report': nodes = []
    reader, log, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page(nodes)})
    if case == 'ambiguous-suites':
        with pytest.raises(GitHubReadError, match='ambiguous'):
            read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
        return
    if case in {'old-pass-new-pending', 'old-pass-new-fail'}:
        data = json.loads(path.read_text())
        data['latest_nodes'] = [nodes[-1]]
        path.write_text(json.dumps(data))
    result = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
    assert result.result == expected
    assert result.pr_number == 17 and result.head_sha == '2' * 40
    assert not any(json.loads(line)[:2] == ['pr', 'checks'] for line in log.read_text().splitlines())


@pytest.mark.parametrize('damage', ['missing-app', 'wrong-head', 'missing-suite', 'duplicate-member',
                                   'missing-required', 'partial-errors', '403', 'page-errors', 'page-403', 'cursor-loop'])
def test_incomplete_required_check_identity_or_pages_never_prove_success(tmp_path, monkeypatch, damage):
    from agent_run.adapters.source import read_required_check_facts
    node = _current_run()
    pages = {'': _rollup_page([node])}
    if damage == 'missing-app': node['checkSuite']['app'] = None
    elif damage == 'wrong-head': node['checkSuite']['commit']['oid'] = '3' * 40
    elif damage == 'missing-suite': node['checkSuite']['id'] = None
    elif damage == 'duplicate-member': pages[''] = _rollup_page([node, node])
    elif damage == 'missing-required': node.pop('isRequired')
    elif damage == 'partial-errors': pages['']['errors'] = [{'message': 'Resource not accessible'}]
    elif damage == '403': pages[''] = {'http': 403}
    else:
        pages[''] = _rollup_page([], more=True, cursor='next')
        pages['next'] = ({'errors': [{'message': 'Partial response'}], 'data': {}} if damage == 'page-errors'
                         else {'http': 403} if damage == 'page-403'
                         else _rollup_page([], more=True, cursor='next'))
    reader, _, _ = _bound_reader(tmp_path, monkeypatch, pages)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _: None)
    with pytest.raises(GitHubReadError) as failure:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    if damage == 'partial-errors':
        assert 'Resource not accessible' in failure.value.message
    elif damage == 'page-errors':
        assert 'Partial response' in failure.value.message


@pytest.mark.parametrize('classic', [False, True])
def test_bound_check_pagination_keeps_app_identity_and_original_member(tmp_path, monkeypatch, classic):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    current = _current_run(identity='new-lower-1')
    reader, log, _ = _bound_reader(tmp_path, monkeypatch, {
        '': _rollup_page([_current_run(app=1002, identity='wrong-app-high-999')], more=True, cursor='next'),
        'next': _rollup_page([current])}, classic=classic)
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    result = normalize_required_checks(facts)
    assert result.result == 'pass' and result.details == (('quality', 'pass'),)
    assert facts['required_checks'][1]['original'] == current
    assert 'cursor=next' in log.read_text()


def test_replaced_suite_is_not_reintroduced_from_historical_results(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run(state='QUEUED')])})
    data = json.loads(path.read_text())
    data['checks'] = [{'name': 'quality', 'bucket': 'pass', 'completedAt': '2099-01-01'}]
    path.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert normalize_required_checks(facts).result == 'pending'
    assert [check['original']['id'] for check in facts['required_checks']] == ['current-check']


@pytest.mark.parametrize('check_state,expected', [(None, 'pass'), ('FAILURE', 'fail'), ('QUEUED', 'pending')])
def test_unbound_context_keeps_status_and_check_run_as_distinct_required_sources(tmp_path, monkeypatch, check_state, expected):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    status = {'__typename': 'StatusContext', 'id': 'status-current', 'context': 'quality',
              'isRequired': True, 'state': 'SUCCESS', 'targetUrl': None}
    nodes = [status] + ([] if check_state is None else [_current_run(state=check_state)])
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page(nodes)}, classic=True)
    data = json.loads(path.read_text())
    data['classic_checks'][0]['app_id'] = -1
    path.write_text(json.dumps(data))
    result = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
    assert result.result == expected
    assert result.details[0] == ('quality', 'pass')


def test_same_head_and_result_do_not_erase_changed_required_app_evidence(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    before = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
    data = json.loads(path.read_text())
    data['ruleset_detail']['rules'][0]['parameters']['required_status_checks'][0]['integration_id'] = 1002
    data['rollup_pages'][''] = _rollup_page([_current_run(app=1002)])
    path.write_text(json.dumps(data))
    after = normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40))
    assert before.result == after.result == 'pass'
    assert before.details == after.details and before.head_sha == after.head_sha
    assert before.logs != after.logs and before != after


@pytest.mark.parametrize('old,new,expected', [('FAILURE', 'SUCCESS', 'pass'), ('SUCCESS', 'SUCCESS', 'pass'),
                                            ('SUCCESS', 'FAILURE', 'fail'), ('SUCCESS', 'QUEUED', 'pending')])
def test_server_latest_replaces_old_rollup_run_without_client_ordering(tmp_path, monkeypatch, old, new, expected):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    older = _current_run(identity='old-higher-999', state=old)
    newer = _current_run(identity='new-lower-1', state=new)
    older['databaseId'], newer['databaseId'] = 999, 1
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([older, newer])})
    data = json.loads(path.read_text())
    data['latest_nodes'] = [newer]  # The server's filter=latest response, not a client sort.
    path.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert normalize_required_checks(facts).result == expected
    assert [row['original']['id'] for row in facts['required_checks']] == ['new-lower-1']


def test_missing_classic_app_field_cannot_become_any_source(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run(app=1002)])}, classic=True)
    data = json.loads(path.read_text())
    del data['classic_checks'][0]['app_id']
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)


@pytest.mark.parametrize('count', [1001, None, -1])
def test_latest_ref_scope_must_be_provably_within_service_suite_bound(tmp_path, monkeypatch, count):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    data = json.loads(path.read_text()); data['suite_count'] = count
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as failure:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert ('1000 suites' if count == 1001 else 'count') in failure.value.message


@pytest.mark.parametrize('damage', ['missing-page', 'wrong-app', 'wrong-head', 'wrong-suite', 'missing-app',
                                   'unobserved-run', 'changed-count', 'missing-run', '403'])
def test_latest_endpoint_cannot_supply_incomplete_or_foreign_evidence(tmp_path, monkeypatch, damage):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    run = {'id': 1, 'name': 'quality', 'app': {'id': 1001}, 'head_sha': '2' * 40,
           'check_suite': {'id': 101}, 'status': 'completed', 'conclusion': 'success'}
    pages = [{'total_count': 1, 'check_runs': [run]}]
    if damage == 'missing-page': pages[0]['total_count'] = 2
    elif damage == 'wrong-app': run['app']['id'] = 1002
    elif damage == 'wrong-head': run['head_sha'] = '3' * 40
    elif damage == 'wrong-suite': run['check_suite']['id'] = 102
    elif damage == 'missing-app': run.pop('app')
    elif damage == 'unobserved-run': run['id'] = 2
    elif damage == 'changed-count': pages.append({'total_count': 2, 'check_runs': []})
    elif damage == 'missing-run': pages = [{'total_count': 0, 'check_runs': []}]
    data = json.loads(path.read_text()); data['latest_pages'] = pages
    if damage == '403': data['latest_http'] = 403
    path.write_text(json.dumps(data))
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _: None)
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)


def test_latest_pages_are_combined_before_required_member_selection(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, log, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    optional = {'id': 300, 'name': 'optional', 'app': {'id': 1001}, 'head_sha': '2' * 40,
                'check_suite': {'id': 101}, 'status': 'completed', 'conclusion': 'failure'}
    current = {**optional, 'id': 1, 'name': 'quality', 'conclusion': 'success'}
    data = json.loads(path.read_text()); data['latest_pages'] = [
        {'total_count': 2, 'check_runs': [optional]}, {'total_count': 2, 'check_runs': [current]}]
    path.write_text(json.dumps(data))
    facts = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert normalize_required_checks(facts).result == 'pass'
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    requests = [call for call in calls if any('/check-runs?' in arg for arg in call)]
    assert len(requests) == 2
    assert 'page=1' in requests[0][1] and 'page=2' in requests[1][1]
    assert all('filter=latest' in call[1] and 'app_id=1001' in call[1] for call in requests)


@pytest.mark.parametrize('app', [None, -1])
def test_only_explicit_classic_unbound_app_values_accept_another_app(tmp_path, monkeypatch, app):
    from agent_run.adapters.source import read_required_check_facts, normalize_required_checks
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run(app=1002)])}, classic=True)
    data = json.loads(path.read_text()); data['classic_checks'][0]['app_id'] = app
    path.write_text(json.dumps(data))
    assert normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40)).result == 'pass'


def test_classic_context_without_explicit_check_entry_is_not_unbound(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run(app=1002)])}, classic=True)
    data = json.loads(path.read_text()); data['classic_checks'] = []
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError, match='explicit App binding'):
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)


def test_suite_coverage_change_during_latest_read_is_unproven(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    data = json.loads(path.read_text()); data['suite_count_after'] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError, match='suites changed'):
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)


@pytest.mark.parametrize('kind', ['check-runs', 'statuses'])
def test_duplicate_optional_page_member_cannot_hide_a_required_pending_member(tmp_path, monkeypatch, kind):
    from agent_run.adapters.source import read_required_check_facts
    if kind == 'check-runs':
        pending = _current_run(identity='pending', state='QUEUED'); pending['databaseId'] = 101
        nodes = [_current_run(), pending]
        first = {'id':1,'name':'quality','app':{'id':1001},'head_sha':'2'*40,
                 'check_suite':{'id':101},'status':'completed','conclusion':'success'}
        rows = [first] + [{**first, 'id': i, 'name': f'optional-{i}'} for i in range(2, 101)]
        field, key = 'check_runs', 'latest_pages'
    else:
        nodes = [{'__typename':'StatusContext','id':'current','context':'quality','state':'SUCCESS','isRequired':True},
                 {'__typename':'StatusContext','id':'pending','context':'quality','state':'PENDING','isRequired':True}]
        rows = [{'id':1,'node_id':'current','context':'quality','state':'success'}] + [
            {'id':i,'node_id':f'optional-{i}','context':f'optional-{i}','state':'success'} for i in range(2,101)]
        field, key = 'statuses', 'status_pages'
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page(nodes)}, classic=kind == 'statuses')
    data = json.loads(path.read_text())
    if kind == 'statuses': data['classic_checks'][0]['app_id'] = None
    pending_row = ({**first, 'id':101,'status':'queued','conclusion':None} if kind == 'check-runs'
                   else {'id':101,'node_id':'pending','context':'quality','state':'pending'})
    data[key] = [{'total_count':101, 'sha':'2'*40, field:rows},
                 {'total_count':101, 'sha':'2'*40, field:[pending_row]}]
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)
    data[key][1][field] = [rows[-1]]
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError, match='duplicate member IDs'):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)


def test_suite_scope_without_member_identity_cannot_prove_latest_coverage(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'': _rollup_page([_current_run()])})
    data = json.loads(path.read_text()); data['suite_sample'] = {'id':None,'head_sha':'2'*40,'app':{'id':1001}}
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)


@pytest.mark.parametrize('field', ['check_runs', 'check_suites', 'statuses'])
@pytest.mark.parametrize('damage', ['duplicate', 'missing-id', 'missing-member', 'changed-count', 'foreign-head'])
def test_all_latest_collection_members_are_validated_before_required_filter(tmp_path, monkeypatch, field, damage):
    from agent_run.adapters.source import read_required_check_facts
    if field == 'statuses':
        nodes = [{'__typename':'StatusContext','id':'current','context':'quality','state':'SUCCESS','isRequired':True}]
        rows = [{'id':1,'node_id':'current','context':'quality','state':'success'},
                {'id':2,'node_id':'optional','context':'optional','state':'success'}]
        key = 'status_pages'
    else:
        nodes = [_current_run()]
        if field == 'check_runs':
            row = {'id':1,'name':'quality','app':{'id':1001},'head_sha':'2'*40,
                   'check_suite':{'id':101},'status':'completed','conclusion':'success'}
            rows = [row, {**row,'id':2,'name':'optional'}]; key = 'latest_pages'
        else:
            row = {'id':101,'head_sha':'2'*40,'app':{'id':1001}}
            rows = [row,{**row,'id':102}]; key = 'suite_pages'
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'':_rollup_page(nodes)}, classic=field=='statuses')
    data = json.loads(path.read_text())
    if field == 'statuses': data['classic_checks'][0]['app_id'] = None
    pages = [{'total_count':2,'sha':'2'*40,field:[rows[0]]}, {'total_count':2,'sha':'2'*40,field:[rows[1]]}]
    if damage == 'duplicate': pages[1][field] = [rows[0]]
    elif damage == 'missing-id': rows[1].pop('id')
    elif damage == 'missing-member': pages[1][field] = []
    elif damage == 'changed-count': pages[1]['total_count'] = 3
    elif field == 'statuses': pages[1]['sha'] = '3'*40
    else: rows[1]['head_sha'] = '3'*40
    data[key] = pages; path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)


def test_same_count_with_replaced_suite_identity_is_not_stable_coverage(tmp_path, monkeypatch):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'':_rollup_page([_current_run()])})
    data = json.loads(path.read_text())
    data['suite_pages_after'] = [{'total_count':1,'check_suites':[{'id':102,'head_sha':'2'*40,'app':{'id':1001}}]}]
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError, match='suites changed'):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)


@pytest.mark.parametrize('headers', ['', 'HTTP/1.1 200 OK\r\nLink: malformed\r\n\r\n',
                                    'HTTP/1.1 200 OK\r\nLink: <https://example.invalid/2>; rel="next"\r\n\r\n'])
def test_missing_or_contradictory_pagination_headers_cannot_end_collection(tmp_path, monkeypatch, headers):
    from agent_run.adapters.source import read_required_check_facts
    reader, _, path = _bound_reader(tmp_path, monkeypatch, {'':_rollup_page([_current_run()])})
    data = json.loads(path.read_text()); data['latest_headers'] = headers
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_required_check_facts(reader,17,expected_head_sha='2'*40)


@pytest.mark.parametrize('branch', ['trunk', 'feature/ci', 'feature/#fix%'])
def test_repository_fixture_reads_only_the_exact_declared_default_ref(tmp_path, monkeypatch, branch):
    from urllib.parse import quote
    reader, log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    data.update(default_branch=branch, refs={'refs/heads/' + branch: 'b' * 40})
    responses.write_text(json.dumps(data))
    repository = reader.repository()
    assert (repository.name_with_owner, repository.default_branch, repository.default_head_sha) == ('example/project', branch, 'b' * 40)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[-1] == ['api', 'repos/example/project/git/ref/heads/' + quote(branch, safe='')]
    assert not any('/commits/' in str(command) for command in commands)


@pytest.mark.parametrize('status', [404, 403, 503])
def test_repository_fixture_preserves_absence_and_ref_read_failures(tmp_path, monkeypatch, status):
    from agent_run.adapters import github_commands
    reader, log = _reader(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(github_commands.time, 'sleep', delays.append)
    responses = tmp_path / 'responses.json'
    data = json.loads(responses.read_text())
    if status == 404:
        data['refs'] = {'refs/heads/another': 'c' * 40}
    else:
        data['ref_errors'] = {'repos/example/project/git/ref/heads/main': f'HTTP {status} ref unavailable'}
    responses.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as failure:
        reader.repository()
    assert failure.value.code == 'github_read_failed'
    assert f'HTTP {status}' in failure.value.message
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[1:] == [['api', 'repos/example/project/git/ref/heads/main']] * 3
    assert not any('graphql' in command for command in commands)
    assert len(delays) == 2 and 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25


@pytest.mark.parametrize('endpoint', [
    'repos/example/project/git/ref/heads/unknown',
    'repos/another/project/git/ref/heads/main',
    'repos/example/project/commits/main',
])
def test_repository_fixture_never_falls_back_for_another_ref_or_old_commit_route(endpoint):
    from support.github_checks import repository_response
    result = repository_response(['api', endpoint], repository='example/project', default_branch='main',
                                 refs={'refs/heads/main': 'a' * 40})
    assert result.returncode == 1 and result.stdout == '' and 'HTTP 404' in result.stderr


@pytest.mark.parametrize(('exit_code', 'stdout', 'stderr', 'code', 'category', 'shape'), [
    (23, '', 'dial tcp: i/o timeout', 'github_read_failed', 'network', 'empty'),
    (23, '{"private":"payload"}', 'HTTP 403', 'github_read_failed', 'command_failed', 'json_object'),
    (0, '', '', 'github_invalid_response', 'invalid_json', 'empty'),
    (0, 'private response text', '', 'github_invalid_response', 'invalid_json', 'text'),
])
def test_required_checks_failure_keeps_safe_exit_and_shape(
    tmp_path, monkeypatch, exit_code, stdout, stderr, code, category, shape,
):
    from agent_run.adapters.source import read_required_check_facts
    reader, _ = _reader(tmp_path, monkeypatch)
    script = Path(os.environ['PATH'].split(os.pathsep)[0]) / 'gh'
    original = script.read_text()
    first, rest = original.split('\n', 1)
    script.write_text(first + '\nimport sys\n'
        "if sys.argv[1:3]==['api','graphql']:\n"
        f' sys.stdout.write({stdout!r});sys.stderr.write({(stderr + " Authorization: Bearer secret-fixture-value")!r});raise SystemExit({exit_code})\n'
        + rest)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _: None)
    with pytest.raises(GitHubReadError) as failed:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert failed.value.code == code
    assert f'exit={exit_code}' in failed.value.message
    assert f'output_shape={shape}' in failed.value.message
    assert f'category={category}' in failed.value.message
    assert 'secret-fixture-value' not in failed.value.message and 'private' not in failed.value.message
    assert len(failed.value.message.encode()) < 512
