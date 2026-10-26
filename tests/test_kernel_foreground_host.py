"""Foreground Host contracts, using current native admission and local facts."""
from dataclasses import replace
import errno
from pathlib import Path

import pytest

from agent_run.adapters import foreground as physical
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from agent_run.runtime.package_lease import runner_management_lease, runner_usage_lease, RunnerLeaseBusy
from agent_run.runtime.foreground_host import ForegroundHost
import agent_run.runtime.foreground_host as foreground_host
from agent_run.cli.lifecycle import attach
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from support.core_operations import CoreOperations, task_run
from support.environment import durable_snapshot


def _domain(nonce):
    observer = {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'}
    common = {'boot_id': 'test-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
    return {'nonce': nonce, 'observer': observer,
            'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
            'executor': {**common, 'pid': 202, 'parent': 200, 'start_token': '203', 'namespace_pids': [202, 2]}}


@pytest.fixture
def inspection(tmp_path):
    kernel, ports = task_run(tmp_path, 'inspection')
    request = kernel.admit_host('launch', kind='run', command=('controlled-executor',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    domain = _domain(request.nonce)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(lock),
                   'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]'}
        running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
            backend='foreground', domain=domain, usage_ready=True, evidence=_encode(receipt).decode())
        assert kernel.observe_host(request, running) == 'running'
        path = physical.record_root(lock, create=True) / (request.nonce + '.json')
        physical.write_record(path, {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
            'receipt': receipt, 'phase': 'running', 'domain': domain,
            'database': str(kernel.storage.path.resolve()), 'action': 'launch'}, create=True)
    return kernel, ports, request, domain, lock, path


def test_readonly_host_inspection_does_not_settle_discovered_exit(inspection, tmp_path, monkeypatch):
    kernel, _, request, domain, lock, path = inspection
    monkeypatch.setattr(foreground_host, 'recover_init', lambda value: 'exited' if value == domain else 'unknown')
    manager = ForegroundHost(environment={})
    before = durable_snapshot(tmp_path)
    observed = manager.inspect(kernel, request, lock)
    assert observed.request == request and observed.source == 'local'
    assert observed.observation.disposition == 'exited'
    assert observed.observation.ended_at is None and observed.observation.exit_status is None
    assert not observed.observation.usage_released
    assert kernel.active_host_action() == 'launch' and path.exists()
    assert physical.read_record(path)['phase'] == 'running'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('change,expected', [('none', 'running'), ('init-unknown', 'unknown'),
    ('executor-exited', 'unknown'), ('pin-failure', 'unknown'), ('domain', 'unknown'), ('receipt', 'unknown')])
def test_readonly_host_inspection_requires_exact_ready_domain(inspection, tmp_path, monkeypatch, change, expected):
    from types import SimpleNamespace
    kernel, _, request, _, lock, path = inspection
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown' if change == 'init-unknown' else 'running')
    closed = []
    def pin(pid, original):
        if change == 'pin-failure':raise physical.DomainUnknown('controlled FD exhaustion')
        assert (pid, original['start_token']) == (202, '203')
        return SimpleNamespace(observe=lambda: 'exited' if change == 'executor-exited' else 'running',
                               close=lambda: closed.append(pid))
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    if change in {'domain', 'receipt'}:
        record = physical.read_record(path)
        if change == 'domain':record['domain']['executor']['start_token'] = 'foreign'
        else:record['receipt']['nonce'] = 'foreign'
        physical.write_record(path, record)
    before = durable_snapshot(tmp_path)
    manager = ForegroundHost(environment={})
    for _ in range(2):
        reading = manager.inspect(kernel, request, lock)
        assert reading.observation.disposition == expected
        assert reading.source == ('local' if expected == 'running' else 'unavailable')
    assert closed == ([202, 202] if change in {'none', 'executor-exited'} else [])
    assert durable_snapshot(tmp_path) == before


@pytest.fixture
def inspection_worker(inspection):
    kernel, ports, request, domain, lock, path = inspection
    operations = CoreOperations(kernel, Execution(request.run_id, request.owner, request.epoch), lambda _: ports)
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, 'inspection-worker')
    process = {'schema': 'linux-namespace-process-v1', 'pid': 3, 'pgid': 3, 'start_token': 'worker',
        'host_pid': 2, 'host_start_token': '203', 'boot_id': 'test-boot',
        'host_namespace': 'pid:[20]', 'pid_namespace': 'pid:[20]'}
    kernel.agent_started(call, process)
    return kernel, request, call, process, domain, lock, path


@pytest.mark.parametrize('change,expected', [('none', 'running'), ('worker-exited', 'exited'),
    ('namespace', 'unknown'), ('pid1', 'unknown'), ('worker-reused', 'unknown'),
    ('owner-reused', 'unknown'), ('owner-exited', 'unknown'), ('worker-pin-failure', 'unknown')])
def test_readonly_worker_inspection_uses_only_its_exact_observable_namespace(
    inspection_worker, tmp_path, monkeypatch, change, expected,
):
    from types import SimpleNamespace
    kernel, request, call, _, domain, lock, _ = inspection_worker
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown')
    anchor = {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]' if change == 'namespace' else 'pid:[20]',
              'pid1_start': 'foreign' if change == 'pid1' else domain['init']['start_token']}
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: anchor)
    opened, closed = [], []
    def pin(pid):
        opened.append(pid)
        if change == 'worker-pin-failure' and pid == 3:raise physical.DomainUnknown('controlled permission failure')
        token = ('reused' if change == 'worker-reused' else 'worker') if pid == 3 else ('reused' if change == 'owner-reused' else '203')
        return SimpleNamespace(identity=SimpleNamespace(start_token=token, boot_id='test-boot', pid_namespace='pid:[20]'),
            observe=lambda: 'exited' if (pid == 3 and change == 'worker-exited' or pid == 2 and change == 'owner-exited') else 'running',
            close=lambda: closed.append(pid))
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    before = durable_snapshot(tmp_path)
    kernel.clock = lambda: pytest.fail('Readonly inspection must not use a query/settlement clock')
    reading = ForegroundHost(environment={}).inspect_worker(kernel, request, call, lock)
    assert reading.execution == call.execution and reading.invocation_id == call.invocation_id
    assert reading.output_ordinal == call.output_ordinal and reading.disposition == expected
    assert reading.source == ('local' if expected != 'unknown' else 'unavailable')
    assert sorted(closed) == sorted(pid for pid in opened if not (change == 'worker-pin-failure' and pid == 3))
    if change in {'namespace', 'pid1'}:assert opened == []
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('mode', ['host-exit', 'recorded-worker-exit', 'raw-only', 'foreign-run', 'foreign-epoch', 'foreign-owner', 'foreign-nonce', 'foreign-context'])
def test_readonly_worker_exit_and_foreign_binding_are_not_execution_grants(inspection_worker, tmp_path, monkeypatch, mode):
    kernel, request, call, process, domain, lock, path = inspection_worker
    if mode == 'recorded-worker-exit':kernel.agent_exited(call, process, 23)
    if mode == 'raw-only':kernel.receive(call, b'original raw does not prove exit')
    if mode == 'foreign-run':call = replace(call, execution=replace(call.execution, run_id='foreign'))
    if mode == 'foreign-epoch':call = replace(call, execution=replace(call.execution, epoch=call.execution.epoch + 1))
    if mode == 'foreign-owner':call = replace(call, execution=replace(call.execution, owner='foreign'))
    if mode == 'foreign-nonce':request = replace(request, nonce='foreign')
    if mode == 'foreign-context':call = replace(call, context={'foreign': True})
    monkeypatch.setattr(foreground_host, 'recover_init', lambda value: 'exited' if mode != 'raw-only' and value == domain else 'unknown')
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: domain['observer'])
    def forbidden(*a, **kw):pytest.fail('No process signal, physical launch or settlement is allowed')
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', forbidden)
    monkeypatch.setattr(foreground_host, '_settle', forbidden)
    monkeypatch.setattr(foreground_host, '_remove_record', forbidden)
    before = durable_snapshot(tmp_path)
    manager = ForegroundHost(environment={})
    if mode.startswith('foreign'):
        with pytest.raises(KernelError):manager.inspect_worker(kernel, request, call, lock)
    else:
        reading = manager.inspect_worker(kernel, request, call, lock)
        assert reading.disposition == ('unknown' if mode == 'raw-only' else 'exited')
        assert reading.source == ('host_exit' if mode == 'host-exit' else 'unavailable' if mode == 'raw-only' else 'recorded_exit')
        assert kernel.active_host_action() == 'launch' and path.exists()
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT count(*) FROM leases')[0] == int(mode != 'recorded-worker-exit')
    assert durable_snapshot(tmp_path) == before


def test_readonly_committed_host_exit_is_recorded_not_a_fresh_process_probe(inspection, tmp_path, monkeypatch):
    kernel, _, request, _, lock, path = inspection
    original = kernel.host_usage('launch')
    exited = replace(original, disposition='exited', ended_at=kernel.clock(), exit_status=None,
        usage_ready=False, usage_released=True, exit_evidence='same_observer_init_exit')
    assert kernel.observe_host(request, exited) == 'exited'
    def forbidden(*a, **kw):pytest.fail('A recorded exact exit needs no process probe or cleanup')
    monkeypatch.setattr(foreground_host, 'recover_init', forbidden)
    monkeypatch.setattr(foreground_host, '_remove_record', forbidden)
    before = durable_snapshot(tmp_path)
    for _ in range(2):
        observed = ForegroundHost(environment={}).inspect(kernel, request, lock)
        assert observed.request == request and observed.source == 'recorded_exit'
        assert observed.observation == exited and path.exists()
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('change', ['raw', 'epoch'])
def test_readonly_worker_inspection_downgrades_a_racing_original_binding(inspection_worker, monkeypatch, change):
    from types import SimpleNamespace
    kernel, request, call, _, domain, lock, _ = inspection_worker
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown')
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: {
        'boot_id': 'test-boot', 'pid_namespace': 'pid:[20]', 'pid1_start': domain['init']['start_token']})
    closed = []
    def pin(pid):
        def observe():
            if pid == 3:
                if change == 'raw':kernel.receive(call, b'original concurrent callback')
                else:assert kernel.stop_task('concurrent-stop') == 'applied'
            return 'running'
        return SimpleNamespace(identity=SimpleNamespace(start_token='203' if pid == 2 else 'worker',
            boot_id='test-boot', pid_namespace='pid:[20]'), observe=observe, close=lambda: closed.append(pid))
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    observed = ForegroundHost(environment={}).inspect_worker(kernel, request, call, lock)
    assert observed.disposition == 'unknown' and observed.source == 'unavailable'
    assert sorted(closed) == [2, 3]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM leases')[0] == 1
        if change == 'epoch':assert tx.one('SELECT stopped FROM runs')[0]
        else:
            material = tx.one('SELECT raw_id FROM invocation_outputs WHERE invocation=?', (call.invocation_id,))[0]
            assert tx.get(material, call.subject) == b'original concurrent callback'


def test_readonly_host_inspection_downgrades_epoch_change_during_probe(inspection, monkeypatch):
    from types import SimpleNamespace
    kernel, _, request, _, lock, _ = inspection
    reads = []
    def init(_):
        reads.append(True)
        if len(reads) == 2:assert kernel.stop_task('concurrent-stop') == 'applied'
        return 'running'
    monkeypatch.setattr(foreground_host, 'recover_init', init)
    closed = []
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', lambda *a: SimpleNamespace(
        observe=lambda: 'running', close=lambda: closed.append(True)))
    observed = ForegroundHost(environment={}).inspect(kernel, request, lock)
    assert observed.observation.disposition == 'unknown' and observed.source == 'unavailable'
    assert closed == [True]


def test_copied_database_cannot_borrow_the_original_local_process_observation(inspection_worker, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import shutil
    kernel, request, call, _, domain, lock, _ = inspection_worker
    copied = tmp_path / 'copied.sqlite3'
    shutil.copyfile(kernel.storage.path, copied)
    snapshot = Kernel(copied)
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'running')
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: {
        'boot_id': 'test-boot', 'pid_namespace': 'pid:[20]', 'pid1_start': domain['init']['start_token']})
    def pin(pid, expected=None):
        return SimpleNamespace(identity=SimpleNamespace(start_token='203' if pid in {2, 202} else 'worker',
            boot_id='test-boot', pid_namespace='pid:[20]'), observe=lambda: 'running', close=lambda: None)
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    before = durable_snapshot(tmp_path)
    manager = ForegroundHost(environment={})
    assert manager.inspect(snapshot, request, lock).observation.disposition == 'unknown'
    assert manager.inspect_worker(snapshot, request, call, lock).disposition == 'unknown'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('damage', [None, 'invalid-phase', 'invalid-exit'])
def test_worker_inspection_cannot_bypass_invalid_protection_record(inspection_worker, tmp_path, monkeypatch, damage):
    from types import SimpleNamespace
    kernel, request, call, _, domain, lock, path = inspection_worker
    record = physical.read_record(path)
    if damage == 'invalid-phase':record['phase'] = 'invalid'
    elif damage == 'invalid-exit':record.update(phase='exited', exit_basis='unproven')
    physical.write_record(path, record)
    # The Host's original outer observer cannot be used here. A valid record
    # still permits independent exact Worker observation inside its namespace.
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown')
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: {
        'boot_id': 'test-boot', 'pid_namespace': 'pid:[20]', 'pid1_start': domain['init']['start_token']})
    opened, closed = [], []
    def pin(pid):
        opened.append(pid)
        return SimpleNamespace(identity=SimpleNamespace(start_token='203' if pid == 2 else 'worker',
            boot_id='test-boot', pid_namespace='pid:[20]'), observe=lambda: 'running', close=lambda: closed.append(pid))
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    before = durable_snapshot(tmp_path)
    manager = ForegroundHost(environment={})
    host = manager.inspect(kernel, request, lock)
    assert host.observation.disposition == 'unknown' and host.source == 'unavailable'
    worker = manager.inspect_worker(kernel, request, call, lock)
    assert worker.disposition == ('unknown' if damage else 'running')
    assert worker.source == ('unavailable' if damage else 'local')
    assert opened == closed[::-1] == ([] if damage else [2, 3])
    assert durable_snapshot(tmp_path) == before


def test_foreground_admission_is_frozen_stop_fences_and_null_exit_does_not_become_zero(tmp_path):
    kernel = task_run(tmp_path, 'foreground')[0]
    args = dict(kind='run', command=('/usr/bin/python3',), environment_carrier=tmp_path / 'carrier',
                preflight=lambda: None, backend='foreground')
    request = kernel.admit_host('launch', **args)
    assert request.unit_name is None and request.backend == 'foreground'
    assert kernel.admit_host('launch', **args) == request
    with pytest.raises(KernelError, match='action_identity_changed'):
        kernel.admit_host('launch', **{**args, 'backend': 'systemd'})
    assert kernel.dispatch_host(request)
    assert not kernel.dispatch_host(request)
    domain = _domain(request.nonce)
    running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
                              backend='foreground', domain=domain, usage_ready=True)
    assert kernel.observe_host(request, running) == 'running'
    kernel.authorize_host(request)
    assert kernel.stop_task('stop') == 'applied'
    assert kernel.stop_task('stop') == 'applied'
    with pytest.raises(KernelError):kernel.authorize_host(request)
    with pytest.raises(KernelError, match='executor_exit_unproven'):
        kernel.admit_host('resume-too-early', **{**args, 'kind': 'resume'})
    exited = replace(running, disposition='exited', exit_status=None, ended_at=kernel.clock(),
                     usage_ready=False, usage_released=True, exit_evidence='same_observer_init_exit')
    assert kernel.observe_host(request, exited) == 'exited'
    assert Kernel(kernel.storage.path).host_usage('launch').exit_status is None
    resumed = kernel.admit_host('resume', **{**args, 'kind': 'resume'})
    assert resumed.owner != request.owner and resumed.epoch > request.epoch
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM attempts')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
        assert tx.one("SELECT count(*) FROM actions WHERE id='stop'")[0] == 1


@pytest.mark.parametrize('known_worker_exit', [False, True], ids=['discovered-domain-exit', 'exact-worker-exit'])
def test_discovered_domain_exit_is_not_an_exact_worker_end_time(tmp_path, monkeypatch, known_worker_exit):
    """Controlled physical-read seam; no Host/process is actually launched."""
    from io import StringIO
    import json
    from agent_run.cli.render import render
    from agent_run.queries.run import read

    kernel, ports = task_run(tmp_path, 'discovered-exit')
    clock = [1000.]
    kernel.clock = lambda: clock[0]
    request = kernel.admit_host('launch', kind='run', command=('controlled-executor',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    domain = _domain(request.nonce)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(lock),
                   'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]'}
        running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
            backend='foreground', domain=domain, usage_ready=True, evidence=_encode(receipt).decode())
        assert kernel.observe_host(request, running) == 'running'
        operations = CoreOperations(kernel, Execution(request.run_id, request.owner, request.epoch), lambda _: ports)
        call = operations.until(lambda step: step.call is not None).call
        assert call is not None and kernel.dispatch_agent(call)
        kernel.bind_thread(call, 'original-backend-thread')
        process = {'schema': 'linux-namespace-process-v1', 'pid': 3, 'pgid': 3, 'start_token': 'worker',
            'host_pid': 2, 'host_start_token': '203', 'boot_id': 'test-boot',
            'host_namespace': 'pid:[20]', 'pid_namespace': 'pid:[20]'}
        clock[0] = 1001.
        kernel.agent_started(call, process)
        kernel.agent_source_ready(call)
        kernel.observe_worker_activity(call, 'active', active=True, observed_at=1001., evidence={'turn': 'started'})
        clock[0] = 1002.
        kernel.observe_worker_activity(call, 'inactive', active=False, observed_at=1002., evidence={'turn': 'completed'})
        raw = b'{"result_kind":"development","summary":"original result","human_blockers":null}'
        kernel.receive(call, raw)
        if known_worker_exit:
            clock[0] = 1003.
            kernel.agent_exited(call, process, 0)
        path = physical.record_root(lock, create=True) / (request.nonce + '.json')
        physical.write_record(path, {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
            'receipt': receipt, 'phase': 'running', 'domain': domain,
            'database': str(kernel.storage.path.resolve()), 'action': 'launch'}, create=True)
        monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown')
        before_unknown = durable_snapshot(tmp_path)
        assert ForegroundHost(environment={}).observe(kernel, request, lock).disposition == 'unknown'
        assert kernel.active_host_action() == 'launch' and path.exists()
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT count(*) FROM leases')[0] == int(not known_worker_exit)
        assert durable_snapshot(tmp_path) == before_unknown
        monkeypatch.setattr(foreground_host, 'recover_init', lambda value: 'exited' if value == domain else 'unknown')
        clock[0] = 2000.  # Only now does a fresh observer discover the old domain exit.
        observed = ForegroundHost(environment={}).observe(kernel, request, lock)
        assert observed.disposition == 'exited' and observed.usage_released
        assert observed.ended_at == 2000. and observed.exit_status is None
        assert observed.exit_evidence == 'same_observer_init_exit'
        assert kernel.active_host_action() is None and not path.exists()

    before = durable_snapshot(tmp_path)
    from types import SimpleNamespace
    import agent_run.queries.run as query_run
    monkeypatch.setattr(query_run, 'time', SimpleNamespace(time=lambda: clock[0]))
    for kind in ('status', 'history'):
        clock[0] += 1000.
        document = read(kernel.storage.path, kind)
        job, = document.data['subjects']
        invocation, = job['pending_attempt']['invocations']
        output, = invocation['outputs']
        assert output['dispatch_state'] == 'exited'
        assert output['raw_return'] == json.loads(raw)
        assert output['exit_code'] == (0 if known_worker_exit else None)
        assert output['exited_at'] == (1003. if known_worker_exit else None)
        assert output['process_span_seconds'] == (2. if known_worker_exit else None)
        assert output['recorded_execution_seconds'] == 1.
        assert invocation['live_status'] == document.data['executor_control']['live_status'] == 'unknown'
        for language, label in [('en', 'Recorded Worker exit; exact end time unknown'),
                                ('zh', '已记录 Worker 退出；准确结束时间未知')]:
            for style in ('plain', 'rich'):
                target = StringIO()
                render(replace(document, language=language), style=style, details=False, stream=target)
                value = target.getvalue()
                if kind == 'status':
                    assert (label in value) is not known_worker_exit
                else:
                    assert ('Time range:' if language == 'en' else '时间区间：') in value
                    assert (('→ Not recorded' if language == 'en' else '→ 未记录') in value) is not known_worker_exit
    assert durable_snapshot(tmp_path) == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
        assert tx.one('SELECT developments_used FROM budgets WHERE subject=? AND purpose=? AND current=1',
                      (call.subject, call.purpose))[0] == 1
        assert tx.one('SELECT backend_id FROM threads WHERE id=?', (call.thread_id,))[0] == 'original-backend-thread'
        raw_id = tx.one('SELECT raw_id FROM invocation_outputs WHERE invocation=?', (call.invocation_id,))[0]
        assert tx.get(raw_id, call.subject) == raw


def test_foreground_cannot_use_systemd_or_a_changed_domain_as_exit_evidence(tmp_path):
    kernel = task_run(tmp_path, 'foreground')[0]
    request = kernel.admit_host('launch', kind='run', command=('/usr/bin/python3',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    with pytest.raises(KernelError, match='host_backend_changed'):
        kernel.observe_host(request, HostObservation('exited', None, exit_status=0, ended_at=kernel.clock()))
    original = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
        backend='foreground', domain=_domain(request.nonce), usage_ready=True)
    assert kernel.observe_host(request, original) == 'running'
    foreign = _domain(request.nonce)
    foreign['executor']['start_token'] = 'replacement'
    with pytest.raises(KernelError, match='host_domain_changed'):
        kernel.observe_host(request, replace(original, domain=foreign, disposition='exited', ended_at=kernel.clock()))
    assert kernel.active_host_action() == 'launch'
    assert not kernel.dispatch_host(request)


def test_unclosed_record_blocks_management_even_after_all_shared_descriptors_close(tmp_path):
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        path = physical.record_root(lock, create=True) / 'original.json'
        physical.write_record(path, {'phase': 'pending', 'domain': None}, create=True)
    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(lock):pytest.fail('Unknown physical exit admitted management')
    assert physical.read_record(path) == {'phase': 'pending', 'domain': None}


def test_fresh_observer_consumes_persisted_exact_exit_without_reopening_reaped_pid(tmp_path, monkeypatch):
    kernel = task_run(tmp_path, 'foreground')[0]
    request = kernel.admit_host('launch', kind='run', command=('/usr/bin/python3',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    domain = _domain(request.nonce)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(lock),
                   'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]'}
        running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
            backend='foreground', domain=domain, usage_ready=True, evidence=_encode(receipt).decode())
        assert kernel.observe_host(request, running) == 'running'
        path = physical.record_root(lock, create=True) / (request.nonce + '.json')
        physical.write_record(path, {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
            'receipt': receipt, 'phase': 'exited', 'domain': domain, 'executor_exit_code': 7,
            'namespace_exit_code': -9, 'ended_at': kernel.clock(), 'exit_basis': 'retained_namespace_pidfd',
            'database': str(kernel.storage.path.resolve()), 'action': 'launch'}, create=True)
        def forbidden(*args, **kwargs):pytest.fail('Persisted exit must not depend on reopening a reaped PID')
        monkeypatch.setattr(physical.os, 'pidfd_open', forbidden)
        manager = ForegroundHost(environment={})
        original_observe = kernel.observe_host
        def unavailable(*args, **kwargs):raise OSError('Controlled storage outage during exact exit settlement')
        monkeypatch.setattr(kernel, 'observe_host', unavailable)
        assert manager.observe(kernel, request, lock).disposition == 'unknown'
        assert physical.read_record(path)['phase'] == 'exited'
        assert kernel.active_host_action() == 'launch'
        monkeypatch.setattr(kernel, 'observe_host', original_observe)
        remove = foreground_host._remove_record
        monkeypatch.setattr(foreground_host, '_remove_record', unavailable)
        assert manager.observe(kernel, request, lock).disposition == 'unknown'
        assert kernel.active_host_action() is None and path.exists()
        monkeypatch.setattr(foreground_host, '_remove_record', remove)
        manager.reconcile_closed(Kernel(kernel.storage.path), lock)
        observed = manager.observe(kernel, request, lock)
        assert observed.disposition == 'exited' and observed.exit_status == 7 and observed.usage_released
        assert observed.namespace_exit_status == -9 and observed.exit_evidence == 'retained_namespace_pidfd'
        assert not path.exists()
        assert manager.observe(Kernel(kernel.storage.path), request, lock) == observed
    with runner_management_lease(lock):pass


def test_unknown_start_keeps_pre_spawn_protection_and_attach_never_launches_again(tmp_path, monkeypatch):
    kernel = task_run(tmp_path, 'foreground')[0]
    request = kernel.admit_host('launch', kind='run', command=('/usr/bin/python3',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    lock = tmp_path / 'install.lock'
    calls = []
    manager = ForegroundHost(environment={})
    monkeypatch.setattr(manager, 'preflight', lambda: None)
    def lost_response(*args, **kwargs):
        record = physical.read_record(physical.record_root(lock) / (request.nonce + '.json'))
        assert record['phase'] == 'pending' and record['domain'] is None
        assert kernel.host_dispatched('launch')
        calls.append('possible physical launch')
        raise physical.DomainUnknown('Controlled unknown launch response')
    monkeypatch.setattr(foreground_host, 'NamespaceProcess', lost_response)
    with runner_usage_lease(lock) as lease:
        with pytest.raises(physical.DomainUnknown, match='unknown launch response'):
            manager.launch(kernel, request, lock=lock, lease=lease)
    for _ in range(2):
        result = attach(kernel, requested_action='resume', manager=manager, environment={}, runner_lock=lock)
        assert result['result'] == 'attached' and result['host'] == 'unknown'
        assert result['requested_action_applied'] is False
    assert calls == ['possible physical launch']
    assert not kernel.dispatch_host(request)
    with pytest.raises(RunnerLeaseBusy, match='Foreground Host'):
        with runner_management_lease(lock):pytest.fail('Unknown launch allowed management')


@pytest.mark.parametrize('error,expected', [(errno.ESRCH, 'exited'), (errno.EMFILE, 'unknown'), (errno.EPERM, 'unknown')])
def test_fresh_observer_classifies_only_exact_same_domain_esrch(monkeypatch, error, expected):
    domain = _domain('nonce')
    monkeypatch.setattr(physical, 'observer_anchor', lambda: domain['observer'])
    def unavailable(pid):
        assert pid == domain['init']['pid']
        raise OSError(error, 'controlled local process observation')
    monkeypatch.setattr(physical.os, 'pidfd_open', unavailable)
    assert physical.recover_init(domain) == expected
    monkeypatch.setattr(physical, 'observer_anchor', lambda: {**domain['observer'], 'pid1_start': 'other'})
    assert physical.recover_init(domain) == 'unknown'


@pytest.mark.parametrize('reused', [False, True], ids=['exited-pidfd', 'reused-pid'])
def test_fresh_observer_does_not_require_zombie_namespace_or_signal_reused_pid(monkeypatch, reused):
    import os
    domain = _domain('nonce')
    read, write = os.pipe()
    try:
        if not reused:os.write(write, b'exit-ready')
        monkeypatch.setattr(physical, 'observer_anchor', lambda: domain['observer'])
        monkeypatch.setattr(physical.os, 'pidfd_open', lambda pid: os.dup(read))
        monkeypatch.setattr(physical, '_stat', lambda pid: ('Z', 1, 'new-process' if reused else '201'))
        def forbidden(*args):pytest.fail('Do not reopen a dead namespace or signal a reused PID')
        monkeypatch.setattr(physical.os, 'readlink', forbidden)
        monkeypatch.setattr(physical.signal, 'pidfd_send_signal', forbidden)
        assert physical.recover_init(domain) == 'exited'
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('boundary,proven', [('before-hello', True), ('before-hello', False), ('after-hello', True)])
def test_launch_failure_settles_only_retained_init_exit(tmp_path, monkeypatch, boundary, proven):
    """Real Core admission with a controlled physical adapter, not Linux proof."""
    import json
    import os
    import socket
    from types import SimpleNamespace
    kernel = task_run(tmp_path, 'foreground')[0]
    request = kernel.admit_host('launch', kind='run', command=('/usr/bin/python3',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    facts = _domain(request.nonce)
    manager = ForegroundHost(environment={})
    monkeypatch.setattr(manager, 'preflight', lambda: None)
    monkeypatch.setattr(foreground_host, 'observer_anchor', lambda: facts['observer'])
    monkeypatch.setattr(foreground_host, 'recover_init', lambda domain: 'unknown')
    events = []

    class Pin:
        def __init__(self, key):
            self.identity = SimpleNamespace(wire=lambda: facts[key],
                namespace_pids=facts[key]['namespace_pids'], pid_namespace=facts[key]['pid_namespace'])
        def close(self):events.append('executor pin closed')
        def observe(self):raise KeyboardInterrupt('controlled after-hello interruption')

    class Domain:
        def __init__(self, command, environment, *, pass_fds):
            self.init = Pin('init')
            self.monitor_pin = Pin('init')
            self.process = SimpleNamespace(stderr=object())
            self.channel = socket.socket(fileno=os.dup(pass_fds[0]))
            self.result = None
        def release(self):
            if boundary == 'before-hello':self.channel.close()
            else:self.channel.sendall(json.dumps({'request_digest': _digest(request), 'pid': 2,
                                                 'namespace': facts['executor']['pid_namespace']}).encode())
        def close(self):
            self.channel.close()
            events.append('physical close')
            if not proven:raise physical.DomainUnknown('controlled unproven init exit')
            self.result = {'namespace_exited': True, 'namespace_exit_code': -9,
                           'executor_exit_code': None, 'monitor_exit_code': None}

    monkeypatch.setattr(foreground_host, 'NamespaceProcess', Domain)
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', lambda pid: Pin('executor'))
    authorize = kernel.authorize_host
    def check(request):
        authorize(request)
        events.append('final Core authorization')
    monkeypatch.setattr(kernel, 'authorize_host', check)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock) as lease:
        error = KeyboardInterrupt if boundary == 'after-hello' else physical.DomainUnknown
        with pytest.raises(error):manager.launch(kernel, request, lock=lock, lease=lease)
    assert events.count('physical close') == 1
    assert ('final Core authorization' in events) == (boundary == 'after-hello')
    receipt = kernel.host_usage('launch')
    path = physical.record_root(lock) / (request.nonce + '.json')
    if proven:
        assert receipt.disposition == 'exited' and receipt.usage_released
        assert receipt.exit_status is None and receipt.namespace_exit_status == -9
        assert (receipt.domain['executor'] is None) == (boundary == 'before-hello')
        assert kernel.active_host_action() is None and not path.exists()
        with runner_management_lease(lock):pass
    else:
        assert kernel.active_host_action() == 'launch' and path.exists()
        assert manager.observe(kernel, request, lock).disposition == 'unknown'
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):pytest.fail('Unproven init released management')
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM attempts')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
    assert not kernel.dispatch_host(request)


@pytest.mark.parametrize('failure', ['none', 'wait', 'reap'])
def test_namespace_close_retains_proof_independently_of_reap_failure(failure):
    from types import SimpleNamespace
    domain = physical.NamespaceProcess.__new__(physical.NamespaceProcess)
    domain.result = None
    domain.gate = domain.read_fd = -1
    domain.process = domain.monitor_pin = None
    domain.status = SimpleNamespace(exit_code=None)
    class Pin:
        descriptor = 5
        def kill(self):pass
        def observe(self):
            if failure == 'wait':raise physical.DomainUnknown('init exit unreadable')
            return 'exited'
        def wait_exit(self, seconds):
            if failure == 'wait':raise physical.DomainUnknown('init exit unreadable')
        def reap(self):
            if failure == 'reap':raise OSError('controlled wait failure after exit proof')
            return -9
        def close(self):self.descriptor = -1
    domain.init = Pin()
    if failure == 'none':domain.close()
    else:
        with pytest.raises(physical.DomainUnknown):domain.close()
    assert domain.init.descriptor == -1
    if failure == 'wait':assert domain.result is None
    else:
        assert domain.result['namespace_exited'] is True
        assert domain.result['namespace_exit_code'] == (-9 if failure == 'none' else None)
        assert domain.result['executor_exit_code'] is None
