"""Bounded test driving of the real Core runner, without a second selector.

Callers supply physical OperationPorts. This helper never supplies a subject,
constructs a World, executes an Agent, or manufactures process-exit evidence.
"""
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from typing import TYPE_CHECKING, Any, Mapping

from agent_run_kernel import (AgentCall, CreationInputs, Delivery, DeliveryKind, Effect, EffectResult, Execution, Issue, Kernel, Policy,
                              ParentIssue, Requirements, ResourceInspection, ResourceWitness, RunSpec, Step)
from agent_run_kernel.operation_runner import DeliveryFacts, DeliveryRead, OperationBinding, OperationPorts, OperationRunner


if TYPE_CHECKING:
    from support.offline import GraphScenario


def scripted_return(kernel: Kernel, runner: OperationRunner, call: AgentCall,
                    agent: Callable[[AgentCall], bytes]) -> None:
    """A fixture Agent's raw result only; no invented start or exit observation."""
    runner.authorize_agent(call)
    kernel.receive(call, agent(call))


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
    from support.offline import fixture_inputs

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


def final_scenario(root: Path, *, policy: Policy | None = None,
                   reviews: tuple[str, ...] = ('pass',), checks: tuple[str, ...] = ('none',),
                   inputs: CreationInputs | None = None, language: str = 'zh') -> 'GraphScenario':
    """Reach Final through a real Ticket Completion, never a seeded history row.

    The preparatory Ticket uses successful fixture Agents/checks. Only after
    Core admits Final do the requested Final Agent/check scripts start.
    """
    from support.offline import GraphScenario

    source = Requirements(ParentIssue(1, 'Parent', 'Deliver the original behavior.', (2,)), (
        Issue(2, 'Ticket', 'Implement the original behavior.', 'OPEN', frozenset({'ready-for-agent'})),
    ))
    scenario = GraphScenario(root, source, policy=policy)
    if inputs is not None:
        # Configure the fresh creation before any business operation, never
        # recreate a progressed Run and lose its real Completion history.
        scenario.spec = replace(scenario.spec, inputs=inputs, language=language)
        scenario.kernel = Kernel.create(root / 'configured.sqlite3', scenario.spec, requirements=source)
        scenario.execution = scenario.kernel.claim_executor('configured-fixture')
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    for _ in range(100):
        step = operations.once()
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
        if scenario.kernel.source_status()['final_subject'] is not None:
            assert len(scenario.kernel.source_status()['completions']) == 1
            scenario.select('final:1')  # Assertion/fixture focus, never a Core selection input.
            scenario.reviews, scenario.checks = list(reviews), list(checks)
            scenario.development_count = scenario.review_count = scenario.publication_count = 0
            return scenario
    raise AssertionError(f'Preparatory Ticket did not reach real Final admission: {operations.steps}')


class CoreOperations:
    def __init__(self, kernel: Kernel, execution: Execution,
                 make_ports: Callable[[OperationBinding], OperationPorts]) -> None:
        self.runner = OperationRunner(kernel, execution, make_ports)
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
