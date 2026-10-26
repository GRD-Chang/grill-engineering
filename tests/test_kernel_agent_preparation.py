"""Native preparation seats precede credentials and real Invocation admission."""
from dataclasses import replace
from pathlib import Path
import json
import os
import sys

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec
from agent_run.runtime.execution import execute
from support.core_operations import native_agent_fixture


def preparation(tmp_path: Path, *, concurrency: int = 1):
    base = Commit('base', 'tree')
    spec = RunSpec('preparation', 'example/repository', 1, 'parent', 'graph', (
        Delivery('ticket:2', DeliveryKind.TICKET, 2, 'original', base,
                 'refs/heads/ticket', 'refs/heads/run', checkout=str(tmp_path)),
    ), Policy(ticket_rounds=1, development_deadline_seconds=120, concurrency=concurrency))
    (tmp_path / 'state').mkdir()
    kernel, ports = native_agent_fixture(tmp_path / 'state' / 'kernel.sqlite3', spec)
    clock = [1000.0]
    kernel.clock = lambda: clock[0]
    owner = kernel.claim_executor('preparation-owner')
    runner = OperationRunner(kernel, owner, lambda _: ports)
    for _ in range(6):
        step = runner.run_once()
        if step.call is not None or getattr(step, 'preparation', None) is not None:
            return kernel, runner, ports, step, clock
    raise AssertionError('Core did not reserve Agent work')


def counts(kernel):
    with kernel.storage.transaction(write=False) as tx:
        return tuple(tx.one(f'SELECT count(*) FROM {table}')[0]
                     for table in ('attempts', 'invocations', 'invocation_outputs', 'leases'))


def test_credentials_precede_invocation_and_the_one_seat_transfers_atomically(tmp_path: Path) -> None:
    kernel, runner, _, step, clock = preparation(tmp_path)
    assert counts(kernel) == (1, 0, 0, 0)
    request = step.preparation
    assert request is not None and step.call is None
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    initial = runner.state()
    assert initial.active == () and initial.preparing == (request.reservation_id,)
    assert request.preparation_deadline == 1600
    for _ in range(3):
        clock[0] += 10
        runner.run_once()
        assert counts(kernel) == (1, 0, 0, 0)
        assert runner.state().preparing == initial.preparing
    kernel.guard_preparation(request)
    call = kernel.admit_agent(request).call
    assert call is not None and call.lease_id == request.reservation_id
    assert call.deadline_at == 1150 and call.deadline_seconds == 120
    assert counts(kernel) == (1, 1, 1, 1)
    assert runner.state().active == initial.preparing and runner.state().preparing == ()
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    with pytest.raises(KernelError, match='preparation_binding'):
        kernel.admit_agent(request)
    assert counts(kernel) == (1, 1, 1, 1)


def test_preparation_failure_retains_attempt_and_thread_without_charging(tmp_path: Path) -> None:
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    kernel.preparation_failed(request, 'worker_preparation_failed', {'closed': True, 'http_status': 503})
    assert kernel.worker_settled(request) and runner.state().preparing == ()
    assert counts(kernel) == (1, 0, 0, 0)
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    kernel.control('resume', 'credentials-repaired', subject=request.subject)
    resumed = runner.run_once().preparation
    assert resumed is not None and resumed.attempt_id == request.attempt_id
    assert resumed.thread_id == request.thread_id and resumed.reservation_id != request.reservation_id
    assert counts(kernel) == (1, 0, 0, 0)


@pytest.mark.parametrize('change', ['stop', 'expiry', 'binding'])
def test_preparation_revocation_holds_seat_until_cleanup(tmp_path: Path, change: str) -> None:
    kernel, runner, _, step, clock = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    if change == 'stop':
        kernel.control('stop', 'stop-preparation', subject=request.subject)
    elif change == 'expiry':
        clock[0] = request.preparation_deadline
    else:
        request = replace(request, thread_id='foreign-thread')
    with pytest.raises(KernelError):
        kernel.admit_agent(request)
    assert counts(kernel) == (1, 0, 0, 0)
    original = step.preparation
    assert not kernel.worker_settled(original)
    assert runner.state().preparing == (original.reservation_id,)
    if change != 'binding':
        assert original.reservation_id in runner.state().cancel
    kernel.preparation_failed(original, 'worker_preparation_failed', {'closed': True})
    assert kernel.worker_settled(original)
    assert counts(kernel) == (1, 0, 0, 0)


def test_exact_executor_exit_settles_preparation_but_unknown_exit_does_not(tmp_path: Path) -> None:
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    kernel.control('stop', 'stop-owned-preparation')
    with pytest.raises(KernelError, match='executor_exit_unproven'):
        kernel.control('resume', 'premature-resume')
    assert runner.state().preparing == (request.reservation_id,)
    with pytest.raises(KernelError):
        kernel.confirm_executor_exit(replace(request.execution, owner='foreign'))
    assert not kernel.worker_settled(request)
    kernel.confirm_executor_exit(request.execution)
    assert kernel.worker_settled(request) and counts(kernel) == (1, 0, 0, 0)
    kernel.control('resume', 'confirmed-exit-resume')


def test_final_release_charges_once_and_late_return_is_not_a_second_charge(tmp_path: Path) -> None:
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    call = kernel.admit_agent(request).call
    assert call is not None
    marker = tmp_path / 'actual-worker'
    raw = b'{"result_kind":"development","summary":"actual local work","human_blockers":null}'
    result = execute(kernel, call, arguments=[sys.executable, '-I', '-c',
        f"from pathlib import Path; import sys; Path({str(marker)!r}).write_text('started'); sys.stdout.buffer.write({raw!r})"],
        cwd=tmp_path, prompt='', environment={'PATH': os.defpath}, timeout=5,
        authorize_source=lambda: runner.authorize_agent(call))
    assert result is not None and result.returncode == 0 and marker.read_text() == 'started'
    assert kernel.worker_settled(request)
    assert kernel.status(call.subject)['budgets']['requirements']['developments_used'] == 1
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT accounted FROM attempts WHERE id=?', (call.attempt_id,))[0] == 1
    kernel.control('stop', 'stop-after-actual-work', subject=call.subject)
    kernel.receive(call, raw)
    assert kernel.status(call.subject)['budgets']['requirements']['developments_used'] == 1
    assert execute(kernel, call, arguments=['/bin/false'], cwd=tmp_path, prompt='',
        environment={'PATH': os.defpath}, timeout=5) is None


def test_failed_spawn_does_not_charge_and_requires_explicit_same_attempt_resume(tmp_path: Path, monkeypatch) -> None:
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    call = kernel.admit_agent(request).call
    assert call is not None
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('bootstrap spawn failed before any process existed')
    monkeypatch.setattr('agent_run.runtime.execution.subprocess.Popen', unavailable)
    assert execute(kernel, call, arguments=['/bin/true'], cwd=tmp_path,
                   prompt='', environment={'PATH': os.defpath}, timeout=5) is None
    assert kernel.worker_settled(request)
    assert kernel.status(call.subject)['budgets']['requirements']['developments_used'] == 0
    kernel.control('resume', 'retry-unstarted-worker', subject=call.subject)
    resumed = runner.run_once().preparation
    assert resumed is not None and resumed.attempt_id == request.attempt_id
    assert resumed.thread_id == request.thread_id


@pytest.fixture
def credential_clock(monkeypatch):
    from types import SimpleNamespace
    from agent_run.adapters.auth import credential_lifecycle
    now = [0.0]
    monkeypatch.setattr(credential_lifecycle, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    return now


@pytest.mark.parametrize('failure', ['transient', 'invalid', 'timeout'])
def test_initial_native_credential_wait_is_bounded_without_worker_retries(
    tmp_path: Path, credential_clock, failure: str,
) -> None:
    import time
    from agent_run.adapters.auth.app import GitHubCredentialError
    from agent_run.adapters.auth.credential_lifecycle import ReadCredential, WorkerCredentialError
    from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery
    attempts = []
    guards = []
    def provider():
        attempts.append(True)
        if failure != 'transient' or len(attempts) == 1:
            raise GitHubCredentialError('private issuer diagnostic', http_status=401 if failure == 'invalid' else 503)
        return ReadCredential('synthetic-only', time.time() + 3600)
    with NativeCredentialDelivery(provider, tmp_path / 'gh') as delivery:
        if failure == 'transient':
            delivery.start(guard=lambda: guards.append(True), deadline=2)
            assert len(attempts) == 2
            assert json.loads((delivery.directory / 'hosts.yml').read_text())['github.com']['oauth_token'] == 'synthetic-only'
        else:
            with pytest.raises(WorkerCredentialError) as caught:
                delivery.start(guard=lambda: guards.append(True), deadline=2)
            assert 'private' not in str(caught.value)
            assert caught.value.http_status == (401 if failure == 'invalid' else 503)
            assert len(attempts) == (1 if failure == 'invalid' else 2)
        assert guards and credential_clock[0] <= 2
    assert not list((tmp_path / 'gh').iterdir())


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_preparation_interrupt_cleans_credentials_before_preserving_exception(
    tmp_path: Path, credential_clock, interruption,
) -> None:
    from agent_run.adapters.auth.app import GitHubCredentialError
    from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery
    attempts = []
    exact = interruption()
    def provider():
        attempts.append(True)
        raise GitHubCredentialError('temporary', http_status=503)
    def guard():
        if attempts:
            raise exact
    with NativeCredentialDelivery(provider, tmp_path / 'gh') as delivery:
        with pytest.raises(interruption) as caught:
            delivery.start(guard=guard, deadline=2)
        assert caught.value is exact
    assert len(attempts) == 1 and not list((tmp_path / 'gh').iterdir())


@pytest.mark.parametrize('control', ['ready', 'stop', 'owner', 'timeout'])
def test_native_app_preparation_owns_no_invocation_and_releases_only_after_cleanup(
    tmp_path: Path, monkeypatch, credential_clock, control: str,
) -> None:
    import time
    from agent_run.adapters.auth import app
    from agent_run.adapters.auth.credential_lifecycle import ReadCredential, WorkerCredentialError
    from agent_run.runtime.codex import _CodexCapture, execute_selected_codex
    kernel, runner, _, step, clock = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    calls = []
    class Provider:
        def __init__(self, *args, **kwargs):
            pass
        def __call__(self):
            calls.append(True)
            assert counts(kernel) == (1, 0, 0, 0)
            assert runner.state().preparing == (request.reservation_id,)
            assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
            if len(calls) == 1:
                if control == 'stop':
                    kernel.control('stop', 'stop-token-read', subject=request.subject)
                elif control == 'owner':
                    kernel.control('stop', 'stop-token-owner')
                elif control == 'timeout':
                    clock[0] = request.preparation_deadline
                raise app.GitHubCredentialError('private failure', http_status=503)
            return ReadCredential('synthetic-only', time.time() + 3600)
        def cancel(self):
            pass
    monkeypatch.setattr(app, '_GitHubAppCredentialProvider', Provider)
    identity = {'source': 'app', 'app_profile': {'app_id': '1', 'installation_id': '2',
        'private_key_path': str(tmp_path / 'unused-reference')}, 'app_read_only': True,
        'app_current_repository_only': True}
    environment = {'PATH': os.defpath, 'HOME': str(tmp_path)}
    if control == 'ready':
        # Preparation-only: no model process and no invented execution evidence.
        capture = _CodexCapture(kernel, request, executable='never-executed', environment=environment,
            credential_file=None, identity=identity, hidden_paths=(), hidden_environment=())
        with capture:
            assert len(calls) == 2 and counts(kernel) == (1, 0, 0, 0)
            call = kernel.admit_agent(request).call
            assert call is not None and call.lease_id == request.reservation_id
            capture.bind_call(call)
            assert counts(kernel) == (1, 1, 1, 1)
        assert capture.cleanup_complete
        assert kernel.dispatch_agent(call)
        kernel.agent_unavailable(call, 'preparation_test_complete', 'No Worker spawned')
        assert kernel.worker_settled(request)
    else:
        with pytest.raises((KernelError, WorkerCredentialError)):
            execute_selected_codex(kernel, request, identity=identity, environment=environment,
                prompt='Never launch revoked work', hidden_paths=(), executable='never-executed',
                authorize_source=runner.authorize_agent)
        assert len(calls) == 1 and counts(kernel) == (1, 0, 0, 0)
        assert kernel.worker_settled(request)
        assert runner.state().preparing == ()
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0


def test_preparing_and_running_work_share_capacity_and_duplicate_admission_cannot_double_count(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    base = Commit('base', 'tree')
    spec = RunSpec('seats', 'example/repository', 1, 'parent', 'graph', tuple(
        Delivery(f'ticket:{number}', DeliveryKind.TICKET, number, 'original', base,
                 f'refs/heads/ticket-{number}', 'refs/heads/run', checkout=str(tmp_path / str(number)))
        for number in (2, 3, 4)), Policy(concurrency=2))
    kernel, source_ports = native_agent_fixture(tmp_path / 'kernel.sqlite3', spec)
    from support.core_operations import ReadOnlyPorts
    class SeatsPorts(ReadOnlyPorts):
        def delivery(self, request):
            assert request.source_ref in {f'refs/heads/ticket-{n}' for n in (2, 3, 4)}
            assert request.target_ref == 'refs/heads/run'
            return self.facts
    ports = SeatsPorts(source_ports.requirements, source_ports.source_ref, source_ports.target_ref, source_ports.facts)
    owner = kernel.claim_executor('one-owner')
    runner = OperationRunner(kernel, owner, lambda _: ports)
    requests = []
    for _ in range(8):
        step = runner.run_once(agent_capacity=2)
        if step.preparation is not None:
            requests.append(step.preparation)
        if len(requests) == 2:
            break
    assert len(requests) == 2 and len({request.subject for request in requests}) == 2
    assert counts(kernel) == (2, 0, 0, 0)
    def admit():
        try:
            return kernel.admit_agent(requests[0]).call
        except KernelError as error:
            assert error.code == 'preparation_binding'
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = list(pool.map(lambda _: admit(), range(2)))
    assert sum(call is not None for call in calls) == 1
    assert counts(kernel) == (2, 1, 1, 1)
    state = runner.state()
    assert state.active == (requests[0].reservation_id,) and state.preparing == (requests[1].reservation_id,)
    assert runner.run_once(agent_capacity=2).preparation is None
    assert counts(kernel) == (2, 1, 1, 1)
    kernel.preparation_failed(requests[1], 'credentials_not_ready', {'resources_closed': True})
    next_request = runner.run_once(agent_capacity=2).preparation
    assert next_request is not None and next_request.subject == 'ticket:4'
    assert len(runner.state().active) + len(runner.state().preparing) == 2


def test_cleanup_exception_cannot_release_the_preparation_seat(tmp_path: Path, monkeypatch, credential_clock) -> None:
    from agent_run.adapters.auth import app
    from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery
    from agent_run.runtime.codex import execute_selected_codex
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    class InvalidProvider:
        def __init__(self, *args, **kwargs):
            pass
        def __call__(self):
            raise app.GitHubCredentialError('invalid', http_status=401)
        def cancel(self):
            pass
    close = NativeCredentialDelivery.close
    def failed_cleanup(delivery):
        close(delivery)
        raise OSError('cleanup completion not acknowledged')
    monkeypatch.setattr(app, '_GitHubAppCredentialProvider', InvalidProvider)
    monkeypatch.setattr(NativeCredentialDelivery, 'close', failed_cleanup)
    with pytest.raises(Exception):
        execute_selected_codex(kernel, request, identity={'source': 'app', 'app_profile': {
            'app_id': '1', 'installation_id': '2', 'private_key_path': str(tmp_path / 'unused')},
            'app_read_only': True, 'app_current_repository_only': True},
            environment={'PATH': os.defpath, 'HOME': str(tmp_path)}, prompt='', hidden_paths=(),
            authorize_source=runner.authorize_agent)
    assert counts(kernel) == (1, 0, 0, 0)
    assert not kernel.worker_settled(request)
    assert runner.state().preparing == (request.reservation_id,)
    kernel.control('stop', 'stop-after-cleanup-failure')
    kernel.confirm_executor_exit(request.execution)
    assert kernel.worker_settled(request)


def test_native_host_read_retries_before_any_invocation_exists(tmp_path: Path, monkeypatch, credential_clock) -> None:
    from agent_run.runtime.codex import _CodexCapture
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    assert request is not None
    gh = tmp_path / 'gh'
    log = tmp_path / 'probe-count'
    gh.write_text(f'#!{sys.executable}\n' + f'''from pathlib import Path
import sys
log = Path({str(log)!r})
previous = log.read_text() if log.exists() else ''
log.write_text(previous + 'read\\n')
assert sys.argv[1:] == ['api', '--hostname', 'github.com', 'user', '--include', '--silent']
print('HTTP/2 ' + ('502 Bad Gateway' if not previous else '200 OK') + '\\r\\n\\r\\n')
raise SystemExit(1 if not previous else 0)
''')
    gh.chmod(0o700)
    capture = _CodexCapture(kernel, request, executable='never-executed',
        environment={'PATH': str(tmp_path) + os.pathsep + os.defpath, 'HOME': str(tmp_path)},
        credential_file=None, hidden_paths=(), hidden_environment=(), identity={'source': 'host'})
    with capture:
        assert log.read_text() == 'read\nread\n'
        assert capture.inherit_host_auth and counts(kernel) == (1, 0, 0, 0)
    assert capture.cleanup_complete
    kernel.preparation_failed(request, 'preparation_test_complete', {'resources_closed': True})
    assert kernel.worker_settled(request) and runner.state().preparing == ()


def test_initial_wait_cannot_extend_the_absolute_preparation_limit(credential_clock) -> None:
    from agent_run.adapters.auth.credential_lifecycle import InitialCredentialUnavailable, prepare_initial_credential
    from agent_run.adapters.auth.app import GitHubCredentialError
    deadlines = []
    def temporary():
        deadlines.append(credential_clock[0])
        raise GitHubCredentialError('temporary', http_status=503)
    with pytest.raises(InitialCredentialUnavailable) as caught:
        prepare_initial_credential(temporary, guard=lambda: None, deadline=10000)
    assert credential_clock[0] == 600 and deadlines[-1] < 600
    assert caught.value.http_status == 503 and len(deadlines) < 30


def test_v20_input_is_rejected_without_mutating_its_protocol_or_bytes(tmp_path: Path) -> None:
    import sqlite3
    from agent_run.storage.preflight import UnsupportedState, require_supported_connection, require_supported_state
    # An unsupported input classifier fixture, never a manufactured live Run.
    path = tmp_path / 'old-protocol.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(protocol TEXT)')
        db.execute('INSERT INTO meta VALUES (?)', ('independent-delivery-kernel-v20',))
    original = path.read_bytes()
    with pytest.raises(UnsupportedState) as rejected:
        require_supported_state(path)
    assert rejected.value.protocol == 'independent-delivery-kernel-v20'
    with sqlite3.connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(UnsupportedState):
            require_supported_connection(db, path)
    assert path.read_bytes() == original


def test_unknown_or_foreign_host_exit_cannot_free_the_original_preparation(tmp_path: Path) -> None:
    from agent_run.model import Execution, HostObservation
    kernel, _, ports, first, _ = preparation(tmp_path)
    original = first.preparation
    assert original is not None
    kernel.preparation_failed(original, 'fixture_resources_closed', {'closed': True})
    kernel.control('stop', 'retire-unit-owner')
    kernel.confirm_executor_exit(original.execution)
    kernel.control('resume', 'resume-original-run')
    kernel.control('resume', 'resume-original-subject', subject=original.subject)
    request = kernel.admit_host('managed-host', kind='run', command=('unit-host',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation('running', request.unit_name,
        'original-host', '/host', 'boot', 42, 'start', usage_ready=True)) == 'running'
    execution = Execution(request.run_id, request.owner, request.epoch)
    runner = OperationRunner(kernel, execution, lambda _: ports)
    pending = runner.run_once().preparation
    assert pending is not None and pending.execution == execution
    assert kernel.observe_host(request, HostObservation('unknown', request.unit_name)) == 'unknown'
    assert not kernel.worker_settled(pending)
    foreign = HostObservation('exited', request.unit_name, 'foreign-host', '/host', 'boot',
        exit_status=0, ended_at=1100.0, usage_released=True)
    assert kernel.observe_host(request, foreign) == 'unknown'
    assert not kernel.worker_settled(pending) and counts(kernel) == (1, 0, 0, 0)
    assert kernel.observe_host(request, replace(foreign, invocation_id='original-host')) == 'exited'
    assert kernel.worker_settled(pending) and runner.state().preparing == ()
    assert counts(kernel) == (1, 0, 0, 0)


def test_core_classifies_expired_inherited_execution_deadline_from_its_original_scope(tmp_path: Path) -> None:
    from support.core_operations import prepare_scripted_agent
    kernel, runner, _, first, clock = preparation(tmp_path)
    original = prepare_scripted_agent(kernel, first).call
    assert original is not None and kernel.dispatch_agent(original)
    kernel.bind_thread(original, 'original-backend-thread')
    process = {'pid': 23, 'pgid': 23, 'start_token': 'synthetic-unit-worker',
               'host_pid': 11, 'host_start_token': 'synthetic-unit-host'}
    kernel.agent_started(original, process)
    kernel.agent_source_ready(original)
    kernel.receive(original, b'{"summary":"incomplete artifact structure"}')
    kernel.agent_exited(original, process, 0)
    repair = runner.run_once().preparation
    assert repair is not None and repair.mode == 'output-repair'
    assert repair.preparation_deadline == original.deadline_at
    clock[0] = original.deadline_at
    with pytest.raises(KernelError, match='invocation_deadline_exceeded'):
        kernel.guard_preparation(repair)
    # No preparation resources were opened. The caller reports the common
    # preparation expiry; Core owns the original inherited execution deadline.
    kernel.preparation_failed(repair, 'preparation_deadline_exceeded', {'resources_opened': False})
    assert kernel.status(repair.subject)['gates'] == ['execution_failed']
    assert kernel.status(repair.subject)['budgets']['requirements']['developments_used'] == 1
    assert kernel.worker_settled(repair)
