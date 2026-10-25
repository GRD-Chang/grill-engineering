"""Actual namespace failure settlement; requires normal bubblewrap capability.

No Agent/model, network, credential or business command is released by these
stories. The original Core admission and inherited-channel executor are used.
"""
import os
import sys

import pytest

from agent_run.adapters.foreground import DomainUnknown, record_root
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.runtime.executor import command
from agent_run.runtime.package_lease import runner_usage_lease, runner_management_lease
from test_kernel_host_admission import task_run


@pytest.mark.parametrize('boundary', ['before-hello', 'after-hello'])
def test_real_namespace_startup_failure_retires_original_admission(tmp_path, monkeypatch, boundary):
    kernel = task_run(tmp_path, 'foreground')[0]
    lock = tmp_path / 'install.lock'
    environment = dict(os.environ)
    arguments = ((sys.executable, '-I', '-c', 'raise SystemExit(23)') if boundary == 'before-hello'
                 else command(kernel.storage.path, 'launch', lock, environment))
    manager = ForegroundHost(environment=environment)
    request = kernel.admit_host('launch', kind='run', command=arguments,
        environment_carrier=tmp_path / 'carrier', preflight=manager.preflight, backend='foreground')
    authorize = kernel.authorize_host
    grants = []
    def interrupt(original):
        authorize(original)
        grants.append(original.nonce)
        raise KeyboardInterrupt('controlled supervisor interruption before executor release')
    if boundary == 'after-hello':monkeypatch.setattr(kernel, 'authorize_host', interrupt)
    with runner_usage_lease(lock) as lease:
        expected = DomainUnknown if boundary == 'before-hello' else KeyboardInterrupt
        with pytest.raises(expected):manager.launch(kernel, request, lock=lock, lease=lease)
    receipt = kernel.host_usage('launch')
    assert receipt.disposition == 'exited' and receipt.usage_released
    assert receipt.exit_evidence == 'retained_namespace_pidfd'
    assert receipt.domain['init']['namespace_pids'][-1] == 1
    assert receipt.domain['init']['pid_namespace'] != receipt.domain['observer']['pid_namespace']
    assert (receipt.domain['executor'] is None) == (boundary == 'before-hello')
    assert receipt.exit_status is None  # Unauthenticated/lost executor status is never zero.
    assert grants == ([] if boundary == 'before-hello' else [request.nonce])
    assert not (record_root(lock) / (request.nonce + '.json')).exists()
    assert kernel.active_host_action() is None and not kernel.dispatch_host(request)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM attempts')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
    with runner_management_lease(lock):pass
