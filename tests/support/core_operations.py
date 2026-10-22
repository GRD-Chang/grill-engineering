"""Bounded test driving of the real Core runner, without a second selector.

Callers supply physical OperationPorts. This helper never supplies a subject,
constructs a World, executes an Agent, or manufactures process-exit evidence.
"""
from collections.abc import Callable

from typing import Any, Mapping

from agent_run_kernel import Effect, EffectResult, Execution, Kernel, Requirements, ResourceInspection, ResourceWitness, Step
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
