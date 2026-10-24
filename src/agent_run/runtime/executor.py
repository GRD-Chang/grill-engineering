"""Concrete native Host entry and delivery I/O supervision.

Kernel owns every transition, seat, queue and exit decision. This process owns
the outstanding local calls, not a second persisted workflow or Run snapshot.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

from agent_run.errors import bounded_error

from agent_run.adapters.systemd import SystemdHost
from agent_run.runtime.execution import ProcessExitUnknown
from agent_run.core.kernel import Kernel
from agent_run.model import AgentCall, Execution, Step
from agent_run.errors import KernelError
from agent_run.core.operation_contract import OperationBinding
from agent_run.core.operation_runner import OperationRunner
from agent_run.runtime.handoff import managed_host_environment
from agent_run.adapters.remote import RemoteEffects
from agent_run.core.prompts.role import render_role_prompt


def command(database: Path, action: str, runner_lock: Path,
            environment: Mapping[str, str]) -> tuple[str, ...]:
    """Seal the selected interpreter and its SQLite loader path before launch.

    Preserve the venv executable spelling: resolving its symlink would select
    another environment. Only the non-secret dynamic-library path enters argv;
    the complete terminal environment still uses the private one-shot carrier.
    """
    if (not database.is_absolute() or not runner_lock.is_absolute()
            or not action or len(action) > 256 or "\0" in action
            or not Path(sys.executable).is_absolute()):
        raise ValueError("An exact native Host command is required")
    arguments = (sys.executable, "-m", "agent_run.runtime.executor", "--database", str(database),
                 "--action", action, "--runner-lock", str(runner_lock))
    library = environment.get("LD_LIBRARY_PATH", "")
    if "\0" in library:
        raise ValueError("Invalid captured library path")
    return ("/usr/bin/env", "LD_LIBRARY_PATH=" + library, *arguments) if library else arguments


class _Executor:
    """Launch exact allocated calls; Core owns operation selection and grants."""
    def __init__(self, kernel: Kernel, runner: OperationRunner, environment: Mapping[str, str]) -> None:
        self.kernel, self.runner = kernel, runner
        self.environment = dict(environment)

    def agent(self, call: AgentCall, cancelled: threading.Event) -> None:
        try:
            prompt = render_role_prompt(call)
        except Exception as error:
            if self.kernel.dispatch_agent(call):
                self.kernel.agent_unavailable(call, "worker_preparation_failed", {"error_type": type(error).__name__})
            return
        from agent_run.runtime.codex import execute_selected_codex
        binding = self.runner.binding
        # Keep the native credential/sandbox/process/raw/exit hooks. Bootstrap
        # authorization executes inline here, never in a successor pool task.
        execute_selected_codex(
            self.kernel, call, identity=binding.inputs.worker_identity, environment=self.environment,
            prompt=prompt, hidden_paths=binding.hidden_paths, hidden_environment=binding.hidden_environment,
            authorize_source=lambda: self.runner.authorize_agent(call), cancelled=cancelled,
        )


def _make_ports(binding: OperationBinding) -> RemoteEffects:
    return RemoteEffects(binding.repository_root, binding.repository,
                         runner_identity=binding.inputs.runner_identity, remote=binding.inputs.repository_remote)


def run(kernel: Kernel, execution: Execution, environment: Mapping[str, str]) -> int:
    """Supervise physical calls and one bounded Core operation independently."""
    from agent_run.notifications.sender import NativeNotifications
    runner: OperationRunner = OperationRunner(kernel, execution, _make_ports)
    runner.activate_observation()
    driver = _Executor(kernel, runner, environment)
    state = runner.state()
    workers: dict[str, tuple[AgentCall, Future[None], threading.Event]] = {}
    operation: Future[Step] | None = None
    notifications = NativeNotifications(kernel.storage.path, kernel.storage.path.parent)
    drain_notifications = False
    try:
        with kernel.executor_scope(execution) as close_admission, \
                ThreadPoolExecutor(max_workers=state.concurrency, thread_name_prefix="agent-run") as agents, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-run-io") as io:
            try:
                while True:
                    if operation is not None and operation.done():
                        step = operation.result()
                        operation = None
                        if step.call is not None:
                            # Hand off an allocated call before state, observer,
                            # or another potentially slow operation is touched.
                            call = step.call
                            if call.lease_id in workers:
                                raise KernelError("duplicate_host_call")
                            cancelled = threading.Event()
                            workers[call.lease_id] = (call, agents.submit(driver.agent, call, cancelled), cancelled)
                    for lease, (call, future, _) in list(workers.items()):
                        if not future.done():
                            continue
                        failure: Exception | None = None
                        try:
                            future.result()
                        except Exception as error:
                            failure = error
                        # Future completion releases no durable lease. Even a
                        # successful Python return requires exact Core evidence.
                        if isinstance(failure, ProcessExitUnknown) or not runner.worker_settled(call):
                            raise ProcessExitUnknown("Worker outcome was not durably settled") from failure
                        if failure is not None:
                            print(bounded_error(str(failure)), file=sys.stderr)
                        del workers[lease]
                    state = runner.state()
                    for lease in state.cancel:
                        active = workers.get(lease)
                        if active is not None:
                            active[2].set()
                    notifications.observe_committed()
                    if operation is None and not workers and state.disposition in {"quiescent", "complete"}:
                        drain_notifications = True
                        return 0
                    now = kernel.clock()
                    ready = (state.disposition == "ready" or state.disposition == "waiting"
                             and state.wake_at is not None and state.wake_at <= now)
                    if operation is None and ready:
                        operation = io.submit(runner.run_once, agent_capacity=max(0, state.concurrency-len(workers)))
                    delay = 1.0 if state.wake_at is None else min(1.0, max(0.01, state.wake_at-now))
                    # The heterogeneous collection is used only for readiness;
                    # results are consumed through their typed handles above.
                    pending: list[Future[Any]] = [future for _, future, _ in workers.values()]
                    if operation is not None:
                        pending.append(operation)
                    if pending:
                        wait(pending, timeout=delay, return_when=FIRST_COMPLETED)
                    else:
                        time.sleep(delay)
            except BaseException as error:
                close_admission(error)
                raise
            finally:
                # Set before the pools wait. Native hooks retain physical exit
                # evidence; Host never confirms exit by dropping a Future.
                for _, _, cancelled in workers.values():
                    cancelled.set()
    finally:
        notifications.close(drain=drain_notifications)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exact admitted native Host entry")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--runner-lock", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    try:
        credential_directory, notify_socket = os.environ.get("CREDENTIALS_DIRECTORY"), os.environ.get("NOTIFY_SOCKET")
        if not credential_directory or not notify_socket:
            raise KernelError("managed_host_required")
        kernel = Kernel(parsed.database)
        request = kernel.host_request(parsed.action)
        manager = SystemdHost(systemctl="systemctl", systemd_run="systemd-run", environment=os.environ)
        with managed_host_environment(
            kernel, parsed.action, manager, runner_lock=parsed.runner_lock,
            credential=Path(credential_directory) / "agent-run-environment", notify_socket=notify_socket,
        ) as inherited:
            # Runner/PAT/App helpers consume process environment. Restore the
            # exact admitted terminal values once, before starting any threads;
            # the user-manager's environment is not an identity fallback.
            os.environ.clear()
            os.environ.update(inherited.environment)
            execution = Execution(request.run_id, request.owner, request.epoch)
            return run(kernel, execution, inherited.environment)
    except (OSError, ValueError, RuntimeError) as error:
        print(bounded_error(str(error)), file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(bounded_error(note), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
