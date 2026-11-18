"""One bounded current view, separate from durable and historical facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import sqlite3
import time
from typing import Any

from agent_run.storage.inspection import InspectionBindings, capture as capture_bindings
from agent_run.storage.sqlite import Storage
from agent_run.runtime.inspection import Inspector, inspector_for
from agent_run.adapters.foreground import DomainUnknown
from agent_run.errors import KernelError
from agent_run.storage.sqlite import Transaction


def capture(storage: Storage, tx: Transaction) -> InspectionBindings | None:
    try:
        return capture_bindings(tx, storage.path)
    except (KernelError, ValueError, KeyError, TypeError, OverflowError):
        # Missing observation bindings must not hide independently readable work.
        return None


def read_current(storage: Storage, expected: InspectionBindings | None,
                 scope: tuple[str, str | None, int], *, inspector: Inspector | None = None) -> dict[str, Any]:
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
        for call in expected.workers]
    if request is None:return unavailable('no_current_host_binding')
    if expected.current != scope:return unavailable('observation_binding_changed')
    try:
        deadline = time.monotonic() + 3  # Query IO budget, never an Agent clock.
        # Keep the original snapshot private: a new source cannot mutate the
        # comparison basis through nested process/domain mappings.
        observed_binding = deepcopy(expected)
        manager = inspector if inspector is not None else inspector_for(observed_binding)
        if manager is None:return unavailable('backend_observation_unavailable')
        if time.monotonic() >= deadline:return unavailable('observation_deadline')
        host = manager.inspect(observed_binding, deadline=deadline)
        if (host.request != request or host.observation.backend != request.backend
                or host.source not in {'local', 'recorded_exit', 'unavailable'}
                or host.observation.disposition not in {'running', 'exited', 'unknown'}
                or host.source == 'unavailable' and host.observation.disposition != 'unknown'
                or host.source == 'recorded_exit' and host.observation.disposition != 'exited'):
            return unavailable('observation_identity_unproven')
        result['host'] = {'disposition': host.observation.disposition, 'source': host.source}
        for call, item in zip(observed_binding.workers, result['workers']):
            if time.monotonic() >= deadline:continue
            observed = manager.inspect_worker(observed_binding, call, deadline=deadline)
            if (observed.execution != call.execution or observed.invocation_id != call.invocation_id
                    or observed.output_ordinal != call.output_ordinal
                    or observed.disposition not in {'running', 'exited', 'unknown'}
                    or observed.source not in {'local', 'recorded_exit', 'host_exit', 'unavailable'}
                    or observed.source == 'unavailable' and observed.disposition != 'unknown'
                    or observed.source in {'recorded_exit', 'host_exit'} and observed.disposition != 'exited'):
                return unavailable('observation_identity_unproven')
            item.update(disposition=observed.disposition, source=observed.source)
        if time.monotonic() >= deadline:return unavailable('observation_deadline')
        if observed_binding != expected:return unavailable('observation_binding_changed')
        with storage.transaction(write=False) as tx:
            if capture_bindings(tx, storage.path) != expected:return unavailable('observation_binding_changed')
        if time.monotonic() >= deadline:return unavailable('observation_deadline')
        return result
    except (OSError, sqlite3.Error, KernelError, DomainUnknown, ValueError, KeyError, TypeError, OverflowError):
        return unavailable('observation_unavailable')
