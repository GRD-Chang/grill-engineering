"""Physical read-only observations consume snapshots, never a Core or store."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import time
from typing import Any, Protocol

from agent_run.adapters.foreground import DomainUnknown, ProcessPin, observer_anchor
from agent_run.contracts.host import valid_foreground_domain
from agent_run.errors import KernelError
from agent_run.model import Execution
from agent_run.runtime.foreground_host import (HostInspection, WorkerInspection,
    _bound_record, _inspect_record, _unavailable)
from agent_run.storage.inspection import InspectionBindings, WorkerBinding


class Inspector(Protocol):
    """Sources must bound any I/O by the caller's original absolute deadline."""
    def inspect(self, binding: InspectionBindings, *, deadline: float) -> HostInspection: ...
    def inspect_worker(self, binding: InspectionBindings, worker: WorkerBinding, *, deadline: float) -> WorkerInspection: ...


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Observation deadline expired')


def _lock(binding: InspectionBindings) -> Path:
    if binding.request is None or binding.usage is None:
        raise KernelError('observation_bindings_unavailable')
    lock = Path(json.loads(binding.usage.evidence)['runner']['lock'])
    if not lock.is_absolute():
        raise KernelError('observation_bindings_unavailable')
    return lock


class ForegroundInspector:
    """No launch, settlement, database access, lease release or notification API."""

    def inspect(self, binding: InspectionBindings, *, deadline: float | None = None) -> HostInspection:
        _check_deadline(deadline)
        request, original = binding.request, binding.usage
        if request is None or request.backend != 'foreground':
            raise KernelError('host_backend_changed')
        if original is not None and original.disposition == 'exited' and original.usage_released:
            return HostInspection(request, original, 'recorded_exit')
        try:
            _, record = _bound_record(request, _lock(binding), binding.database)
            _check_deadline(deadline)
            result = _inspect_record(request, original, record)
            if (result.observation.disposition == 'running'
                    and binding.current != (request.run_id, request.owner, request.epoch)):
                return _unavailable(request)
            return result
        except (OSError, ValueError, KeyError, TypeError, OverflowError, DomainUnknown):
            return _unavailable(request)

    def inspect_worker(self, binding: InspectionBindings, worker: WorkerBinding, *,
                       deadline: float | None = None) -> WorkerInspection:
        _check_deadline(deadline)
        def result(disposition: Any, source: Any) -> WorkerInspection:
            return WorkerInspection(worker.execution, worker.invocation_id, worker.output_ordinal, disposition, source)

        request, domain, process = binding.request, binding.domain, worker.process
        if request is None or request.backend != 'foreground':
            raise KernelError('host_backend_changed')
        if (worker not in binding.workers
                or worker.execution != Execution(request.run_id, request.owner, request.epoch)):
            raise KernelError('worker_host_binding')
        output = worker.output
        if output['dispatch_state'] == 'exited' and type(output['exit_code']) is int:
            return result('exited', 'recorded_exit')
        if process is None or domain is None or not valid_foreground_domain(domain, request.nonce):
            return result('unknown', 'unavailable')
        executor = domain['executor']
        if (executor is None or process.get('schema') != 'linux-namespace-process-v1'
                or (process.get('host_pid'), process.get('host_start_token'), process.get('boot_id'),
                    process.get('host_namespace'), process.get('pid_namespace')) !=
                   (executor['namespace_pids'][-1], executor['start_token'], executor['boot_id'],
                    executor['pid_namespace'], executor['pid_namespace'])):
            return result('unknown', 'unavailable')
        observed = self.inspect(binding, deadline=deadline)
        try:
            if observed.observation.disposition == 'exited':
                return result('exited', 'host_exit')
            _, record = _bound_record(request, _lock(binding), binding.database)
            _check_deadline(deadline)
            if record.get('domain') != domain:
                return result('unknown', 'unavailable')
            anchor = observer_anchor()
            expected = {'boot_id': process['boot_id'], 'pid_namespace': process['host_namespace'],
                        'pid1_start': domain['init']['start_token']}
            if (binding.current == (request.run_id, request.owner, request.epoch)
                    and anchor == expected and process['pid_namespace'] == anchor['pid_namespace']):
                with ExitStack() as stack:
                    owner = ProcessPin.open(process['host_pid'])
                    stack.callback(owner.close)
                    child = ProcessPin.open(process['pid'])
                    stack.callback(child.close)
                    if ((owner.identity.start_token, owner.identity.boot_id, owner.identity.pid_namespace)
                            == (process['host_start_token'], anchor['boot_id'], anchor['pid_namespace'])
                            and (child.identity.start_token, child.identity.boot_id, child.identity.pid_namespace)
                            == (process['start_token'], anchor['boot_id'], anchor['pid_namespace'])
                            and owner.observe() == 'running' and observer_anchor() == anchor):
                        state = child.observe()
                        if state in {'running', 'exited'}:
                            return result(state, 'local')
            return result('unknown', 'unavailable')
        except (OSError, ValueError, KeyError, TypeError, DomainUnknown):
            return result('unknown', 'unavailable')


def inspector_for(binding: InspectionBindings) -> Inspector | None:
    """The runtime composition point for supported read-only observation sources."""
    return ForegroundInspector() if binding.request is not None and binding.request.backend == 'foreground' else None
