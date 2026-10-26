"""Credential diagnostics observe existing renewal; they never authorize retry."""
import json
from dataclasses import replace
import os
import sys
import threading
import time

from agent_run.adapters.auth.app import GitHubCredentialError
from agent_run.adapters.auth.credential_lifecycle import ReadCredential, RenewableCredential
import pytest


def test_renewal_diagnostic_records_last_failure_and_valid_old_token_without_secret():
    now = [1000.0]
    calls = []
    delivered = []
    exhausted = threading.Event()
    def provider():
        calls.append(True)
        if len(calls) > 1:
            raise GitHubCredentialError('private issuer body and credential', http_status=503)
        return ReadCredential('private-test-token', 4600.0)
    lifecycle = RenewableCredential(provider, clock=lambda: now[0],
        on_update=delivered.append, on_exhausted=lambda message: exhausted.set())
    def observe(attempts):
        deadline = time.monotonic() + 2
        while True:
            result = lifecycle.diagnostic()
            if result['credential_attempts'] == attempts:
                return result
            assert time.monotonic() < deadline, 'Renewal failure was not observed'
            time.sleep(0.005)
    try:
        lifecycle.start()
        initial = lifecycle.diagnostic()
        assert initial['credential_failure_observed'] is False
        assert initial['credential_valid_at_last_failure'] is None
        now[0] = 4310.0
        lifecycle.wake()
        first = observe(1)
        assert first['credential_stage'] == 'renewal'
        assert first['credential_retries'] == 0
        assert first['credential_failure_observed'] is True
        assert first['http_status'] == 503
        assert first['credential_valid_at_last_failure'] is True
        assert first['credential_remaining_seconds_at_last_failure'] == 290.0
        assert len(delivered) == 1 and not exhausted.is_set()
        now[0] = 4312.0
        lifecycle.wake()
        second = observe(2)
        assert second['credential_retries'] == 1
        assert second['credential_remaining_seconds_at_last_failure'] == 288.0
        assert first['credential_remaining_seconds_at_last_failure'] == 290.0
        now[0] = 4600.0
        lifecycle.wake()
        assert exhausted.wait(2)
        # Historical failure observations never become a new current validity claim.
        assert lifecycle.diagnostic() == second
        text = json.dumps(second, allow_nan=False)
        assert 'private' not in text and len(text.encode()) < 512
        assert len(calls) == 3 and len(delivered) == 1
    finally:
        lifecycle.close()


def test_real_worker_credential_expiry_persists_only_safe_last_failure(tmp_path, monkeypatch):
    from agent_run.adapters.auth import app, worker_credentials
    from agent_run.adapters.auth.credential_lifecycle import WorkerCredentialError
    from agent_run.queries.run import read
    from agent_run.runtime.codex import execute_selected_codex
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec
    from support.core_operations import native_agent_fixture
    from support.offline import fixture_inputs
    identity = {'source': 'app', 'app_profile': {'app_id': '1', 'installation_id': '2',
        'private_key_path': str(tmp_path / 'unused-key')}, 'app_read_only': True,
        'app_current_repository_only': True}
    (tmp_path / 'state').mkdir()
    spec = RunSpec('credential-observation', 'example/repository', 1, 'parent', 'graph', (
        Delivery('ticket:2', DeliveryKind.TICKET, 2, 'original', Commit('base', 'tree'),
                 'refs/heads/ticket', 'refs/heads/run', checkout=str(tmp_path)),),
        Policy(ticket_rounds=1, development_deadline_seconds=120),
        inputs=replace(fixture_inputs(), worker_identity=identity))
    kernel, ports = native_agent_fixture(tmp_path / 'state/kernel.sqlite3', spec)
    kernel.clock = lambda: 1000.0
    execution = kernel.claim_executor('credential-observation-owner')
    runner = OperationRunner(kernel, execution, lambda _: ports)
    request = None
    for _ in range(6):
        request = runner.run_once().preparation
        if request is not None:
            break
    assert request is not None
    now, lives, calls, errors = [1000.0], [], [], []
    stopped, cancelled = threading.Event(), threading.Event()
    marker = tmp_path / 'actual-worker-entered'
    class Provider:
        def __init__(self, *args, **kwargs):
            pass
        def __call__(self):
            calls.append(True)
            if len(calls) > 1:
                raise GitHubCredentialError('private issuer response', http_status=503)
            return ReadCredential('private-test-token', 4600.0)
        def cancel(self):
            cancelled.set()
    def clocked(*args, **kwargs):
        lifecycle = RenewableCredential(*args, clock=lambda: now[0], **kwargs)
        lives.append(lifecycle)
        return lifecycle
    monkeypatch.setattr(app, '_GitHubAppCredentialProvider', Provider)
    monkeypatch.setattr(worker_credentials, 'RenewableCredential', clocked)
    def advance():
        try:
            deadline = time.monotonic() + 4
            while not marker.exists():
                if stopped.wait(0.005):
                    return
                assert time.monotonic() < deadline, 'Actual Worker did not enter'
            now[0] = 4310.0
            lives[0].wake()
            while lives[0].diagnostic()['credential_attempts'] != 1:
                assert time.monotonic() < deadline, 'Renewal failed observation was not recorded'
                if stopped.wait(0.005):
                    return
            now[0] = 4600.0
            lives[0].wake()
        except BaseException as error:
            errors.append(error)
    executable = tmp_path / 'local-codex'
    executable.write_text(f'#!{sys.executable}\nimport json,signal\nfrom pathlib import Path\n'
        "print(json.dumps({'type':'thread.started','thread_id':'credential-thread'}),flush=True)\n"
        f'Path({str(marker)!r}).write_text("entered")\nsignal.pause()\n')
    executable.chmod(0o700)
    controller = threading.Thread(target=advance)
    controller.start()
    try:
        with pytest.raises(WorkerCredentialError):
            execute_selected_codex(kernel, request, identity=identity,
                environment={'PATH': os.defpath, 'HOME': str(tmp_path)}, prompt='Wait in the controlled Worker',
                executable=str(executable), hidden_paths=(), authorize_source=runner.authorize_agent, timeout=5)
    finally:
        stopped.set()
        controller.join(timeout=1)
    assert not controller.is_alive() and not errors and cancelled.is_set()
    document = read(kernel.storage.path, 'status').data
    subject = next(item for item in document['subjects'] if item['subject'] == request.subject)
    output = subject['pending_attempt']['invocations'][0]['outputs'][0]
    proof = output['failure_evidence']
    assert proof['returncode'] == -15 and proof['failure'] == 'WorkerCredentialError'
    diagnostic = proof['diagnostic']
    assert diagnostic['source'] == 'worker-credentials' and diagnostic['credential_stage'] == 'renewal'
    assert diagnostic['credential_attempts'] == 1 and diagnostic['credential_retries'] == 0
    assert diagnostic['credential_valid_at_last_failure'] is True
    assert diagnostic['credential_remaining_seconds_at_last_failure'] == 290.0
    assert diagnostic['http_status'] == 503 and diagnostic['final_output_json_shape'] == 'empty'
    serialized = json.dumps(document)
    assert 'private-test-token' not in serialized and 'private issuer response' not in serialized
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 1
    assert runner.state().active == () and kernel.worker_settled(request)
