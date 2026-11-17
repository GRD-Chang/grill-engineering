"""Exact Run closure selection with real Core/SQLite and controlled records.

No Host, Worker, namespace, signal, child or external notification is run.
The observations below are an adapter substitute, not physical exit proof.
"""

from agent_run.runtime.task import create_task, selected_kernel
from support.repository_fixture import create_kernel
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from agent_run.adapters import foreground as physical
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.model import Commit, Delivery, DeliveryKind, HostObservation, ParentIssue, Requirements, RunSpec
from agent_run.runtime import foreground_host
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.runtime.handoff import _digest, _encode, _runner_witness
from agent_run.runtime.package_lease import RunnerLeaseBusy, runner_management_lease, runner_usage_lease
from agent_run.storage.routing import RepositoryRoute
from support.offline import fixture_inputs


def create_run(path, root, name, parent, *, route=None):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery('parent', DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
        'refs/heads/work-' + name, 'refs/heads/main')
    spec = RunSpec(name, 'example/project', parent, 'r', 'g', (delivery,), language='en',
        repository_root=str(root), inputs=fixture_inputs())
    source = Requirements(ParentIssue(parent, 'Parent', 'Frozen requirements'), ())
    if route is None:
        return create_kernel(path, spec, requirements=source)
    assert create_task(route, TaskKey(root, route.repository, parent), spec, source).created
    return selected_kernel(route, name)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    route = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    route.initialize()
    kernels = {name: create_run(route.database, route.repository_root, name, parent, route=route)
        for name, parent in (('A', 1), ('B', 2))}
    def forbidden(*args, **kwargs):
        pytest.fail('Closure reconciliation reached a physical/process/notification boundary')
    monkeypatch.setattr(physical.ProcessPin, 'open', forbidden)
    monkeypatch.setattr(foreground_host, 'recover_init', forbidden)
    monkeypatch.setattr(foreground_host, 'NamespaceProcess', forbidden)
    from agent_run.notifications import sender
    monkeypatch.setattr(sender, 'notify_committed', forbidden)
    monkeypatch.setattr(sender, 'deliver_host_exit_notice', forbidden)
    return route, kernels


def snapshot(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def protection(kernel, lock, action, *, closed=False):
    """Normal Host admission; only the external observation is controlled."""
    request = kernel.admit_host(action, kind='run', command=('/unexecuted/python',),
        environment_carrier=lock.parent / (kernel.run_paths().run_id + '.environment.json'),
        preflight=lambda: None, backend='foreground')
    receipt = {'request_digest': _digest(request), 'nonce': request.nonce,
        'runner': _runner_witness(lock), 'boot_id': 'controlled-boot', 'pid_namespace': 'pid:[10]'}
    record = {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
        'receipt': receipt, 'phase': 'pending', 'domain': None,
        'database': str(kernel.storage.path.resolve()), 'action': action}
    if closed:
        assert kernel.dispatch_host(request)
        common = {'boot_id': 'controlled-boot', 'observer_namespace': 'pid:[10]', 'pid_namespace': 'pid:[20]'}
        domain = {'nonce': request.nonce,
            'observer': {'boot_id': 'controlled-boot', 'pid_namespace': 'pid:[10]', 'pid1_start': '100'},
            'init': {**common, 'pid': 200, 'parent': 199, 'start_token': '201', 'namespace_pids': [200, 1]},
            'executor': {**common, 'pid': 202, 'parent': 200, 'start_token': '203', 'namespace_pids': [202, 2]}}
        running = HostObservation('running', None, boot_id='controlled-boot', main_pid=202, start_token='203',
            backend='foreground', domain=domain, usage_ready=True, evidence=_encode(receipt).decode())
        assert kernel.observe_host(request, running) == 'running'
        exited = replace(running, disposition='exited', usage_ready=False, usage_released=True,
            exit_status=None, namespace_exit_status=-9, ended_at=kernel.clock(), exit_evidence='retained_namespace_pidfd')
        assert kernel.observe_host(request, exited) == 'exited'
        record.update(phase='exited', domain=domain, notice_settled=True, executor_exit_code=None,
            namespace_exit_code=-9, ended_at=exited.ended_at, exit_basis='retained_namespace_pidfd')
    path = physical.record_root(lock, create=True) / (request.nonce + '.json')
    physical.write_record(path, record, create=True)
    return request, path


@pytest.mark.parametrize('same_action', [False, True])
def test_foreign_run_protection_never_enters_selected_host_lookup(repository, tmp_path, monkeypatch, same_action):
    route, kernels = repository
    lock = tmp_path / 'runner.lock'
    with runner_usage_lease(lock):
        _, foreign = protection(kernels['A'], lock, 'same-action')
        if same_action:
            protection(kernels['B'], lock, 'same-action')
        before, original = snapshot(route.database), foreign.read_bytes()
        calls, usage = [], kernels['B'].host_usage
        def selected_usage(action):
            calls.append(action)
            return usage(action)
        monkeypatch.setattr(kernels['B'], 'host_usage', selected_usage)
        ForegroundHost(environment={}).reconcile_closed(kernels['B'], lock)
        assert calls == (['same-action'] if same_action else [])
        assert foreign.read_bytes() == original and snapshot(route.database) == before
    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(lock):
            pytest.fail('Foreign/unknown protection was removed from the management barrier')


def test_shared_database_reconciles_only_own_committed_exit_then_original_run(repository, tmp_path):
    route, kernels = repository
    lock = tmp_path / 'runner.lock'
    with runner_usage_lease(lock):
        _, a = protection(kernels['A'], lock, 'same-action', closed=True)
        _, b = protection(kernels['B'], lock, 'same-action', closed=True)
        before, original_a = snapshot(route.database), a.read_bytes()
        manager = ForegroundHost(environment={})
        manager.reconcile_closed(kernels['B'], lock)
        assert not b.exists() and a.read_bytes() == original_a
        assert snapshot(route.database) == before
        manager.reconcile_closed(kernels['A'], lock)
        assert not a.exists() and snapshot(route.database) == before
    with runner_management_lease(lock):
        pass


@pytest.mark.parametrize('corruption', ['pending', 'owner', 'missing-binding'])
def test_own_unknown_or_foreign_receipt_keeps_record_and_original_state(repository, tmp_path, corruption):
    route, kernels = repository
    lock = tmp_path / 'runner.lock'
    with runner_usage_lease(lock):
        _, path = protection(kernels['A'], lock, 'own', closed=corruption != 'pending')
        if corruption != 'pending':
            record = physical.read_record(path)
            if corruption == 'owner':
                record['binding'][1] = 'foreign-owner'
            else:
                record.pop('binding')
            physical.write_record(path, record)
        before, original = snapshot(route.database), path.read_bytes()
        if corruption == 'pending':
            ForegroundHost(environment={}).reconcile_closed(kernels['A'], lock)
        else:
            with pytest.raises(physical.DomainUnknown):
                ForegroundHost(environment={}).reconcile_closed(kernels['A'], lock)
        assert path.read_bytes() == original and snapshot(route.database) == before
    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(lock):
            pytest.fail('Unproven closure released the management barrier')


def test_current_single_run_committed_exit_still_closes_only_original_record(tmp_path, monkeypatch):
    kernel = create_run(tmp_path / 'current' / 'kernel.sqlite3', tmp_path / 'repository', 'original', 1)
    def forbidden(*args, **kwargs):
        pytest.fail('Committed closure attempted fresh physical observation')
    monkeypatch.setattr(physical.ProcessPin, 'open', forbidden)
    lock = tmp_path / 'runner.lock'
    with runner_usage_lease(lock):
        _, path = protection(kernel, lock, 'original-action', closed=True)
        before = snapshot(kernel.storage.path)
        ForegroundHost(environment={}).reconcile_closed(Kernel(kernel.storage.path, run_id=kernel.storage.run_id), lock)
        assert not path.exists() and snapshot(kernel.storage.path) == before
    with runner_management_lease(lock):
        pass
