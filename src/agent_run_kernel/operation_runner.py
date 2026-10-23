"""Core operation interface, bound to one in-process physical adapter.

run_once performs one committed transition, one Effect, or one poll and returns;
an AgentCall is returned immediately for Host supervision. It never runs an
Agent or waits for a workflow/CI to finish. observe is an explicit operational
read: it accepts fresh Requirements/audit facts but allocates no work, advances
no phase and grants no write. User status/history queries do not call observe.

No Receipt, persisted cursor, or alternative current Run crosses this seam.
"""
from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Callable
import json



from .model import AgentCall, Effect, EffectResult, Execution, KernelError, ReadWait, Step, World
from .operation_contract import (OperationBinding, OperationPorts, OperationState, ReadUnavailable,
                                 ReadFence, RequirementsRead, DeliveryRead, DeliveryFacts,
                                 _ObservationPass, PrepareIO, EffectIO, ObserveIO, IOResult)

if TYPE_CHECKING:
    from .kernel import Kernel


def _read_observation(kernel: Kernel, ports: OperationPorts, request: RequirementsRead) -> tuple[World, ReadFence]:
    source = ports.source(request.parent_number)
    delivery = kernel.accept_requirements(request, source)
    facts = ports.delivery(delivery.query)
    return kernel.accept_delivery(delivery, facts)


def observe_control(kernel: Kernel, subject: str, make_ports: Callable[[OperationBinding], OperationPorts]) -> World:
    """Explicit physical observation for control; public queries never call it."""
    ports = make_ports(kernel.operation_binding())
    try:
        return _read_observation(kernel, ports, kernel.begin_observation(subject))[0]
    except ReadUnavailable as error:
        raise KernelError(error.code, error.message) from error


class OperationRunner:
    """Physical driver: OperationRunner(kernel, execution, make_ports).

    Core seals the binding; this driver calls make_ports(binding). Host never
    pre-reads Core SQL or substitutes future defaults. Host runs at most one
    run_once Future in its independent I/O pool. A
    bootstrap authorization reads inline in the Agent thread, never queues back
    into a pool it is waiting on. Native raw/exit/thread/activity hooks remain
    their existing exact-call record interfaces.

    agent_capacity is this call's available new physical Agent slots. Zero
    still permits returned-result consumption, mechanical work and polls.
    state and run_once use the same transition selection. worker_settled uses
    exact durable exit/non-start proof plus lease release, never Future.done.
    Source outages retain their existing bounded wait; failed post-write reads
    retain the sent intent, and unknown does not authorize rewriting. Each real
    write calls authorize immediately beforehand; external I/O holds no SQL tx.
    """
    def __init__(self, kernel: Kernel, execution: Execution,
                 make_ports: Callable[[OperationBinding], OperationPorts]) -> None:
        self.kernel, self.execution = kernel, execution
        self.binding = kernel.operation_binding(execution)
        self.ports = make_ports(self.binding)
        self._observation: _ObservationPass | None = None
        self._activation_lock = RLock()

    def activate_observation(self) -> None:
        """Once per admitted, usage-ready Host entry, before its first state().

        Capture finite original sent identities and approval boundaries only.
        Repeated activation cannot refresh membership, waits or deadlines.
        Direct claim_executor fixtures do not possess this admission.
        """
        with self._activation_lock:
            if self._observation is None:
                self._observation = self.kernel.activate_observation(self.execution)

    def state(self) -> OperationState:
        return self.kernel.operation_state(self._observation)

    def observe(self, subject: str | None) -> World:
        return self._observe(subject)[0]

    def _observe(self, subject: str | None, *, cleanup: bool = False) -> tuple[World, ReadFence]:
        request = self.kernel.begin_observation(subject, execution=self.execution, cleanup=cleanup)
        return _read_observation(self.kernel, self.ports, request)

    def authorize_agent(self, call: AgentCall) -> None:
        if call.execution != self.execution:
            raise KernelError("invocation_binding")
        request = self.kernel.begin_observation(call.subject, execution=self.execution, call=call)
        try:
            world, fence = _read_observation(self.kernel, self.ports, request)
        except ReadUnavailable as error:
            raise ReadWait(self.kernel._wait_for_read(request.fence, self.execution, error.code, error.message)) from error
        self.kernel.authorize_agent_observation(call, world, fence)

    def worker_settled(self, call: AgentCall) -> bool:
        with self.kernel.storage.transaction(write=False) as tx:
            row = self.kernel._invocation(tx, call)
            leased = tx.rows("SELECT id FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
            return not leased and (row["dispatch_state"] == "exited" and row["exit_code"] is not None
                                   or row["dispatch_state"] == "unavailable" and row["process_identity"] is None and row["start_evidence"] is not None)

    def run_once(self, *, agent_capacity: int = 1) -> Step:
        request = self.kernel.next_work(self.execution, agent_capacity=agent_capacity, observation=self._observation)
        while not isinstance(request, Step):
            current = request
            try:
                result: IOResult
                if isinstance(current, ObserveIO):
                    result = _read_observation(self.kernel, self.ports, current.request)
                elif isinstance(current, PrepareIO):
                    result = (self.ports.prepare_publication(current.effect) if current.kind == "publication" else
                              self.ports.prepare_retirement(current.effect))
                else:
                    if current.kind == "readback":
                        result = self.ports.reconcile(current.effect)
                    else:
                        try:
                            result = self.ports.execute(current.effect, lambda: self._authorize_work(current))
                        except OSError as error:
                            request = self.kernel.fail_work(current.work, error, request=current)
                            continue
                request = self.kernel.accept_work(current, result)
            except (ReadWait, ReadUnavailable, KernelError) as error:
                request = self.kernel.fail_work(current.work, error, request=current)
        return request

    def _authorize_work(self, request: EffectIO) -> None:
        effect = request.effect
        try:
            world, fence = self._observe(effect.subject)
            prepared = self.kernel.effect_observation_prepared(effect, fence)
            resources = self.kernel.effect_resources(effect)
            inspections = None if resources is None else self.ports.inspect(resources)
            self.kernel.authorize_effect_observation(effect, world, fence, prepared=prepared, inspections=inspections)
        except ReadUnavailable as error:
            raise ReadWait(self.kernel._wait_for_read(request.token, self.execution, error.code, error.message)) from error
