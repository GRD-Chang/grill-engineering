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
from agent_run.runtime.inspection import ForegroundInspector
from agent_run.storage.inspection import capture as capture_inspection
import agent_run.runtime.inspection as readonly
import agent_run.runtime.foreground_host as foreground_host
from agent_run.runtime.host_control import attach
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from support.core_operations import CoreOperations, task_run
from support.environment import durable_snapshot


class InspectionProbe:
    """Test the read-only composition's capture / IO / identity check contract."""
    def _capture(self, kernel, request):
        with kernel.storage.transaction(write=False) as tx:
            return capture_inspection(tx, kernel.storage.path, request=request)

    def inspect(self, kernel, request, lock):
        from agent_run.runtime.foreground_host import _unavailable
        binding = self._capture(kernel, request)
        result = ForegroundInspector().inspect(binding)
        return result if self._capture(kernel, request) == binding else _unavailable(request)

    def inspect_worker(self, kernel, request, call, lock):
        from agent_run.runtime.foreground_host import WorkerInspection
        binding = self._capture(kernel, request)
        worker, = (item for item in binding.workers if item.invocation_id == call.invocation_id)
        result = ForegroundInspector().inspect_worker(binding, replace(worker, execution=call.execution))
        if self._capture(kernel, request) != binding:
            return WorkerInspection(call.execution, call.invocation_id, call.output_ordinal, 'unknown', 'unavailable')
        return result


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
    manager = InspectionProbe()
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
    manager = InspectionProbe()
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
    monkeypatch.setattr(readonly, 'observer_anchor', lambda: anchor)
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
    reading = InspectionProbe().inspect_worker(kernel, request, call, lock)
    assert reading.execution == call.execution and reading.invocation_id == call.invocation_id
    assert reading.output_ordinal == call.output_ordinal and reading.disposition == expected
    assert reading.source == ('local' if expected != 'unknown' else 'unavailable')
    assert sorted(closed) == sorted(pid for pid in opened if not (change == 'worker-pin-failure' and pid == 3))
    if change in {'namespace', 'pid1'}:assert opened == []
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('mode', ['host-exit', 'recorded-worker-exit', 'raw-only', 'foreign-run', 'foreign-epoch', 'foreign-owner', 'foreign-nonce'])
def test_readonly_worker_exit_and_foreign_binding_are_not_execution_grants(inspection_worker, tmp_path, monkeypatch, mode):
    kernel, request, call, process, domain, lock, path = inspection_worker
    if mode == 'recorded-worker-exit':kernel.agent_exited(call, process, 23)
    if mode == 'raw-only':kernel.receive(call, b'original raw does not prove exit')
    if mode == 'foreign-run':call = replace(call, execution=replace(call.execution, run_id='foreign'))
    if mode == 'foreign-epoch':call = replace(call, execution=replace(call.execution, epoch=call.execution.epoch + 1))
    if mode == 'foreign-owner':call = replace(call, execution=replace(call.execution, owner='foreign'))
    if mode == 'foreign-nonce':request = replace(request, nonce='foreign')
    monkeypatch.setattr(foreground_host, 'recover_init', lambda value: 'exited' if mode != 'raw-only' and value == domain else 'unknown')
    monkeypatch.setattr(readonly, 'observer_anchor', lambda: domain['observer'])
    def forbidden(*a, **kw):pytest.fail('No process signal, physical launch or settlement is allowed')
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', forbidden)
    monkeypatch.setattr(foreground_host, '_settle', forbidden)
    monkeypatch.setattr(foreground_host, '_remove_record', forbidden)
    before = durable_snapshot(tmp_path)
    manager = InspectionProbe()
    if mode.startswith('foreign'):
        with pytest.raises(KernelError):manager.inspect_worker(kernel, request, call, lock)
    else:
        reading = manager.inspect_worker(kernel, request, call, lock)
        assert reading.disposition == ('unknown' if mode == 'raw-only' else 'exited')
        assert reading.source == ('host_exit' if mode == 'host-exit' else 'unavailable' if mode == 'raw-only' else 'recorded_exit')
        assert kernel.active_host_action() == 'launch' and path.exists()
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == int(mode != 'recorded-worker-exit')
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
        observed = InspectionProbe().inspect(kernel, request, lock)
        assert observed.request == request and observed.source == 'recorded_exit'
        assert observed.observation == exited and path.exists()
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('change', ['raw', 'epoch'])
def test_readonly_worker_inspection_downgrades_a_racing_original_binding(inspection_worker, monkeypatch, change):
    from types import SimpleNamespace
    kernel, request, call, _, domain, lock, _ = inspection_worker
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'unknown')
    monkeypatch.setattr(readonly, 'observer_anchor', lambda: {
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
    observed = InspectionProbe().inspect_worker(kernel, request, call, lock)
    assert observed.disposition == 'unknown' and observed.source == 'unavailable'
    assert sorted(closed) == [2, 3]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 1
        if change == 'epoch':assert tx.one('SELECT stopped FROM runs WHERE id=?', (kernel.storage.run_id,))[0]
        else:
            material = tx.one('SELECT raw_id FROM invocation_outputs WHERE run_id=? AND invocation=?', (kernel.storage.run_id, call.invocation_id))[0]
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
    observed = InspectionProbe().inspect(kernel, request, lock)
    assert observed.observation.disposition == 'unknown' and observed.source == 'unavailable'
    assert closed == [True]


def test_copied_database_cannot_borrow_the_original_local_process_observation(inspection_worker, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import shutil
    kernel, request, call, _, domain, lock, _ = inspection_worker
    copied = tmp_path / 'copied.sqlite3'
    shutil.copyfile(kernel.storage.path, copied)
    snapshot = Kernel(copied, run_id=kernel.storage.run_id)
    monkeypatch.setattr(foreground_host, 'recover_init', lambda _: 'running')
    monkeypatch.setattr(readonly, 'observer_anchor', lambda: {
        'boot_id': 'test-boot', 'pid_namespace': 'pid:[20]', 'pid1_start': domain['init']['start_token']})
    def pin(pid, expected=None):
        return SimpleNamespace(identity=SimpleNamespace(start_token='203' if pid in {2, 202} else 'worker',
            boot_id='test-boot', pid_namespace='pid:[20]'), observe=lambda: 'running', close=lambda: None)
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    before = durable_snapshot(tmp_path)
    manager = InspectionProbe()
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
    monkeypatch.setattr(readonly, 'observer_anchor', lambda: {
        'boot_id': 'test-boot', 'pid_namespace': 'pid:[20]', 'pid1_start': domain['init']['start_token']})
    opened, closed = [], []
    def pin(pid):
        opened.append(pid)
        return SimpleNamespace(identity=SimpleNamespace(start_token='203' if pid == 2 else 'worker',
            boot_id='test-boot', pid_namespace='pid:[20]'), observe=lambda: 'running', close=lambda: closed.append(pid))
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', pin)
    before = durable_snapshot(tmp_path)
    manager = InspectionProbe()
    host = manager.inspect(kernel, request, lock)
    assert host.observation.disposition == 'unknown' and host.source == 'unavailable'
    worker = manager.inspect_worker(kernel, request, call, lock)
    assert worker.disposition == ('unknown' if damage else 'running')
    assert worker.source == ('unavailable' if damage else 'local')
    assert opened == closed[::-1] == ([] if damage else [2, 3])
    assert durable_snapshot(tmp_path) == before


def test_public_status_assembles_the_original_readonly_current_observation(inspection_worker, tmp_path, monkeypatch, capsys):
    import json
    from agent_run.cli.main import main
    kernel, request, call, _, _, _, _ = inspection_worker
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw:
        HostInspection(binding.request, kernel.host_usage('launch'), 'local'))
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', lambda self, binding, c, **kw:
        WorkerInspection(c.execution, c.invocation_id, c.output_ordinal, 'running', 'local'))
    before = durable_snapshot(tmp_path)
    assert main(['status', request.run_id, '--state-dir', str(kernel.storage.path), '--json']) == 0
    document = json.loads(capsys.readouterr().out)
    current = document['current_observation']
    assert current['scope'] == {'run_id': request.run_id, 'owner': request.owner,
                                'epoch': request.epoch, 'nonce': request.nonce}
    assert current['host'] == {'disposition': 'running', 'source': 'local'}
    worker, = current['workers']
    assert worker['invocation_id'] == call.invocation_id and worker['output_ordinal'] == call.output_ordinal
    assert worker['disposition'] == 'running' and worker['source'] == 'local'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('worker_receipt', [False, True], ids=['domain-only', 'recorded-worker-exit'])
def test_public_domain_exit_observation_is_not_a_recorded_worker_exit(inspection_worker, tmp_path, monkeypatch, worker_receipt):
    from io import StringIO
    from agent_run.cli.render import render
    from agent_run.queries.run import read
    kernel, request, call, process, domain, _, _ = inspection_worker
    if worker_receipt:kernel.agent_exited(call, process, 0)
    monkeypatch.setattr(foreground_host, 'recover_init', lambda value: 'exited' if value == domain else 'unknown')
    before = durable_snapshot(tmp_path)
    for kind in ('status', 'history'):
        document = read(kernel.storage.path, kind, storage=kernel.storage)
        current = document.data['current_observation']
        assert current['host'] == {'disposition': 'exited', 'source': 'local'}
        worker, = current['workers']
        assert worker['disposition'] == 'exited'
        assert worker['source'] == ('recorded_exit' if worker_receipt else 'host_exit')
        invocation, = document.data['subjects'][0]['pending_attempt']['invocations']
        output, = invocation['outputs']
        assert output['dispatch_state'] == ('exited' if worker_receipt else 'started')
        assert output['exit_code'] == (0 if worker_receipt else None)
        assert (output['exited_at'] is not None) is worker_receipt
        for language, recorded, observed in [('en', 'recorded exit', 'Host-domain exit observed; no Worker exit receipt recorded'),
                                              ('zh', '已记录退出', '已观察到 Host 域退出；未记录 Worker 退出回执')]:
            for style in ('plain', 'rich'):
                stream = StringIO()
                render(replace(document, language=language), style=style, details=False, stream=stream)
                text = ' '.join(stream.getvalue().split())  # Rich may wrap the descriptive label.
                assert (recorded in text) is worker_receipt
                assert (observed in text) is not worker_receipt
    assert kernel.active_host_action() == 'launch'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('state', ['running', 'exited', 'unknown'])
def test_public_current_summary_does_not_replace_history_and_renders_both_languages(
    inspection_worker, tmp_path, monkeypatch, capsys, state,
):
    import json
    from io import StringIO
    from agent_run.cli.main import main
    from agent_run.cli.render import render
    from agent_run.queries.run import read
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    kernel, request, call, _, _, _, _ = inspection_worker
    original = kernel.host_usage('launch')
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw:
        HostInspection(binding.request, HostObservation('unknown', None, backend='foreground'), 'unavailable'))
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', lambda self, binding, c, **kw:
        WorkerInspection(c.execution, c.invocation_id, c.output_ordinal, 'unknown', 'unavailable'))
    frozen = read(kernel.storage.path, 'history', storage=kernel.storage).data['history']
    observed = replace(original, disposition=state, ended_at=None, exit_status=None)
    source = 'unavailable' if state == 'unknown' else 'local'
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw: HostInspection(binding.request, observed, source))
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', lambda self, binding, c, **kw:
        WorkerInspection(c.execution, c.invocation_id, c.output_ordinal, state, source))
    before = durable_snapshot(tmp_path)
    status = read(kernel.storage.path, 'status', storage=kernel.storage)
    history = read(kernel.storage.path, 'history', storage=kernel.storage)
    assert status.data['current_observation'] == history.data['current_observation']
    assert history.data['history'] == frozen
    assert history.data['executor_control']['live_status'] == 'unknown'
    assert status.data['executor_control']['live_status'] == state
    for attempt in history.data['history']['attempts']:
        assert all(i['live_status'] == 'unknown' for i in attempt['invocations'])
    pending, = status.data['subjects']
    invocation, = pending['pending_attempt']['invocations']
    assert invocation['live_status'] == state and invocation['outputs'][0]['exited_at'] is None
    for language, heading, alive, exited in [('en', 'Current process observation', 'observed alive', 'observed exited'),
                                           ('zh', '当前进程观察', '已观察到存活', '已观察到退出')]:
        for document in (status, history):
            for style in ('plain', 'rich'):
                out = StringIO()
                render(replace(document, language=language), style=style, details=False, stream=out)
                text = out.getvalue()
                assert heading in text
                assert (alive in text) is (state == 'running')
                assert (exited in text) is (state == 'exited')
                assert call.invocation_id not in text and request.nonce not in text
    args = ['status', request.run_id, '--state-dir', str(kernel.storage.path)]
    assert main([*args, '--json']) == 0
    assert json.loads(capsys.readouterr().out)['current_observation'] == status.data['current_observation']
    assert main([*args, '--plain']) == 0
    assert 'Current process observation' in capsys.readouterr().out
    assert durable_snapshot(tmp_path) == before


def test_public_observation_io_is_outside_readonly_transactions(inspection_worker, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent_run.queries.run import read
    from agent_run.storage.sqlite import Storage
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    kernel, request, _, _, _, _, _ = inspection_worker
    original, active, reads = Storage.transaction, [], []
    @contextmanager
    def transaction(storage, *, write=True, **kwargs):
        assert write is False, 'A public query attempted a write transaction'
        with original(storage, write=write, **kwargs) as tx:
            active.append(storage)
            try:yield tx
            finally:active.pop()
    monkeypatch.setattr(Storage, 'transaction', transaction)
    def host(self, binding, *, deadline):
        r = binding.request
        assert not active
        reads.append('host')
        return HostInspection(binding.request, binding.usage, 'local')
    def worker(self, binding, c, *, deadline):
        assert not active
        reads.append('worker')
        return WorkerInspection(c.execution, c.invocation_id, c.output_ordinal, 'running', 'local')
    monkeypatch.setattr(ForegroundInspector, 'inspect', host)
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', worker)
    def forbidden(*a, **kw):pytest.fail('Public observation called a mutating lifecycle entry')
    for name in ('observe', 'launch', 'preflight'):monkeypatch.setattr(ForegroundHost, name, forbidden)
    for name in ('observe_host', 'dispatch_host', 'authorize_host', 'control', 'stop_task'):
        monkeypatch.setattr(Kernel, name, forbidden)
    before = durable_snapshot(tmp_path)
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data['current_observation']['host']['disposition'] == 'running'
    assert reads == ['host', 'worker'] and active == []
    assert durable_snapshot(tmp_path) == before


def test_public_observation_sources_share_the_original_deadline(inspection_worker, monkeypatch):
    from types import SimpleNamespace
    from agent_run.queries.run import read
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    import agent_run.queries.current_observation as current
    kernel = inspection_worker[0]
    seen = []
    def host(self, binding, *, deadline):
        seen.append(deadline)
        return HostInspection(binding.request, binding.usage, 'local')
    def worker(self, binding, call, *, deadline):
        seen.append(deadline)
        return WorkerInspection(call.execution, call.invocation_id, call.output_ordinal, 'running', 'local')
    monkeypatch.setattr(current, 'time', SimpleNamespace(monotonic=lambda: 100.))
    monkeypatch.setattr(ForegroundInspector, 'inspect', host)
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', worker)
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data['current_observation']['host']['disposition'] == 'running'
    assert seen == [103., 103.]


def test_fifo_protection_record_cannot_block_public_status_or_history(inspection, tmp_path):
    import os
    import subprocess
    import sys
    kernel, _, request, _, _, path = inspection
    before = durable_snapshot(tmp_path)
    original = path.read_bytes()
    path.unlink()
    os.mkfifo(path, 0o600)
    program = '''import sys,time
from pathlib import Path
from agent_run.queries.run import read
path,run=Path(sys.argv[1]),sys.argv[2]
start=time.monotonic()
status=read(path,'status',expected_run=run).data
assert status['current_observation']['host']['disposition']=='unknown'
events=[];after=0
while True:
    data=read(path,'history',expected_run=run,after=after,limit=1).data
    assert data['current_observation']['host']['disposition']=='unknown'
    page=data['history'];events.extend(page['events'])
    if not page['has_more']:break
    after=page['next_after']
assert events and len({e['position'] for e in events})==len(events)
assert time.monotonic()-start<3
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(kernel.storage.path), request.run_id],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o600)
    assert durable_snapshot(tmp_path) == before


def test_invalid_recorded_document_is_rejected_before_current_process_io(inspection_worker, tmp_path, monkeypatch):
    import agent_run.queries.run as query
    kernel, _, _, _, _, _, _ = inspection_worker
    original = query._subject
    def damaged(*args, **kwargs):
        job = original(*args, **kwargs)
        job['recorded_value'] = float('inf')
        return job
    monkeypatch.setattr(query, '_subject', damaged)
    def forbidden(*args, **kwargs):pytest.fail('Invalid recorded document reached physical observation')
    monkeypatch.setattr(ForegroundInspector, 'inspect', forbidden)
    before = durable_snapshot(tmp_path)
    with pytest.raises(query.QueryError, match='Out of range float values') as failed:
        query.read(kernel.storage.path, 'status', storage=kernel.storage)
    assert failed.value.code == 'incompatible_run_state'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('fault', ['os', 'domain', 'host-scope', 'worker-scope', 'deadline', 'final-deadline'])
def test_public_observation_failure_is_unknown_without_losing_recorded_work(inspection_worker, tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    from agent_run.queries.run import read
    import agent_run.queries.current_observation as current
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    kernel, request, _, _, _, _, _ = inspection_worker
    def host(self, binding, *, deadline):
        r = binding.request
        if fault == 'os':raise PermissionError('controlled private path must not leak')
        if fault == 'domain':raise physical.DomainUnknown('controlled namespace unavailable')
        if fault == 'host-scope':r = replace(r, nonce='foreign')
        return HostInspection(r, kernel.host_usage('launch'), 'local')
    def worker(self, binding, c, *, deadline):
        assert fault != 'deadline', 'Deadline must prevent the Worker probe'
        execution = replace(c.execution, epoch=c.execution.epoch + 1) if fault == 'worker-scope' else c.execution
        return WorkerInspection(execution, c.invocation_id, c.output_ordinal, 'running', 'local')
    monkeypatch.setattr(ForegroundInspector, 'inspect', host)
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', worker)
    if fault in {'deadline', 'final-deadline'}:
        ticks = iter((0., 0., 4., 4.) if fault == 'deadline' else (0., 0., 1., 1., 4.))
        monkeypatch.setattr(current, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    before = durable_snapshot(tmp_path)
    document = read(kernel.storage.path, 'status', storage=kernel.storage)
    view = document.data['current_observation']
    assert view['host']['disposition'] == 'unknown' and view['reason'] is not None
    assert all(worker['disposition'] == 'unknown' for worker in view['workers'])
    assert document.data['subjects'][0]['pending_attempt'] is not None
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('fault', ['factory-error', 'factory-timeout', 'factory-deadline', 'mutated-binding'])
def test_observation_source_setup_and_snapshot_mutation_cannot_hide_history(inspection_worker, monkeypatch, fault):
    from types import SimpleNamespace
    from agent_run.queries.run import read
    from agent_run.runtime.foreground_host import HostInspection
    import agent_run.queries.current_observation as current
    kernel = inspection_worker[0]
    before = durable_snapshot(kernel.storage.path.parent)
    def factory(binding):
        from agent_run.runtime.foreground_host import WorkerInspection
        assert not hasattr(binding, 'storage') and not hasattr(binding, 'kernel')
        if fault == 'factory-error':raise PermissionError('source unavailable')
        if fault == 'factory-timeout':raise TimeoutError('source budget expired')
        if fault == 'mutated-binding':binding.host['status'] = 'foreign'
        def host(b, *, deadline):
            assert fault == 'mutated-binding', 'Setup deadline must prevent physical IO'
            return HostInspection(b.request, b.usage, 'local')
        return SimpleNamespace(inspect=host, inspect_worker=lambda b, w, **kw:
            WorkerInspection(w.execution, w.invocation_id, w.output_ordinal, 'running', 'local'))
    monkeypatch.setattr(current, 'inspector_for', factory)
    if fault == 'factory-deadline':
        ticks = iter((0., 4.))
        monkeypatch.setattr(current, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    document = read(kernel.storage.path, 'history', storage=kernel.storage, after=0, limit=1)
    observed = document.data['current_observation']
    assert observed['host']['disposition'] == 'unknown'
    assert observed['reason'] == ('observation_deadline' if fault == 'factory-deadline' else
        'observation_binding_changed' if fault == 'mutated-binding' else 'observation_unavailable')
    assert document.data['history']['events']
    assert durable_snapshot(kernel.storage.path.parent) == before


@pytest.mark.parametrize('change', ['raw', 'epoch', 'host', 'invocation'])
def test_public_observation_rechecks_the_original_frozen_bindings(inspection_worker, inspection, monkeypatch, change):
    from agent_run.queries.run import read
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    kernel, request, call, process, _, _, _ = inspection_worker
    ports = inspection[1]
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw:
        HostInspection(binding.request, binding.usage, 'local'))
    successor = []
    def worker(self, binding, c, *, deadline):
        if change == 'raw':
            kernel.receive(call, b'original concurrent callback')
        elif change == 'epoch':
            assert kernel.stop_task('concurrent-public-stop') == 'applied'
        elif change == 'host':
            kernel.observe_host(request, HostObservation('unknown', None, backend='foreground'))
        else:
            kernel.agent_exited(call, process, 1, failure='ordinary')
            operations = CoreOperations(kernel, call.execution, lambda _: ports)
            next_call = operations.until(lambda step: step.call is not None).call
            assert next_call is not None and next_call.invocation_id != call.invocation_id
            successor.append(next_call)
        return WorkerInspection(c.execution, c.invocation_id, c.output_ordinal, 'running', 'local')
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', worker)
    current = read(kernel.storage.path, 'status', storage=kernel.storage).data['current_observation']
    assert current['host']['disposition'] == 'unknown' and current['reason'] == 'observation_binding_changed'
    assert all(item['disposition'] == 'unknown' for item in current['workers'])
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 1
        if change == 'epoch':
            assert tx.one('SELECT stopped FROM runs WHERE id=?', (kernel.storage.run_id,))[0]
        elif change == 'raw':
            raw_id = tx.one('SELECT raw_id FROM invocation_outputs WHERE run_id=? AND invocation=?',
                (kernel.storage.run_id, call.invocation_id))[0]
            assert tx.get(raw_id, call.subject) == b'original concurrent callback'
        elif change == 'invocation':
            assert tx.one('SELECT current_invocation FROM attempts WHERE run_id=? AND id=?',
                (kernel.storage.run_id, call.attempt_id))[0] == successor[0].invocation_id


def test_public_observation_refuses_local_evidence_for_a_copied_database(inspection_worker, tmp_path, monkeypatch):
    import shutil
    from agent_run.queries.run import read
    kernel, _, _, _, _, _, _ = inspection_worker
    copied = tmp_path / 'public-copy.sqlite3'
    shutil.copyfile(kernel.storage.path, copied)
    def forbidden(*args, **kwargs):pytest.fail('A copied DB must not reach local process observation')
    monkeypatch.setattr(foreground_host, 'recover_init', forbidden)
    monkeypatch.setattr(foreground_host.ProcessPin, 'open', forbidden)
    before = durable_snapshot(tmp_path)
    copied_view = Kernel(copied, run_id=kernel.storage.run_id)
    current = read(copied, 'status', expected_run=kernel.storage.run_id, storage=copied_view.storage).data['current_observation']
    assert current['host']['disposition'] == 'unknown'
    assert all(item['disposition'] == 'unknown' for item in current['workers'])
    assert durable_snapshot(tmp_path) == before


def test_public_observation_unsupported_backend_does_not_probe_real_services(tmp_path, monkeypatch):
    from agent_run.adapters.systemd import SystemdHost
    from agent_run.queries.run import read
    kernel, _ = task_run(tmp_path, 'systemd-current')
    request = kernel.admit_host('launch', kind='run', command=('controlled-executor',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation('running', request.unit_name, 'a' * 32,
        '/controlled/host', 'test-boot', 42, 'original', usage_ready=True)) == 'running'
    def forbidden(*a, **kw):pytest.fail('Unsupported query wiring must not contact a real service')
    monkeypatch.setattr(SystemdHost, 'observe', forbidden)
    monkeypatch.setattr(ForegroundInspector, 'inspect', forbidden)
    before = durable_snapshot(tmp_path)
    current = read(kernel.storage.path, 'status', storage=kernel.storage).data['current_observation']
    assert current['host']['disposition'] == 'unknown' and current['reason'] == 'backend_observation_unavailable'
    assert durable_snapshot(tmp_path) == before


@pytest.mark.parametrize('language,heading', [('en', 'Current process observation'), ('zh', '当前进程观察')])
def test_public_current_observation_uses_frozen_run_language(tmp_path, monkeypatch, capsys, language, heading):
    import json
    from agent_run.cli.main import main
    from agent_run.runtime.foreground_host import HostInspection
    from support.cli_fixtures import _create
    kernel = _create(tmp_path / 'state', 'current-language', language=language)
    request = kernel.admit_host('launch', kind='run', command=('controlled-executor',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(lock)}
    running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
        backend='foreground', domain=_domain(request.nonce), usage_ready=True, evidence=_encode(receipt).decode())
    assert kernel.observe_host(request, running) == 'running'
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw: HostInspection(binding.request, running, 'local'))
    before = durable_snapshot(tmp_path)
    args = ['status', request.run_id, '--state-dir', str(kernel.storage.path)]
    assert main([*args, '--plain']) == 0
    assert heading in capsys.readouterr().out
    assert main([*args, '--json']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['current_observation']['host']['disposition'] == 'running'
    assert durable_snapshot(tmp_path) == before


def test_public_current_workers_keep_parallel_subject_and_call_bindings(tmp_path, monkeypatch):
    from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec
    from agent_run.queries.run import read
    from agent_run.runtime.foreground_host import HostInspection, WorkerInspection
    from support.core_operations import native_agent_fixture, source_operations
    base = Commit('base', 'tree')
    deliveries = tuple(Delivery(f'ticket:{n}', DeliveryKind.TICKET, n, 'original', base,
        f'refs/heads/ticket-{n}', 'refs/heads/run', checkout=str(tmp_path / f'work-{n}')) for n in (2, 3))
    kernel, ports = native_agent_fixture(tmp_path / 'parallel.sqlite3', RunSpec('parallel-inspection',
        'example/project', 1, 'parent', 'graph', deliveries, Policy(concurrency=2),
        language='en', repository_root=str(tmp_path / 'repository')))
    request = kernel.admit_host('launch', kind='run', command=('controlled-executor',),
        environment_carrier=tmp_path / 'carrier', preflight=lambda: None, backend='foreground')
    assert kernel.dispatch_host(request)
    lock = tmp_path / 'install.lock'
    with runner_usage_lease(lock):
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce, 'runner': _runner_witness(lock)}
    running = HostObservation('running', None, boot_id='test-boot', main_pid=202, start_token='203',
        backend='foreground', domain=_domain(request.nonce), usage_ready=True, evidence=_encode(receipt).decode())
    assert kernel.observe_host(request, running) == 'running'
    operations = source_operations(kernel, ports.requirements, Execution(request.run_id, request.owner, request.epoch))
    calls = [operations.until(lambda step: step.call is not None).call for _ in range(2)]
    assert all(call is not None for call in calls)
    expected = {(call.subject, call.attempt_id, call.invocation_id, call.output_ordinal) for call in calls}
    assert len(expected) == 2
    seen = []
    monkeypatch.setattr(ForegroundInspector, 'inspect', lambda self, binding, **kw: HostInspection(binding.request, running, 'local'))
    def worker(self, binding, call, *, deadline):
        seen.append(call)
        return WorkerInspection(call.execution, call.invocation_id, call.output_ordinal, 'running', 'local')
    monkeypatch.setattr(ForegroundInspector, 'inspect_worker', worker)
    before = durable_snapshot(tmp_path)
    document = read(kernel.storage.path, 'status', storage=kernel.storage)
    current = document.data['current_observation']
    assert {(item['subject'], item['attempt_id'], item['invocation_id'], item['output_ordinal'])
            for item in current['workers']} == expected
    assert {(call.subject, call.attempt_id, call.invocation_id, call.output_ordinal) for call in seen} == expected
    with kernel.storage.transaction(write=False) as tx:
        assert all(row[0] == 0 for row in tx.rows("SELECT developments_used FROM budgets WHERE run_id=? AND purpose='requirements'", (kernel.storage.run_id,)))
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 2
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
    assert Kernel(kernel.storage.path, run_id=kernel.storage.run_id).host_usage('launch').exit_status is None
    resumed = kernel.admit_host('resume', **{**args, 'kind': 'resume'})
    assert resumed.owner != request.owner and resumed.epoch > request.epoch
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM attempts WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
        assert tx.one("SELECT count(*) FROM actions WHERE run_id=? AND id='stop'", (kernel.storage.run_id,))[0] == 1


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
            assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == int(not known_worker_exit)
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
        document = read(kernel.storage.path, kind, storage=kernel.storage)
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
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
        assert tx.one('SELECT developments_used FROM budgets WHERE run_id=? AND subject=? AND purpose=? AND current=1',
                      (kernel.storage.run_id, call.subject, call.purpose))[0] == 1
        assert tx.one('SELECT backend_id FROM threads WHERE run_id=? AND id=?', (kernel.storage.run_id, call.thread_id))[0] == 'original-backend-thread'
        raw_id = tx.one('SELECT raw_id FROM invocation_outputs WHERE run_id=? AND invocation=?', (kernel.storage.run_id, call.invocation_id))[0]
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
        manager.reconcile_closed(Kernel(kernel.storage.path, run_id=kernel.storage.run_id), lock)
        observed = manager.observe(kernel, request, lock)
        assert observed.disposition == 'exited' and observed.exit_status == 7 and observed.usage_released
        assert observed.namespace_exit_status == -9 and observed.exit_evidence == 'retained_namespace_pidfd'
        assert not path.exists()
        assert manager.observe(Kernel(kernel.storage.path, run_id=kernel.storage.run_id), request, lock) == observed
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
        assert tx.one('SELECT count(*) FROM attempts WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
        assert tx.one('SELECT count(*) FROM leases WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
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
