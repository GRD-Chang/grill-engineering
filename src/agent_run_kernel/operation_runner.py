"""Core operation interface, bound to one in-process physical adapter.

run_once performs one committed transition, one Effect, or one poll and returns;
an AgentCall is returned immediately for Host supervision. It never runs an
Agent or waits for a workflow/CI to finish. observe is an explicit operational
read: it accepts fresh Requirements/audit facts but allocates no work, advances
no phase and grants no write. User status/history queries do not call observe.

No Receipt, persisted cursor, or alternative current Run crosses this seam.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Protocol
from dataclasses import asdict
import json

from agent_run.error_safety import bounded_error


from .model import (AgentCall, Checks, Commit, CreationInputs, Effect, EffectResult,
                    Execution, KernelError, PullRequest, ReadWait, Requirements, ResourceInspection,
                    ResourceWitness, Step, World, wire_json)

if TYPE_CHECKING:
    from .kernel import Kernel


class ReadUnavailable(RuntimeError):
    """Port read failure; optional fields are an actual write response only."""
    def __init__(self, code: str, message: str, *, write_returncode: int | None = None,
                 write_diagnostic: str = "") -> None:
        super().__init__(message)
        self.code, self.message = code, message
        self.write_returncode, self.write_diagnostic = write_returncode, write_diagnostic


@dataclass(frozen=True)
class OperationBinding:
    repository_root: Path
    repository: str
    parent_number: int
    inputs: CreationInputs
    hidden_paths: tuple[Path, ...] = ()
    hidden_environment: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryRead:
    source_ref: str
    target_ref: str
    pr_number: int | None
    anchors: tuple[str, ...]
    check_head: str | None = None
    read_pr: bool = True


@dataclass(frozen=True)
class DeliveryFacts:
    target: Commit | None
    source: Commit | None
    pr: PullRequest | None = None
    checks: Checks | None = None
    ancestry: tuple[tuple[str, str], ...] = ()
    publisher_actor: str | None = None
    publisher_identity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class OperationState:
    concurrency: int
    active: tuple[str, ...]  # Durable lease identities, never Future completion.
    cancel: tuple[str, ...]  # The same lease identity space as active.
    wake_at: float | None  # Absolute time in Kernel.clock's domain.
    disposition: Literal["ready", "waiting", "quiescent", "complete"]


class OperationPorts(Protocol):
    """Physical reads/writes only; no Core SQL, refresh or World construction."""

    def source(self, parent_number: int) -> Requirements: ...
    def delivery(self, request: DeliveryRead) -> DeliveryFacts: ...
    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]: ...
    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult: ...
    def reconcile(self, effect: Effect) -> EffectResult: ...
    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]: ...
    def prepare_retirement(self, effect: Effect) -> Mapping[str, Any]: ...


class OperationRunner:
    """Factory: kernel.operation_runner(execution, make_ports).

    Core obtains the frozen binding, then calls make_ports(binding); Host never
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
        self.binding = kernel._operation_binding(execution)
        self.ports = make_ports(self.binding)

    def state(self) -> OperationState:
        return self.kernel.operation_state()

    def observe(self, subject: str | None) -> World:
        return self._observe(subject)[0]

    def _observe(self, subject: str | None, *, cleanup: bool = False) -> tuple[World, str]:
        kernel, execution = self.kernel, self.execution
        token = kernel._begin_automatic_step(execution, subject, cleanup=cleanup)
        waiting = kernel._guard_automatic_step(token, execution)
        if waiting is not None:
            raise ReadWait(waiting)
        source = self.ports.source(self.binding.parent_number)
        with kernel.storage.transaction() as tx:
            kernel._check_operation(tx, token, execution)
            kernel._refresh(tx, source)
            token = kernel._operation_token(tx, subject, cleanup)
            run = tx.one("SELECT * FROM runs")
            if subject is None:
                job = tx.one("SELECT * FROM final_target")
                records = tx.all("SELECT c.* FROM completions c JOIN subjects s ON s.id=c.subject AND s.generation=c.generation WHERE s.phase='complete' ORDER BY s.issue")
                anchors = {row["merge_sha"] for row in records}
            else:
                job = kernel._job(tx, subject)
                anchors = {job[key] for key in ("base_sha", "source_sha", "candidate_sha", "integrated_sha") if job[key] is not None}
                for row in tx.rows("SELECT source_sha,target_sha FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,)):
                    anchors.update(value for value in row if value is not None)
                if job["review_attempt"] is not None:
                    review = tx.one("SELECT context_id FROM attempts WHERE id=?", (job["review_attempt"],))
                    context = json.loads(tx.get(review[0], subject))
                    if "review_context" in context:
                        anchors.add(context["review_context"]["full_range"]["candidate_sha"])
            request = DeliveryRead(job["source_ref"], job["target_ref"], job["pr_number"] if subject is not None else None,
                                   tuple(sorted(anchors)), job["source_sha"] if subject is not None and job["phase"] == "checks" else None,
                                   subject is not None)
        facts = self.ports.delivery(request)
        with kernel.storage.transaction(write=False) as tx:
            kernel._check_operation(tx, token, execution)
            run = tx.one("SELECT * FROM runs")
            revision, completion = run["observed_parent_revision"], "no-tickets"
            if subject is not None:
                job = kernel._job(tx, subject)
                if job["kind"] == "ticket":
                    revision = tx.one("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))[0]
                completion = job["completion_revision"]
            if facts.pr is not None and facts.pr.state == "OPEN" and (
                    facts.source != facts.pr.head or facts.target != facts.pr.base
                    or facts.checks is not None and (facts.checks.pr_number != facts.pr.number or facts.checks.head_sha != facts.pr.head.sha)):
                raise ReadUnavailable("github_observation_pending", "PR, refs and checks have not converged")
            return World(run["observed_parent_revision"], run["observed_graph_revision"], revision, facts.target,
                         facts.source, facts.pr, facts.checks, completion, facts.ancestry,
                         facts.publisher_actor, facts.publisher_identity), token

    def authorize_agent(self, call: AgentCall) -> None:
        if call.execution != self.execution:
            raise KernelError("invocation_binding")
        with self.kernel.storage.transaction(write=False) as tx:
            self.kernel._agent_authority(tx, call)
            original = self.kernel._operation_token(tx, call.subject)
        try:
            world, token = self._observe(call.subject)
        except ReadUnavailable as error:
            raise ReadWait(self.kernel._wait_for_read(original, self.execution, error.code, error.message)) from error
        with self.kernel.storage.transaction() as tx:
            self.kernel._check_operation(tx, token, self.execution)
            run, job = self.kernel._owned(tx, call.subject, self.execution)
            if not self.kernel._current(tx, job, run, world):
                raise KernelError("agent_authority_changed")
            self.kernel._agent_authority(tx, call)

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
            choice = kernel._operation_choice(tx, run, agent_capacity=agent_capacity)
            if choice is None:
                state = kernel.operation_state()
                return Step(state.disposition, "", retry_at=state.wake_at)
            subject, cleanup = choice
            token = kernel._operation_token(tx, subject, cleanup)
        try:
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
                step = (kernel._advance_cleanup(subject, execution, world, token=observed) if cleanup else
                        kernel._advance(subject, execution, world, allow_agent=bool(agent_capacity), token=observed))
            # Return a freshly allocated AgentCall before any further slow read.
            if step.call is not None:
                return step
            if step.effect is not None:
                step = self._effect(step)
            return kernel._finish_automatic_step(token, execution, step)
        except ReadWait as waiting:
            return waiting.step
        except ReadUnavailable as error:
            return kernel._wait_for_read(token, execution, error.code, error.message)
        except KernelError as error:
            if error.code not in {"automatic_step_superseded", "automatic_step_paused", "executor_fenced"}:
                raise
            return Step("waiting" if error.code == "automatic_step_superseded" else "paused", subject or "", error.code)

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
                with kernel.storage.transaction(write=False) as tx:
                    kernel._check_operation(tx, token, execution)
                    status = tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0]
                resources = kernel.effect_resources(effect)
                inspections = None if resources is None else self.ports.inspect(resources)
                if status == "prepared" and not kernel._dispatch(effect, execution, world, resource_inspections=inspections, token=token):
                    raise KernelError("operation_cancelled")
                kernel._guard_effect(effect, execution, world, resource_inspections=inspections, token=token)
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
            if effect.operation == "issue_link":
                kernel.acknowledge_link(effect, result)
                return Step("progress", effect.subject)
        with kernel.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
            if kernel._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            evidence = tx.put(effect.subject, "effect_readback", wire_json({"dispatch_serial": effect.dispatch_serial, "result": asdict(result)}))
            kernel._event(tx, effect.subject, "effect_readback", effect.effect_id, evidence, generation=row["generation"])
        settled = kernel._reconcile(effect, result, execution)
        if settled.kind == "observe":
            world, token = self._observe(effect.subject)
            settled = kernel._reconcile(effect, result, execution, world, token=token)
        return settled
