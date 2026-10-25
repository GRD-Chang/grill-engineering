"""Foreground Host contracts, using current native admission and local facts."""
from dataclasses import replace
import errno
from pathlib import Path

import pytest

from agent_run.adapters import foreground as physical
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import HostObservation
from agent_run.runtime.package_lease import runner_management_lease, runner_usage_lease, RunnerLeaseBusy
from agent_run.runtime.foreground_host import ForegroundHost
import agent_run.runtime.foreground_host as foreground_host
from agent_run.cli.lifecycle import attach
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from test_kernel_host_admission import task_run


def _domain(nonce):
    observer = {'boot_id': 'test-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'}
    common = {'boot_id': 'test-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
    return {'nonce': nonce, 'observer': observer,
            'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
            'executor': {**common, 'pid': 202, 'parent': 200, 'start_token': '203', 'namespace_pids': [202, 2]}}


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
