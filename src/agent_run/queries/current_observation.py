"""One bounded current view, separate from durable and historical facts."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from agent_run.core.kernel import InspectionBindings, Kernel
from agent_run.adapters.foreground import DomainUnknown
from agent_run.errors import KernelError
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.storage.sqlite import Transaction


def capture(kernel: Kernel, tx: Transaction) -> InspectionBindings | None:
    try:
        return kernel.inspection_bindings(tx)
    except (KernelError, ValueError, KeyError, TypeError, OverflowError):
        # Missing observation bindings must not hide independently readable work.
        return None


def read_current(kernel: Kernel, expected: InspectionBindings | None,
                 scope: tuple[str, str | None, int]) -> dict[str, Any]:
    request = None if expected is None else expected.request
    result: dict[str, Any] = {
        'scope': dict(run_id=scope[0], owner=scope[1], epoch=scope[2], nonce=None if request is None else request.nonce),
        'host': {'disposition': 'unknown', 'source': 'unavailable'}, 'workers': [], 'reason': None}

    def unavailable(reason: str) -> dict[str, Any]:
        result['host'] = {'disposition': 'unknown', 'source': 'unavailable'}
        for worker in result['workers']:
            worker.update(disposition='unknown', source='unavailable')
        result['reason'] = reason
        return result

    if expected is None:return unavailable('observation_bindings_unavailable')
    result['workers'] = [{'subject': call.subject, 'role': call.role.value, 'attempt_id': call.attempt_id,
        'invocation_id': call.invocation_id, 'output_ordinal': call.output_ordinal,
        'execution': asdict(call.execution), 'disposition': 'unknown', 'source': 'unavailable'}
        for call, _ in expected.workers]
    if request is None:return unavailable('no_current_host_binding')
    if expected.current != scope:return unavailable('observation_binding_changed')
    if request.backend != 'foreground':return unavailable('backend_observation_unavailable')
    manager = ForegroundHost(environment={})  # inspect only; no preflight/launch.
    try:
        receipt = kernel.host_usage(kernel.host_action(request))
        if receipt is None:return unavailable('observation_bindings_unavailable')
        lock = Path(json.loads(receipt.evidence)['runner']['lock'])
        if not lock.is_absolute():return unavailable('observation_bindings_unavailable')
        deadline = time.monotonic() + 3  # Query IO budget, never an Agent clock.
        host = manager.inspect(kernel, request, lock)
        if (host.request != request or host.observation.backend != request.backend
                or host.source not in {'local', 'recorded_exit', 'unavailable'}
                or host.observation.disposition not in {'running', 'exited', 'unknown'}
                or host.source == 'unavailable' and host.observation.disposition != 'unknown'
                or host.source == 'recorded_exit' and host.observation.disposition != 'exited'):
            return unavailable('observation_identity_unproven')
        result['host'] = {'disposition': host.observation.disposition, 'source': host.source}
        for (call, _), item in zip(expected.workers, result['workers']):
            if time.monotonic() >= deadline:continue
            observed = manager.inspect_worker(kernel, request, call, lock)
            if (observed.execution != call.execution or observed.invocation_id != call.invocation_id
                    or observed.output_ordinal != call.output_ordinal
                    or observed.disposition not in {'running', 'exited', 'unknown'}
                    or observed.source not in {'local', 'recorded_exit', 'host_exit', 'unavailable'}
                    or observed.source == 'unavailable' and observed.disposition != 'unknown'
                    or observed.source in {'recorded_exit', 'host_exit'} and observed.disposition != 'exited'):
                return unavailable('observation_identity_unproven')
            item.update(disposition=observed.disposition, source=observed.source)
        if time.monotonic() >= deadline:return unavailable('observation_deadline')
        with kernel.storage.transaction(write=False) as tx:
            if kernel.inspection_bindings(tx) != expected:return unavailable('observation_binding_changed')
        if time.monotonic() >= deadline:return unavailable('observation_deadline')
        return result
    except (OSError, sqlite3.Error, KernelError, DomainUnknown, ValueError, KeyError, TypeError, OverflowError):
        return unavailable('observation_unavailable')
