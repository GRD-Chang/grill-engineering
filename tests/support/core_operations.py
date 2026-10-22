"""Bounded test driving of the real Core runner, without a second selector.

Callers supply physical OperationPorts. This helper never supplies a subject,
constructs a World, executes an Agent, or manufactures process-exit evidence.
"""
from collections.abc import Callable

from agent_run_kernel import Execution, Kernel, Step
from agent_run_kernel.operation_runner import OperationBinding, OperationPorts


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
