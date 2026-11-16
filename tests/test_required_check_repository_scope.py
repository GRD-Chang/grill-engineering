"""A repository snapshot belongs to one complete required-check observation."""
import json
import subprocess

import pytest

from agent_run.adapters.source import normalize_required_checks, read_required_check_facts
from agent_run.errors import GitHubReadError
from support.github_retries import set_read_retry_sleep
from support.remote_effects import _reader


def test_required_checks_resolve_repository_once_and_refresh_the_next_observation(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    first = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 7
    assert sum(call[:2] == ['repo', 'view'] for call in calls) == 1
    assert sum(call[:2] == ['api', 'repos/example/project/git/ref/heads/main'] for call in calls) == 1
    assert sum(call[:2] == ['api', 'repos/example/project/pulls/17'] for call in calls) == 2
    assert first['pr_before'] == first['pr_after']
    assert first['repository_default_branch'] == 'main'
    assert normalize_required_checks(first).result == 'none'

    path = tmp_path / 'responses.json'
    changed = json.loads(path.read_text())
    changed['default_branch'] = 'trunk'
    changed['refs'] = {'refs/heads/trunk': '3' * 40}
    changed['pull_request']['base']['ref'] = 'trunk'
    changed['rulesets'] = [{'id': 42, 'target': 'branch', 'enforcement': 'active'}]
    changed['ruleset_detail'] = {'id': 42, 'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH']}},
        'rules': [{'type': 'required_status_checks', 'parameters': {
            'required_status_checks': [{'context': 'new-required', 'integration_id': None}]}}]}
    path.write_text(json.dumps(changed))
    log.unlink()
    second = read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 8  # One additional ruleset detail, not a cached configuration.
    assert sum(call[:2] == ['repo', 'view'] for call in calls) == 1
    assert sum(call[:2] == ['api', 'repos/example/project/git/ref/heads/trunk'] for call in calls) == 1
    assert sum(call[:2] == ['api', 'repos/example/project/pulls/17'] for call in calls) == 2
    assert second['pr_before'] == second['pr_after'] == changed['pull_request']
    assert second['repository_default_branch'] == 'trunk'
    assert normalize_required_checks(second).details == (('new-required', 'pending'),)
    assert first['repository_default_branch'] == 'main'


@pytest.mark.parametrize('head_read', ['before', 'after'])
def test_required_checks_reject_head_changes_at_both_fresh_pr_reads(tmp_path, monkeypatch, head_read):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    if head_read == 'before':
        data['pull_request']['head']['sha'] = '3' * 40
    else:
        data['pr_head_after'] = '3' * 40
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as failed:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert failed.value.code == 'change_pr_head_drift'


@pytest.mark.parametrize('boundary', ['repository', 'default-ref', 'pr-before', 'pr-after', 'rollup', 'rulesets', 'classic'])
def test_required_checks_permission_errors_never_become_unprotected_success(tmp_path, monkeypatch, boundary):
    import agent_run.adapters.github_commands as commands
    reader, _ = _reader(tmp_path, monkeypatch)
    original = commands._run_bounded_command
    pr_reads = 0
    def read(arguments, **kwargs):
        nonlocal pr_reads
        endpoint = arguments[2] if arguments[:2] == ['gh', 'api'] else ''
        if endpoint == 'repos/example/project/pulls/17':
            pr_reads += 1
        refused = (boundary == 'repository' and arguments[:3] == ['gh', 'repo', 'view']
            or boundary == 'default-ref' and '/git/ref/heads/' in endpoint
            or boundary == 'pr-before' and endpoint.endswith('/pulls/17')
            or boundary == 'pr-after' and endpoint.endswith('/pulls/17') and pr_reads >= 2
            or boundary == 'rollup' and endpoint == 'graphql'
            or boundary == 'rulesets' and endpoint.endswith('/rulesets')
            or boundary == 'classic' and endpoint.endswith('/protection/required_status_checks'))
        if refused:
            return subprocess.CompletedProcess(arguments, 1, '', 'HTTP 403 forbidden')
        return original(arguments, **kwargs)
    monkeypatch.setattr(commands, '_run_bounded_command', read)
    set_read_retry_sleep(monkeypatch, lambda _: None)
    with pytest.raises(GitHubReadError) as failed:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert failed.value.code == 'github_read_failed'


def test_completed_observation_does_not_hide_a_later_repository_permission_failure(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    # This is a freshness/error test, not a real-time backoff boundary. Keep
    # every actual command and its scheduled delays without sleeping here.
    delays = []
    set_read_retry_sleep(monkeypatch, delays.append)
    assert normalize_required_checks(read_required_check_facts(reader, 17, expected_head_sha='2' * 40)).result == 'none'
    before = len(log.read_text().splitlines())
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['ref_errors'] = {'repos/example/project/git/ref/heads/main': 'HTTP 403 ref unavailable'}
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as failed:
        read_required_check_facts(reader, 17, expected_head_sha='2' * 40)
    assert failed.value.code == 'github_read_failed'
    assert 'HTTP 403' in failed.value.message
    later = [json.loads(line) for line in log.read_text().splitlines()[before:]]
    assert sum(command[:2] == ['repo', 'view'] for command in later) == 1
    assert sum(command[:2] == ['api', 'repos/example/project/git/ref/heads/main'] for command in later) == 3
    assert len(delays) == 2 and 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25
