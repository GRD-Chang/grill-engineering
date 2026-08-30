"""Business execution boundary for one admitted Delivery Run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from agent_run.state import StateStore
from agent_run.task_control import TaskControlError


class _RunDriver(Protocol):
    def advance(self, state: dict[str, Any]) -> Mapping[str, Any]: ...


class DeliveryExecutor:
    """Execute exactly the Run selected by the lifecycle Action.

    The lifecycle spine and Executor Host do not know about Delivery Engine
    details.  The CLI only composes this boundary; all current-Task business
    writes happen from this Executor entrypoint.
    """

    def __init__(
        self,
        *,
        states: StateStore,
        driver_factory: Callable[[StateStore], _RunDriver],
        state_store_factory: Callable[[], StateStore] | None = None,
        record_execution_failure: Callable[[StateStore, str, str], bool]
        | None = None,
    ) -> None:
        self.states = states
        self.driver_factory = driver_factory
        self.state_store_factory = state_store_factory
        self.record_execution_failure = record_execution_failure

    def execute(self, run_id: str) -> Mapping[str, Any]:
        states = self.state_store_factory() if self.state_store_factory else self.states
        state = states.load_current_run(run_id)
        if state is None:
            raise TaskControlError("Executor 找不到要推进的 Delivery Run")
        try:
            return self.driver_factory(states).advance(state)
        except KeyboardInterrupt:
            if self.record_execution_failure is not None:
                self.record_execution_failure(
                    states, run_id, "executor_agent_interrupted"
                )
            raise
