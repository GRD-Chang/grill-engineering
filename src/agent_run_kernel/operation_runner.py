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
from dataclasses import asdict
import json

from agent_run.error_safety import bounded_error


from .model import AgentCall, Effect, EffectResult, Execution, KernelError, ReadWait, Step, World, wire_json
from .operation_contract import (OperationBinding, OperationPorts, OperationState, ReadUnavailable,
                                 ReadFence, RequirementsRead, DeliveryRead, DeliveryFacts)
from .kernel import _ApprovalRead, _ObservationPass, _SentRead

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
                self._observation = self.kernel._activate_observation(self.execution)

    def state(self) -> OperationState:
        return self.kernel._operation_state(self._observation)

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
        kernel, execution = self.kernel, self.execution
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            try:
                kernel._automatic_owned(tx, run, execution)
            except KernelError as error:
                if error.code != "executor_fenced":
                    raise
                return Step("paused", "", error.code)
            choice = kernel._operation_choice(tx, run, agent_capacity=agent_capacity,
                poll_manual=self._observation is None, observation=self._observation)
            if choice is None:
                state = self.state()
                return Step(state.disposition, "", retry_at=state.wake_at)
            subject, cleanup = choice
            member = kernel._observation_member(tx, run, self._observation, subject)
            token = kernel._operation_token(tx, subject, cleanup)
            original = (kernel._effect(tx, tx.one("SELECT * FROM effects WHERE id=?", (member.effect_id,)), execution)
                        if isinstance(member, _SentRead) else None)
        observed_approval = False
        try:
            if original is not None:
                return self._observe_sent(member, original, token)
            if subject is None:
                world, observed = self._observe(None)
                if world.source is None or world.target is None:
                    raise ReadUnavailable("final_source_missing", "The original remote Final refs are absent")
                final = kernel._activate_final(execution, world.source, world.target, ancestry=world.ancestry, token=observed)
                return kernel._finish_automatic_step(token, execution, Step("progress", final, "final_activated"))
            step = (kernel._advance_cleanup(subject, execution, token=token) if cleanup else
                    kernel._advance(subject, execution, allow_agent=bool(agent_capacity), token=token))
            if step.kind == "observe":
                subject = step.subject
                world, observed = self._observe(subject, cleanup=cleanup)
                observed_approval = isinstance(member, _ApprovalRead)
                step = (kernel._advance_cleanup(subject, execution, world, token=observed) if cleanup else
                        kernel._advance(subject, execution, world, allow_agent=bool(agent_capacity), token=observed))
            # Return a freshly allocated AgentCall before any further slow read.
            if step.call is not None:
                return step
            if step.effect is not None:
                step = self._effect(step)
            step = kernel._finish_automatic_step(token, execution, step)
            if observed_approval and member is not None and self._observation is not None:
                self._observation.complete(member)
            return step
        except ReadWait as waiting:
            if member is not None and self._observation is not None:
                self._observation.defer(member)
            return waiting.step
        except ReadUnavailable as error:
            if member is not None and self._observation is not None:
                self._observation.defer(member)
            return kernel._wait_for_read(token, execution, error.code, error.message, readback=original)
        except KernelError as error:
            if error.code not in {"automatic_step_superseded", "automatic_step_paused", "executor_fenced"}:
                raise
            if member is not None and self._observation is not None:
                self._observation.defer(member)
                if error.code == "automatic_step_superseded":
                    return kernel._wait_for_read(token, execution, error.code, str(error), readback=original)
            return Step("waiting" if error.code == "automatic_step_superseded" else "paused", subject or "", error.code)

    def _observe_sent(self, member: _SentRead | _ApprovalRead | None, effect: Effect, token: str) -> Step:
        waiting = self.kernel._guard_automatic_step(token, self.execution, readback=effect)
        if waiting is not None:
            raise ReadWait(waiting)
        result = self.ports.reconcile(effect)
        self._record_readback(effect, result)
        waiting = self.kernel._finish_operation_read(token, self.execution)
        if waiting is not None:
            raise ReadWait(waiting)
        if result.disposition not in {"target", "unknown", "third"}:
            if self._observation is not None and member is not None:
                self._observation.defer(member)
            return self.kernel._wait_for_read(token, self.execution, "original_write_readback_pending",
                "The original sent request has no final readback yet", readback=effect)
        step = self.kernel._reconcile(effect, result, self.execution)
        step = self.kernel._finish_automatic_step(token, self.execution, step)
        if self._observation is not None and member is not None:
            self._observation.complete(member)
        return step

    def _record_readback(self, effect: Effect, result: EffectResult) -> None:
        with self.kernel.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
            if self.kernel._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            evidence = tx.put(effect.subject, "effect_readback", wire_json({"dispatch_serial": effect.dispatch_serial, "result": asdict(result)}))
            self.kernel._event(tx, effect.subject, "effect_readback", effect.effect_id, evidence, generation=row["generation"])

    def _effect(self, step: Step) -> Step:
        kernel, execution, effect = self.kernel, self.execution, step.effect
        assert effect is not None
        read_token = kernel._begin_automatic_step(execution, effect.subject)
        if step.kind == "reconcile":
            token = kernel._begin_automatic_step(execution, effect.subject)
            with kernel.storage.transaction(write=False) as tx:
                sent = tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0] == "sent"
            waiting = kernel._guard_automatic_step(token, execution, readback=effect if sent else None)
            if waiting is not None:
                raise ReadWait(waiting)
            result = self.ports.reconcile(effect)
        else:
            if effect.operation == "publication_commit" and not effect.payload.get("reuse_sha") and not effect.payload.get("publication_commit_intent"):
                proof = self.ports.prepare_publication(effect)
                effect = kernel.attach_publication_commit(effect, proof, execution, self.observe(effect.subject))
            if effect.operation == "retire_pr" and not effect.payload.get("retirement_intent"):
                proof = self.ports.prepare_retirement(effect)
                effect = kernel.attach_retirement(effect, proof, execution, self.observe(effect.subject))
            def authorize() -> None:
                try:
                    world, token = self._observe(effect.subject)
                except ReadUnavailable as error:
                    raise ReadWait(kernel._wait_for_read(read_token, execution, error.code, error.message)) from error
                prepared = kernel.effect_observation_prepared(effect, token)
                resources = kernel.effect_resources(effect)
                inspections = None if resources is None else self.ports.inspect(resources)
                kernel.authorize_effect_observation(effect, world, token, prepared=prepared, inspections=inspections)
            try:
                result = self.ports.execute(effect, authorize)
            except ReadUnavailable as error:
                if error.write_returncode is not None and error.write_returncode != 0:
                    kernel.observe_effect_failure(effect, {"returncode": error.write_returncode,
                        "message": bounded_error(error.write_diagnostic)})
                raise ReadWait(kernel._wait_for_read(read_token, execution, error.code, error.message)) from error
            except OSError as error:
                # A real adapter failure is not proof that the write was absent.
                # Keep the durable original sent intent for the next readback.
                with kernel.storage.transaction(write=False) as tx:
                    sent = tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0] == "sent"
                if not sent:
                    raise
                kernel.observe_effect_failure(effect, {"type": type(error).__name__, "message": bounded_error(str(error))})
                return Step("waiting", effect.subject, "original_write_readback_required")
            returncode = result.raw.get("write_returncode")
            if type(returncode) is int and returncode != 0:
                kernel.observe_effect_failure(effect, {"returncode": returncode,
                    "message": bounded_error(str(result.raw.get("write_diagnostic", "")))})
            if effect.operation == "issue_link":
                kernel.acknowledge_link(effect, result)
                return Step("progress", effect.subject)
        self._record_readback(effect, result)
        settled = kernel._reconcile(effect, result, execution)
        if settled.kind == "observe":
            world, observed = self._observe(effect.subject)
            settled = kernel._reconcile(effect, result, execution, world, token=observed)
        return settled
