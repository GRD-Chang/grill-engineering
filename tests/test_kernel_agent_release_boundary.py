"""Local gated processes and owned final files, not sandbox or live Codex E2E."""
from pathlib import Path
import os
import sys
import tempfile

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Policy, Role
from agent_run.runtime.codex import _CodexCapture, _execute_prepared, _write_member
from support.core_operations import prepare_scripted_agent
from support.offline import Scenario
from test_kernel_agent_preparation import preparation


class LocalFileCapture(_CodexCapture):
    """The execution/file seam only; no credential or sandbox claim is made."""
    def __enter__(self):
        try:
            temporary = Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix='release-boundary-')))
            self.directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            self.resources.callback(os.close, self.directory)
            self._preparation_guard()
            _write_member(self.directory, 'last-message.txt', b'')
            final = temporary / 'last-message.txt'
            self.marker = self.cwd / 'actual-target-started'
            self.arguments = [sys.executable, '-I', '-c',
                f"from pathlib import Path; Path({str(self.marker)!r}).write_text('ran'); "
                f"Path({str(final)!r}).write_text({self.output!r})"]
            self.environment = {'PATH': os.defpath}
            return self
        except BaseException:
            self._close()
            raise

    output = '{"result_kind":"development","summary":"Actual local return","human_blockers":null}'


def local_capture(kernel, request):
    return LocalFileCapture(kernel, request, executable=sys.executable,
        environment={'PATH': os.defpath}, credential_file=None, hidden_paths=(), hidden_environment=())


def next_development(tmp_path):
    scenario = Scenario(tmp_path, DeliveryKind.TICKET, reviews=('fail',), policy=Policy(ticket_rounds=2))
    runner = OperationRunner(scenario.kernel, scenario.execution, scenario.make_ports)
    original = None
    for _ in range(64):
        step = runner.run_once()
        if step.preparation is None:
            continue
        request = step.preparation
        if request.role == Role.DEVELOPMENT and original is not None:
            assert request.thread_id == original.thread_id and request.reported_thread == 'offline-' + original.thread_id
            assert request.attempt_id != original.attempt_id
            return scenario.kernel, runner, request
        call = prepare_scripted_agent(scenario.kernel, request).call
        assert call is not None
        if call.role == Role.DEVELOPMENT:
            original = call
        runner.authorize_agent(call)
        scenario.kernel.receive(call, scenario.agent(call))
    raise AssertionError('Review did not return the original Thread to Development')


def test_stop_after_bootstrap_binding_does_not_turn_precreated_empty_file_into_return(tmp_path):
    kernel, runner, request = next_development(tmp_path)
    before = kernel.status(request.subject)['budgets']['requirements']['developments_used']
    assert before == 1
    capture = local_capture(kernel, request)
    reads = []
    finish = capture.finish
    def observed_finish():
        reads.append(True)
        return finish()
    capture.finish = observed_finish
    checks = []
    def authorize(call):
        checks.append(True)
        if len(checks) == 2:
            kernel.control('stop', 'stop-bound-bootstrap', subject=request.subject)
        runner.authorize_agent(call)
    with pytest.raises(KernelError):
        _execute_prepared(capture, prompt='', timeout=5, authorize_source=authorize)
    assert len(checks) == 2 and not capture.marker.exists()
    assert capture.call is not None and kernel.worker_settled(request)
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT raw_id,exit_code FROM invocation_outputs WHERE invocation=?', (capture.call.invocation_id,))
        assert row['raw_id'] is None and row['exit_code'] is not None
        assert tx.one('SELECT accounted FROM attempts WHERE id=?', (request.attempt_id,))[0] == 0
    assert reads == []
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == before


def test_local_stop_after_admission_settles_only_proven_unspawned_work(tmp_path, monkeypatch):
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    capture = local_capture(kernel, request)
    admit = kernel.admit_agent
    def stop_after_admission(exact):
        admitted = admit(exact)
        kernel.control('stop', 'stop-before-dispatch', subject=exact.subject)
        return admitted
    monkeypatch.setattr(kernel, 'admit_agent', stop_after_admission)
    with pytest.raises(KernelError):
        _execute_prepared(capture, prompt='', timeout=5, authorize_source=runner.authorize_agent)
    assert capture.cleanup_complete and not capture.marker.exists()
    assert kernel.worker_settled(request)
    assert runner.state().active == ()
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0


@pytest.mark.parametrize('output', ['', '{not a valid artifact', LocalFileCapture.output])
def test_released_native_final_is_preserved_even_when_empty_or_invalid(tmp_path, output):
    kernel, runner, request = next_development(tmp_path)
    capture = local_capture(kernel, request)
    capture.output = output
    result = _execute_prepared(capture, prompt='', timeout=5, authorize_source=runner.authorize_agent)
    assert result is not None and result.returncode == 0 and result.raw == output.encode()
    assert capture.marker.read_text() == 'ran' and kernel.worker_settled(request)
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 2
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT raw_id FROM invocation_outputs WHERE invocation=?', (capture.call.invocation_id,))
        assert tx.get(row['raw_id'], request.subject) == output.encode()
    if output != LocalFileCapture.output:
        repair = runner.run_once().preparation
        assert repair is not None and repair.mode == 'output-repair'
        assert repair.attempt_id == request.attempt_id and repair.thread_id == request.thread_id
        assert repair.context['readonly'] is True
        repair_call = prepare_scripted_agent(kernel, repair).call
        assert repair_call.invocation_id == capture.call.invocation_id
        assert repair_call.deadline_at == capture.call.deadline_at and repair_call.output_ordinal == 1
        kernel.agent_unavailable(repair_call, 'test_no_repair_worker_spawned', {'resources_closed': True})
    kernel.control('stop', 'late-original-output', subject=request.subject)
    kernel.receive(capture.call, output.encode())
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 2


@pytest.mark.parametrize('boundary', ['source', 'dispatch-response', 'owner', 'stop', 'interrupt'])
def test_known_pre_spawn_failure_waits_for_cleanup_and_keeps_original_exception(tmp_path, monkeypatch, boundary):
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    capture = local_capture(kernel, request)
    exact = KeyboardInterrupt() if boundary == 'interrupt' else RuntimeError('local pre-spawn failure')
    if boundary == 'dispatch-response':
        dispatch = kernel.dispatch_agent
        def lost_response(call):
            assert dispatch(call)
            raise exact
        monkeypatch.setattr(kernel, 'dispatch_agent', lost_response)
    elif boundary == 'owner':
        admit = kernel.admit_agent
        def revoke(exact_request):
            admitted = admit(exact_request)
            kernel.control('stop', 'stop-original-owner')
            return admitted
        monkeypatch.setattr(kernel, 'admit_agent', revoke)
    def authorize(call):
        assert not kernel.worker_settled(request) and not capture.cleanup_complete
        if boundary == 'stop':
            kernel.control('stop', 'stop-before-spawn', subject=request.subject)
            runner.authorize_agent(call)
        raise exact
    expected = KernelError if boundary in {'owner', 'stop'} else type(exact)
    with pytest.raises(expected) as caught:
        _execute_prepared(capture, prompt='', timeout=5, authorize_source=authorize)
    if boundary not in {'owner', 'stop'}:
        assert caught.value is exact
    assert capture.cleanup_complete and not capture.marker.exists()
    assert kernel.worker_settled(request) and runner.state().active == ()
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT raw_id FROM invocation_outputs WHERE lease=?', (request.reservation_id,))[0] is None
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    kernel.agent_unavailable(capture.call, 'worker_preparation_failed', {'resources_closed': True})
    assert kernel.worker_settled(request) and runner.state().active == ()


@pytest.mark.parametrize('failure', ['cleanup', 'unknown-spawn', 'spawn-oserror'])
def test_failed_cleanup_or_uncertain_spawn_never_becomes_unstarted_proof(tmp_path, monkeypatch, failure):
    from agent_run.runtime.execution import ProcessExitUnknown
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    capture = local_capture(kernel, request)
    if failure == 'cleanup':
        close = capture._close
        def failed_close():
            assert not kernel.worker_settled(request)
            close()
            capture.cleanup_complete = False
            raise OSError('local resource cleanup acknowledgement lost')
        monkeypatch.setattr(capture, '_close', failed_close)
        def authorize(call):
            raise RuntimeError('before spawn')
    else:
        def spawn(*args, **kwargs):
            if failure == 'spawn-oserror':
                raise FileNotFoundError('bootstrap spawn rejected')
            raise RuntimeError('Popen outcome unknown')
        monkeypatch.setattr('agent_run.runtime.execution.subprocess.Popen', spawn)
        authorize = runner.authorize_agent
    if failure == 'spawn-oserror':
        assert _execute_prepared(capture, prompt='', timeout=5, authorize_source=authorize) is None
        assert capture.cleanup_complete and kernel.worker_settled(request)
    else:
        with pytest.raises(ProcessExitUnknown if failure == 'cleanup' else RuntimeError):
            _execute_prepared(capture, prompt='', timeout=5, authorize_source=authorize)
        assert not kernel.worker_settled(request)
        assert request.reservation_id in runner.state().active
        if failure == 'unknown-spawn':
            assert capture.cleanup_complete and capture.unstarted_failure is None
            with pytest.raises(KernelError, match='worker_exit_unproven'):
                kernel.agent_unavailable(capture.call, 'not_a_proof', 'No stored PID is insufficient')
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0


def test_repeated_claim_is_not_proof_that_the_original_invocation_never_spawned(tmp_path):
    from agent_run.runtime.execution import _execute
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    call = prepare_scripted_agent(kernel, request).call
    assert kernel.dispatch_agent(call)
    capture = local_capture(kernel, request)
    capture.bind_call(call)
    assert _execute(kernel, call, arguments=['/bin/true'], cwd=tmp_path, prompt='',
        environment={'PATH': os.defpath}, timeout=5, authorize_source=None, capture=capture) is None
    assert capture.unstarted_failure is None
    assert not kernel.worker_settled(request) and request.reservation_id in runner.state().active


def test_stop_before_dispatch_settles_its_call_while_real_sibling_keeps_running(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import json
    import select
    import time
    from agent_run.model import Issue, ParentIssue, Requirements
    from agent_run.runtime.execution import execute
    from support.offline import GraphScenario
    scenario = GraphScenario(tmp_path / 'graph', Requirements(ParentIssue(1, 'Parent', 'Scope', (2, 3)),
        tuple(Issue(n, f'Ticket {n}', 'Scope', 'OPEN', labels=('ready-for-agent',)) for n in (2, 3))),
        policy=Policy(concurrency=2))
    kernel = scenario.kernel
    runner = OperationRunner(kernel, scenario.execution, scenario.make_ports)
    requests = []
    for _ in range(20):
        step = runner.run_once(agent_capacity=2)
        if step.preparation is not None:
            requests.append(step.preparation)
        if len(requests) == 2:
            break
    assert len(requests) == 2
    target, sibling = requests
    call = prepare_scripted_agent(kernel, sibling).call
    assert call is not None
    started, release = tmp_path / 'sibling-started', tmp_path / 'sibling-release'
    code = (f"from pathlib import Path; import sys,time; Path({str(started)!r}).write_text('running'); "
            f"gate=Path({str(release)!r})\nwhile not gate.exists(): time.sleep(0.01)\n"
            f"sys.stdout.write({LocalFileCapture.output!r})")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, kernel, call, arguments=[sys.executable, '-I', '-c', code],
            cwd=Path(call.context['checkout']), prompt='', environment={'PATH': os.defpath}, timeout=5,
            authorize_source=lambda: runner.authorize_agent(call))
        try:
            deadline = time.monotonic() + 3
            while not started.exists() and not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.read_text() == 'running' and not future.done()
            capture = local_capture(kernel, target)
            admit = kernel.admit_agent
            def stop_target(exact):
                admitted = admit(exact)
                kernel.control('stop', 'stop-only-target-before-dispatch', subject=target.subject)
                return admitted
            monkeypatch.setattr(kernel, 'admit_agent', stop_target)
            with pytest.raises(KernelError):
                _execute_prepared(capture, prompt='', timeout=5, authorize_source=runner.authorize_agent)
            assert kernel.worker_settled(target) and not kernel.worker_settled(sibling)
            assert runner.state().active == (sibling.reservation_id,)
            assert sibling.reservation_id not in runner.state().cancel
            assert kernel.status(target.subject)['budgets']['requirements']['developments_used'] == 0
            assert kernel.status(sibling.subject)['budgets']['requirements']['developments_used'] == 1
            with kernel.storage.transaction(write=False) as tx:
                row = tx.one('SELECT process_identity,exit_code FROM invocation_outputs WHERE invocation=?', (call.invocation_id,))
                assert row['exit_code'] is None
                identity = json.loads(tx.get(row['process_identity'], sibling.subject))
            descriptor = os.pidfd_open(identity['pid'])
            try:
                assert select.select([descriptor], [], [], 0)[0] == []
            finally:
                os.close(descriptor)
            assert not future.done()
        finally:
            release.write_text('finish original sibling')
        result = future.result(timeout=5)
    assert result is not None and result.returncode == 0
    assert kernel.worker_settled(sibling) and runner.state().active == ()


def test_local_execute_without_capture_settles_its_known_pre_dispatch_stop(tmp_path):
    from agent_run.runtime.execution import execute
    kernel, runner, _, step, _ = preparation(tmp_path)
    request = step.preparation
    call = prepare_scripted_agent(kernel, request).call
    kernel.control('stop', 'stop-local-direct-call', subject=request.subject)
    with pytest.raises(KernelError):
        execute(kernel, call, arguments=['/bin/true'], cwd=tmp_path, prompt='',
            environment={'PATH': os.defpath}, timeout=5)
    assert kernel.worker_settled(request) and runner.state().active == ()
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
