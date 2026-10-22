"""Bounded test driving of the real Core runner, without a second selector.

Callers supply physical OperationPorts. This helper never supplies a subject,
constructs a World, executes an Agent, or manufactures process-exit evidence.
"""
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from typing import Any, Mapping

from agent_run_kernel import (Delivery, DeliveryKind, Effect, EffectResult, Execution, Issue, Kernel,
                              ParentIssue, Requirements, ResourceInspection, ResourceWitness, RunSpec, Step)
from agent_run_kernel.operation_runner import DeliveryFacts, DeliveryRead, OperationBinding, OperationPorts


class ReadOnlyPorts:
    """Explicit external facts for Agent-lifecycle tests with no Git writes.

    Effects fail loudly: this fixture cannot stand in for an offline delivery
    adapter or silently claim a side effect succeeded.
    """
    def __init__(self, source: Requirements, source_ref: str, target_ref: str,
                 facts: DeliveryFacts) -> None:
        self.requirements, self.source_ref, self.target_ref, self.facts = source, source_ref, target_ref, facts

    def source(self, parent_number: int) -> Requirements:
        assert parent_number == self.requirements.parent.number
        return self.requirements

    def delivery(self, request: DeliveryRead) -> DeliveryFacts:
        assert (request.source_ref, request.target_ref) == (self.source_ref, self.target_ref)
        return self.facts

    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        raise AssertionError("This read-only fixture cannot inspect physical resources")

    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        raise AssertionError(f"This read-only fixture cannot execute {effect.operation}")

    def reconcile(self, effect: Effect) -> EffectResult:
        raise AssertionError(f"This read-only fixture cannot reconcile {effect.operation}")

    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]:
        raise AssertionError("This read-only fixture cannot prepare Publication")

    def prepare_retirement(self, effect: Effect) -> Mapping[str, Any]:
        raise AssertionError("This read-only fixture cannot prepare retirement")


def native_agent_fixture(path: Path, spec: RunSpec) -> tuple[Kernel, ReadOnlyPorts]:
    """Create fresh explicit source/input facts for an isolated Agent unit.

    This is fixture setup, never an upgrade or a runtime fallback. It adds the
    real Completion target required by native Ticket creation; it cannot run
    that delivery or authorize physical effects through ReadOnlyPorts.
    """
    from agent_run_kernel.offline import fixture_inputs

    assert not any(item.kind == DeliveryKind.FINAL_RUN for item in spec.deliveries)
    first = spec.deliveries[0]
    tickets = tuple(item for item in spec.deliveries if item.kind == DeliveryKind.TICKET)
    source = Requirements(ParentIssue(spec.parent_issue, "Parent", "Explicit original requirement",
        tuple(item.issue for item in tickets)), tuple(Issue(item.issue, "Ticket", item.revision,
            "OPEN", frozenset({"ready-for-agent"})) for item in tickets))
    deliveries = spec.deliveries
    if tickets:
        assert all(item.base == first.base and item.target_ref == first.target_ref for item in tickets)
        deliveries += (Delivery('fixture-final', DeliveryKind.FINAL_RUN, spec.parent_issue, 'unread',
            first.base, first.target_ref, 'refs/heads/fixture-main'),)
    inputs = spec.inputs if spec.inputs is not None else fixture_inputs()
    spec = replace(spec, repository_root=spec.repository_root or str(path.parent.resolve()),
                   language=spec.language or 'zh', inputs=inputs, deliveries=deliveries)
    return (Kernel.create(path, spec, requirements=source),
            ReadOnlyPorts(source, first.source_ref, first.target_ref, DeliveryFacts(first.base, None)))


class CoreOperations:
    def __init__(self, kernel: Kernel, execution: Execution,
                 make_ports: Callable[[OperationBinding], OperationPorts]) -> None:
        self.runner = kernel.operation_runner(execution, make_ports)
        self.steps: list[Step] = []

    def once(self, *, agent_capacity: int = 1) -> Step:
        step = self.runner.run_once(agent_capacity=agent_capacity)
        self.steps.append(step)
        return step

    def until(self, predicate: Callable[[Step], bool], *, limit: int = 64,
              agent_capacity: int = 1) -> Step:
        for _ in range(limit):
            step = self.once(agent_capacity=agent_capacity)
            if predicate(step):
                return step
            if step.call is not None:
                raise AssertionError(f"Test must handle the allocated AgentCall: {step}")
        raise AssertionError(f"Core did not reach the expected boundary in {limit} operations: {self.steps}")
