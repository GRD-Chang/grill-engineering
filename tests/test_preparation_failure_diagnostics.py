"""Safe native-auth facts survive actual preparation into public query evidence."""
import json
import sys
import time
from types import SimpleNamespace

import pytest

from agent_run.errors import GitHubIdentityError, GitHubReadError
from agent_run.adapters.auth.credential_lifecycle import InitialCredentialUnavailable
from agent_run.queries.run import read
from agent_run.runtime.codex import execute_selected_codex
from support.core_operations import agent_preparation_fixture


@pytest.mark.parametrize(('status', 'stderr', 'category', 'retryable'), [
    (None, 'dial tcp: network is unreachable', 'network', True),
    (401, 'arbitrary private text', 'unauthorized', False),
    (None, 'arbitrary private text', 'command_failed', False),
    (None, 'process-error', 'process_error', False),
    (None, 'timeout', 'timeout', True),
])
def test_native_preparation_retains_safe_auth_diagnostic_without_invocation(
    tmp_path, monkeypatch, status, stderr, category, retryable,
):
    from agent_run.adapters.auth import credential_lifecycle
    now = [time.monotonic()]
    monkeypatch.setattr(credential_lifecycle, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + max(seconds, 30))))
    kernel, runner, _, step, _ = agent_preparation_fixture(tmp_path)
    request = step.preparation
    assert request is not None
    tools = tmp_path / 'tools'
    tools.mkdir()
    gh = tools / 'gh'
    calls = tmp_path / 'auth-calls'
    header = f'HTTP/2.0 {status} Synthetic\r\n\r\n' if status is not None else ''
    gh.write_text(f'#!{sys.executable}\nimport sys\n'
        f"from pathlib import Path\nwith Path({str(calls)!r}).open('a') as log:log.write('call\\n')\n"
        "assert sys.argv[1:]==['api','--hostname','github.com','user','--include','--silent']\n"
        f'sys.stdout.write({header!r})\nsys.stderr.write({(stderr + " Authorization: Bearer secret-fixture-value")!r})\n'
        'raise SystemExit(23)\n')
    gh.chmod(0o700)
    if stderr == 'process-error':
        gh.unlink()
    elif stderr == 'timeout':
        from agent_run.adapters.auth import identity
        from agent_run.adapters.auth.native_probe import capture_auth_probe
        gh.write_text(f'#!{sys.executable}\nimport signal\nsignal.pause()\n')
        monkeypatch.setattr(identity, 'capture_auth_probe', lambda process, timeout:
            capture_auth_probe(process, timeout=0))
    with pytest.raises((GitHubReadError, GitHubIdentityError, InitialCredentialUnavailable)):
        execute_selected_codex(kernel, request, identity={'source': 'host'},
            environment={'PATH': str(tools), 'HOME': str(tmp_path)}, prompt='Never execute',
            executable='never-started-codex', hidden_paths=(), authorize_source=runner.authorize_agent)
    document = read(kernel.storage.path, 'status', storage=kernel.storage).data
    action = next(item for item in document['operator_actions'] if item['status_code'] == 'worker_unavailable')
    evidence = action['evidence']['failure_evidence']['evidence']
    assert evidence['category'] == category
    if stderr == 'process-error':
        assert evidence['errno'] == 2 and 'returncode' not in evidence
    elif stderr == 'timeout':
        assert type(evidence['returncode']) is int and evidence['returncode'] != 0
    else:
        assert evidence['returncode'] == 23
    assert evidence['retryable'] is retryable
    assert evidence['http_status'] == status
    assert evidence['source'] == 'worker-preparation'
    if stderr not in {'process-error', 'timeout'}:
        attempts = len(calls.read_text().splitlines())
        assert evidence['credential_attempts'] == attempts
        assert evidence['credential_retries'] == attempts - 1
    assert evidence['credential_stage'] == 'initial'
    assert evidence['credential_valid_at_last_failure'] is False
    assert evidence['credential_remaining_seconds_at_last_failure'] is None
    assert action['evidence']['failure_evidence']['deadline'] == request.preparation_deadline
    assert action['evidence']['failure_evidence']['started_at'] == 1000.0
    serialized = json.dumps(document)
    assert 'secret-fixture-value' not in serialized and 'arbitrary private text' not in serialized
    assert len(json.dumps(evidence).encode()) < 512
    assert kernel.worker_settled(request) and runner.state().preparing == ()
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
    assert not list(kernel.storage.path.parent.glob('worker-*'))


def test_credential_diagnostic_rejects_arbitrary_provider_metadata():
    from agent_run.adapters.auth.credential_lifecycle import WorkerCredentialError, credential_diagnostic
    error = WorkerCredentialError('private unstructured failure', diagnostic={
        'category': ['private category'], 'returncode': True, 'errno': -1,
        'http_status': '401 secret', 'retryable': 'private', 'stderr': 'private stderr'})
    assert credential_diagnostic(error) == {'http_status': None}


@pytest.mark.parametrize('remaining', [float('nan'), float('inf'), -1, True, 10**1000, 'private expiry'])
def test_credential_diagnostic_rejects_unproved_duration_and_counter(remaining):
    from agent_run.adapters.auth.credential_lifecycle import WorkerCredentialError, credential_diagnostic
    error = WorkerCredentialError('private provider details', diagnostic={'credential_stage': 'renewal',
        'credential_attempts': True, 'credential_retries': -1,
        'credential_remaining_seconds_at_last_failure': remaining})
    result = credential_diagnostic(error)
    assert 'credential_attempts' not in result and 'credential_retries' not in result
    assert 'credential_remaining_seconds_at_last_failure' not in result
    assert result['credential_failure_observed'] is None and result['credential_valid_at_last_failure'] is None
    assert 'private' not in json.dumps(result, allow_nan=False)
