"""Business execution boundary for one admitted Delivery Run."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent_run.executor_environment import consume_environment_carrier
from agent_run.executor_host import ExecutorSpec
from agent_run.notifications import Notifications
from agent_run.messages import error_detail, selected_language
from agent_run.runner_lease import runner_usage_lease
from agent_run.state import SimulatedProcessCrash, StateStore
from agent_run.task_control import TaskControlBusyError, TaskControlError, TaskKey
from agent_run.ticket_eligibility import TicketEligibilityError

if TYPE_CHECKING:
    from agent_run.resume_feedback import ResumeFeedback
    from agent_run.run_driver import ControlRunOperation


class _RunDriver(Protocol):
    def advance(
        self,
        state: dict[str, Any],
        *,
        control_operation: ControlRunOperation | None = None,
    ) -> Mapping[str, Any]: ...


_ExecutorBinding = tuple[str, int, str]


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
        driver_factory: Callable[[StateStore, _ExecutorBinding | None], _RunDriver],
        state_store_factory: Callable[[_ExecutorBinding | None], StateStore]
        | None = None,
        resume_feedback: ResumeFeedback | None = None,
        record_execution_failure: Callable[
            [StateStore, str, str, _ExecutorBinding | None], bool
        ]
        | None = None,
    ) -> None:
        self.resume_feedback = resume_feedback
        self.states = states
        self.driver_factory = driver_factory
        self.state_store_factory = state_store_factory
        self.record_execution_failure = record_execution_failure

    def execute(
        self,
        run_id: str,
        *,
        action_id: str | None = None,
        generation: int | None = None,
        control_operation: ControlRunOperation | None = None,
    ) -> Mapping[str, Any]:
        if (action_id is None) != (generation is None):
            raise TaskControlError("Executor action/generation binding 不完整")
        binding = (
            (action_id, generation, run_id)
            if action_id is not None and generation is not None
            else None
        )
        states = (
            self.state_store_factory(binding)
            if self.state_store_factory
            else self.states
        )
        state = states.load_current_run(run_id)
        if state is None:
            raise TaskControlError("Executor 找不到要推进的 Delivery Run")
        feedback = self.resume_feedback
        if feedback is not None:
            if action_id is not None:
                feedback.identity = action_id
            feedback.observe(state)
        notifications = Notifications(states.root, feedback.project(state) if feedback else state)
        if feedback is not None:
            feedback.attach(notifications, action_id)
        states.run_saved_observer = feedback.observe if feedback else notifications.observe
        try:
            driver = self.driver_factory(states, binding)
            if control_operation is None:
                result = driver.advance(state)
                if feedback is not None:
                    feedback.observe(dict(result))
                return result
            return driver.advance(state, control_operation=control_operation)
        except KeyboardInterrupt:
            if self.record_execution_failure is not None:
                self.record_execution_failure(
                    states,
                    run_id,
                    (
                        "control_executor_interrupted"
                        if control_operation is not None
                        else "executor_agent_interrupted"
                    ),
                    binding,
                )
            raise
        except (SimulatedProcessCrash, TaskControlBusyError, TicketEligibilityError):
            # An abrupt exit, unresolved ownership or rejected precondition
            # must not be rewritten as an applied business-operation failure.
            raise
        except Exception as error:
            if feedback is not None:
                feedback.failure(error_detail(error, selected_language(state)), evidence="execution")
            if self.record_execution_failure is not None:
                self.record_execution_failure(states, run_id, str(error), binding)
            if feedback is not None:
                feedback.observe(states.load_current_run(run_id) or state)
            raise
        finally:
            states.run_saved_observer = None
            notifications.close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-run-executor")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--carrier", required=True)
    parser.add_argument("--runner-lock", required=True)
    parsed = parser.parse_args(arguments)
    task = TaskKey(Path(parsed.workspace), parsed.repository, parsed.parent)
    state_root = Path(parsed.state_root)
    if not state_root.is_absolute():
        parser.error("--state-root must be an absolute path")
    state_root = state_root.resolve()
    spec = ExecutorSpec(
        task=task,
        action_id=parsed.action_id,
        run_id=parsed.run_id,
        generation=parsed.generation,
        state_root=state_root,
    )
    with runner_usage_lease(Path(parsed.runner_lock)):
        environment, command = consume_environment_carrier(
            Path(parsed.carrier), spec
        )
        for key in tuple(environment):
            if key.startswith(("AGENT_RUN_EXECUTOR_", "AGENT_RUN_INTERNAL_")):
                environment.pop(key)
        environment.update(
            {
                "AGENT_RUN_EXECUTOR_ACTION_ID": parsed.action_id,
                "AGENT_RUN_EXECUTOR_GENERATION": str(parsed.generation),
                "AGENT_RUN_EXECUTOR_TASK": task.fingerprint,
                "AGENT_RUN_INTERNAL_STATE_ROOT": str(state_root),
            }
        )
        os.environ.clear()
        os.environ.update(environment)
        os.chdir(task.workspace)
        from agent_run.cli import main as cli_main

        return cli_main(list(command))


if __name__ == "__main__":
    raise SystemExit(main())
