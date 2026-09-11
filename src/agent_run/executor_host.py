"""Small Executor Host seams used by the lifecycle controller and tests.

The production Linux host adapter is intentionally outside Ticket #188.  The
fixture host supports deterministic in-process and forked execution while
recording exact ownership in the task control record.
"""

from __future__ import annotations

import codecs
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from agent_run.state import SimulatedProcessCrash
from agent_run.task_control import TaskControlError, TaskControlStore, TaskKey

HostStatus = Literal[
    "absent",
    "reserved",
    "starting",
    "running",
    "exited",
    "unknown",
    "conflict",
]
_STDERR_CHUNK_BYTES = 64 * 1024


class ExecutorHostError(TaskControlError):
    """The Executor Host could not establish an exact ownership result."""


class ExecutorStartUnknownError(ExecutorHostError):
    """The launch outcome is unknown; a second Executor must not be started."""


class ExecutorLostError(ExecutorHostError):
    """An Executor disappeared before its lifecycle action was closed."""


class ExecutorAgentInterruptedError(ExecutorHostError):
    """The Executor's Agent invocation was interrupted after handoff."""


@dataclass(frozen=True)
class ExecutorSpec:
    task: TaskKey
    action_id: str
    run_id: str | None
    generation: int
    command: tuple[str, ...] = ()
    cwd: Path | None = None
    environment: Mapping[str, str] | None = None
    state_root: Path | None = None
    runner_binding: str | None = None


@dataclass(frozen=True)
class HostObservation:
    status: HostStatus
    generation: int | None
    pid: int | None
    handshake: bool
    reason: str | None = None
    runner_binding: str | None = None


class ExecutorHost(Protocol):
    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        recover: bool = False,
    ) -> HostObservation: ...

    def inspect(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation: ...

    def observe(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation: ...

    def cleanup_startup(self, spec: ExecutorSpec) -> None: ...

    def terminate_control_target(
        self, target_executor: Mapping[str, Any], *, timeout: float = 1.0
    ) -> None: ...


def _bind_execute_callback(
    execute: Callable[[], Mapping[str, Any]],
    spec: ExecutorSpec,
    generation: int,
) -> None:
    binder = getattr(execute, "bind_executor", None)
    if callable(binder):
        binder(spec.action_id, generation, spec.run_id)


def _relay_stderr(source_fd: int, target_fd: int) -> None:
    """Drain Executor stderr through fixed-size pipe buffers.

    If the observing CLI leaves, the relay keeps draining and discards future
    output so the detached Executor cannot become coupled to the terminal by
    pipe backpressure.
    """

    forwarding = True
    try:
        while chunk := os.read(source_fd, _STDERR_CHUNK_BYTES):
            if not forwarding:
                continue
            remaining = memoryview(chunk)
            while remaining:
                try:
                    written = os.write(target_fd, remaining)
                except BrokenPipeError:
                    forwarding = False
                    os.close(target_fd)
                    break
                remaining = remaining[written:]
    finally:
        os.close(source_fd)
        if forwarding:
            os.close(target_fd)


def _forward_available_stderr(
    source_fd: int, decoder: codecs.IncrementalDecoder
) -> bool:
    """Forward every available chunk and report whether the relay closed."""

    while True:
        try:
            chunk = os.read(source_fd, _STDERR_CHUNK_BYTES)
        except BlockingIOError:
            return False
        if not chunk:
            return True
        sys.stderr.write(decoder.decode(chunk))
        sys.stderr.flush()


class FakeExecutorHost:
    """Deterministic host for the public CLI and contract tests.

    ``start_outcome="unknown"`` models a host manager that accepted a launch
    request but lost its response.  In that case this adapter leaves the
    ``starting`` record in place and never invokes the business callback.
    """

    def __init__(
        self,
        *,
        start_outcome: Literal["accepted", "unknown"] = "accepted",
        separate_process: bool = False,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if start_outcome not in {"accepted", "unknown"}:
            raise ValueError("start_outcome must be accepted or unknown")
        self.start_outcome = start_outcome
        self.separate_process = separate_process
        self.fault_hook = fault_hook
        self.start_count = 0

    def _checkpoint(self, name: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name)

    def terminate_control_target(
        self, target_executor: Mapping[str, Any], *, timeout: float = 1.0
    ) -> None:
        _terminate_control_target(target_executor, timeout=timeout)

    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        recover: bool = False,
    ) -> HostObservation:
        reservation = control.begin_executor(
            spec.task,
            action_id=spec.action_id,
            run_id=spec.run_id,
            reclaim=recover,
        )
        if not reservation.created:
            observation = self.inspect(spec, control)
            if observation.status in {"unknown", "running", "starting"}:
                return observation
            if recover and observation.status in {"absent", "exited"}:
                control.mark_executor_absent(
                    spec.task,
                    action_id=spec.action_id,
                    generation=reservation.generation,
                )
                reservation = control.begin_executor(
                    spec.task,
                    action_id=spec.action_id,
                    run_id=spec.run_id,
                    reclaim=True,
                )
            else:
                raise ExecutorStartUnknownError(
                    "Executor 已有收口或不可确认的旧 ownership；不会启动第二个 Executor"
                )

        self.start_count += 1
        self._checkpoint("host_accepted")
        if self.start_outcome == "unknown":
            return HostObservation(
                status="unknown",
                generation=reservation.generation,
                pid=None,
                handshake=False,
                reason="host launch response was lost",
            )
        if execute is not None:
            _bind_execute_callback(execute, spec, reservation.generation)
        if self.separate_process and execute is not None:
            return self._execute_separately(
                spec,
                control,
                execute,
                generation=reservation.generation,
            )

        try:
            pid = os.getpid()
            start_token = _process_start_token(pid)
            control.mark_process_started(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                pid=pid,
                process_start_token=start_token,
            )
            self._checkpoint("pre_handshake")
            control.mark_handshake(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                pid=pid,
                process_start_token=start_token,
            )
            self._checkpoint("post_handshake")
            if execute is None:
                return HostObservation(
                    status="running",
                    generation=reservation.generation,
                    pid=pid,
                    handshake=True,
                )
            control.assert_executor_current(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                run_id=spec.run_id,
            )
            result = execute()
            result_status = result.get("status")
            status = result_status if isinstance(result_status, str) else None
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                result_status=status,
            )
            return HostObservation(
                status="exited",
                generation=reservation.generation,
                pid=pid,
                handshake=True,
            )
        except KeyboardInterrupt:
            # The client and Executor share a process in this test adapter.
            # Once the Executor has handshaken, an interrupt is an observed
            # Executor failure and must release the Action slot.  Preserve the
            # interrupt if this best-effort cleanup itself cannot complete.
            try:
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=reservation.generation,
                    failure="executor_interrupted",
                )
            except TaskControlError:
                pass
            raise
        except SimulatedProcessCrash:
            # Fault injection models an abrupt process exit.  The real host
            # would not get a chance to convert that crash into an Action
            # failure, so leave the ownership record unresolved for the next
            # command to reconcile fail-closed.
            raise
        except BaseException as error:
            try:
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=reservation.generation,
                    failure=str(error),
                )
            except TaskControlError:
                pass
            raise

    def _execute_separately(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]],
        *,
        generation: int,
    ) -> HostObservation:
        """Run a fixture Executor outside the observing CLI process."""

        executor_stderr_read, executor_stderr_write = os.pipe()
        try:
            observer_stderr_read, observer_stderr_write = os.pipe()
        except BaseException:
            os.close(executor_stderr_read)
            os.close(executor_stderr_write)
            raise
        try:
            relay_pid = os.fork()
        except BaseException:
            os.close(executor_stderr_read)
            os.close(executor_stderr_write)
            os.close(observer_stderr_read)
            os.close(observer_stderr_write)
            raise
        if relay_pid == 0:  # pragma: no branch - relay exits in every path
            try:
                os.setsid()
                os.close(executor_stderr_write)
                os.close(observer_stderr_read)
                null_fd = os.open(os.devnull, os.O_RDWR)
                try:
                    for descriptor in (0, 1, 2):
                        os.dup2(null_fd, descriptor)
                finally:
                    if null_fd > 2:
                        os.close(null_fd)
                _relay_stderr(executor_stderr_read, observer_stderr_write)
                os._exit(0)
            except BaseException:
                os._exit(1)

        try:
            pid = os.fork()
        except BaseException:
            os.close(executor_stderr_read)
            os.close(executor_stderr_write)
            os.close(observer_stderr_read)
            os.close(observer_stderr_write)
            os.waitpid(relay_pid, 0)
            raise

        if pid == 0:  # pragma: no branch - child process exits in every path
            try:
                os.close(executor_stderr_read)
                os.close(observer_stderr_read)
                os.close(observer_stderr_write)
                os.setsid()
                null_fd = os.open(os.devnull, os.O_RDWR)
                try:
                    for descriptor in (0, 1):
                        os.dup2(null_fd, descriptor)
                    os.dup2(executor_stderr_write, 2)
                finally:
                    if null_fd > 2:
                        os.close(null_fd)
                    if executor_stderr_write > 2:
                        os.close(executor_stderr_write)
                start_token = _process_start_token(os.getpid())
                control.mark_process_started(
                    spec.task,
                    action_id=spec.action_id,
                    generation=generation,
                    pid=os.getpid(),
                    process_start_token=start_token,
                )
                control.mark_handshake(
                    spec.task,
                    action_id=spec.action_id,
                    generation=generation,
                    pid=os.getpid(),
                    process_start_token=start_token,
                )
                control.assert_executor_current(
                    spec.task,
                    action_id=spec.action_id,
                    generation=generation,
                    run_id=spec.run_id,
                )
                result = execute()
                result_status = result.get("status")
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=generation,
                    result_status=(
                        result_status if isinstance(result_status, str) else None
                    ),
                )
                os._exit(0)
            except SimulatedProcessCrash:
                # Model an abrupt Executor death: no Python cleanup and no
                # Task Control transition may run after the injected crash.
                os._exit(86)
            except KeyboardInterrupt:
                try:
                    control.finish_executor(
                        spec.task,
                        action_id=spec.action_id,
                        generation=generation,
                        failure="executor_agent_interrupted",
                    )
                finally:
                    os._exit(130)
            except BaseException as error:
                try:
                    control.finish_executor(
                        spec.task,
                        action_id=spec.action_id,
                        generation=generation,
                        failure=str(error),
                    )
                finally:
                    os._exit(1)

        os.close(executor_stderr_read)
        os.close(executor_stderr_write)
        os.close(observer_stderr_write)
        stderr_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        child_status: int | None = None
        stderr_finished = False
        try:
            os.set_blocking(observer_stderr_read, False)
            while child_status is None or not stderr_finished:
                stderr_finished = _forward_available_stderr(
                    observer_stderr_read, stderr_decoder
                )
                if child_status is None:
                    waited_pid, waited_status = os.waitpid(pid, os.WNOHANG)
                    if waited_pid == pid:
                        child_status = waited_status
                if child_status is None or not stderr_finished:
                    time.sleep(0.01)
        finally:
            os.close(observer_stderr_read)

        _, relay_status = os.waitpid(relay_pid, 0)
        sys.stderr.write(stderr_decoder.decode(b"", final=True))
        sys.stderr.flush()
        if not os.WIFEXITED(relay_status) or os.WEXITSTATUS(relay_status) != 0:
            raise ExecutorHostError("Fixture Executor stderr 中继未正常收口")
        assert child_status is not None
        if os.WIFEXITED(child_status) and os.WEXITSTATUS(child_status) == 130:
            raise ExecutorAgentInterruptedError(
                "Executor Agent invocation was interrupted"
            )
        observation = self.inspect(spec, control)
        if not os.WIFEXITED(child_status) or os.WEXITSTATUS(child_status) != 0:
            control.mark_executor_absent(
                spec.task,
                action_id=spec.action_id,
                generation=generation,
            )
            raise ExecutorHostError(
                observation.reason or "Fixture Executor 未正常收口"
            )
        return observation

    def inspect(self, spec: ExecutorSpec, control: TaskControlStore) -> HostObservation:
        record = control.load(spec.task)
        if record is None:
            return HostObservation(
                status="absent", generation=None, pid=None, handshake=False
            )
        executor = record.get("executor")
        if not isinstance(executor, dict):
            return HostObservation(
                status="absent", generation=None, pid=None, handshake=False
            )
        generation = executor.get("generation")
        generation_value = generation if type(generation) is int else None
        pid = executor.get("pid")
        pid_value = pid if type(pid) is int else None
        if (
            executor.get("action_id") != spec.action_id
            or (spec.run_id is not None and executor.get("run_id") != spec.run_id)
            or generation_value != spec.generation
        ):
            return HostObservation(
                status="unknown",
                generation=generation_value,
                pid=pid_value,
                handshake=executor.get("handshake_at") is not None,
                reason="Executor binding does not match the requested generation",
            )
        status = executor.get("status")
        handshake = isinstance(executor.get("handshake_at"), str)
        if status == "absent":
            return HostObservation(
                status="absent",
                generation=generation_value,
                pid=pid_value,
                handshake=handshake,
            )
        if status == "exited":
            return HostObservation(
                status="exited",
                generation=generation_value,
                pid=pid_value,
                handshake=handshake,
            )
        if status == "starting" and pid_value is None:
            return HostObservation(
                status="unknown",
                generation=generation_value,
                pid=None,
                handshake=False,
                reason="Executor launch has no verifiable process binding",
            )
        if pid_value is None:
            return HostObservation(
                status="unknown",
                generation=generation_value,
                pid=None,
                handshake=handshake,
                reason="Executor process binding is missing",
            )
        process_status = _process_binding_status(
            pid_value, executor.get("process_start_token")
        )
        if process_status == "matches":
            return HostObservation(
                status="running" if status == "running" else "starting",
                generation=generation_value,
                pid=pid_value,
                handshake=handshake,
            )
        if process_status == "absent":
            return HostObservation(
                status="absent",
                generation=generation_value,
                pid=pid_value,
                handshake=handshake,
                reason="bound Executor process is no longer present",
            )
        return HostObservation(
            status="unknown",
            generation=generation_value,
            pid=pid_value,
            handshake=handshake,
            reason="cannot prove the recorded process binding",
        )

    def observe(self, spec: ExecutorSpec, control: TaskControlStore) -> HostObservation:
        """Inspect ownership without changing Task Control or Host state."""

        return self.inspect(spec, control)

    def cleanup_startup(self, spec: ExecutorSpec) -> None:
        del spec


class BoundExecutorHost:
    """Executor-side adapter that takes over one pre-reserved generation."""

    def __init__(self, *, action_id: str, generation: int) -> None:
        self.action_id = action_id
        self.generation = generation

    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        recover: bool = False,
    ) -> HostObservation:
        del recover
        self._validate(spec)
        if execute is None:
            raise ExecutorHostError("Executor 没有绑定业务执行入口")
        pid = os.getpid()
        start_token = _process_start_token(pid)
        try:
            control.mark_process_started(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                pid=pid,
                process_start_token=start_token,
            )
            control.mark_handshake(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                pid=pid,
                process_start_token=start_token,
            )
            control.assert_executor_current(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                run_id=spec.run_id,
            )
            result = execute()
            result_status = result.get("status")
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                result_status=(
                    result_status if isinstance(result_status, str) else None
                ),
            )
            return HostObservation("exited", spec.generation, pid, True)
        except BaseException as error:
            try:
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    failure=str(error),
                )
            except TaskControlError:
                pass
            raise

    def inspect(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation:
        self._validate(spec)
        record = control.load(spec.task)
        executor = record.get("executor") if isinstance(record, dict) else None
        if not isinstance(executor, dict):
            return HostObservation(
                "conflict", None, None, False, "Executor binding 缺失"
            )
        if (
            executor.get("action_id") != self.action_id
            or executor.get("generation") != self.generation
        ):
            return HostObservation(
                "conflict", self.generation, None, False, "Executor binding 不匹配"
            )
        return HostObservation("reserved", self.generation, os.getpid(), False)

    def observe(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation:
        return self.inspect(spec, control)

    def cleanup_startup(self, spec: ExecutorSpec) -> None:
        self._validate(spec)

    def terminate_control_target(
        self, target_executor: Mapping[str, Any], *, timeout: float = 1.0
    ) -> None:
        _terminate_control_target(target_executor, timeout=timeout)

    def _validate(self, spec: ExecutorSpec) -> None:
        if spec.action_id != self.action_id or spec.generation != self.generation:
            raise ExecutorHostError("Executor 不能接管另一个 Action/generation")


def _process_start_token(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _process_binding_status(
    pid: int, expected_start_token: object
) -> Literal["matches", "absent", "unknown"]:
    current = _process_start_token(pid)
    if isinstance(expected_start_token, str) and current is not None:
        return "matches" if current == expected_start_token else "absent"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    return "unknown"


def _terminate_control_target(
    target_executor: Mapping[str, Any], *, timeout: float = 1.0
) -> None:
    """Terminate only the process identity captured by a Stop/Abandon fence.

    A Worker is a separate process group and is always preferred.  When the
    Executor is in external supervision or Publisher work, no Worker exists;
    then the exact Executor process is terminated so the fenced session ends
    without waiting for another polling boundary.
    """

    worker = target_executor.get("worker")
    if isinstance(worker, Mapping):
        pid = worker.get("pid")
        token = worker.get("process_start_token")
        if type(pid) is not int or not isinstance(token, str) or not token:
            raise ExecutorStartUnknownError("Worker ownership 无法确认；不会终止进程")
        status = _process_binding_status(pid, token)
        if status == "matches":
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_for_process_exit(pid, token, timeout=timeout)
            return
        if status == "unknown":
            raise ExecutorStartUnknownError("Worker ownership 无法确认；不会终止进程")

    pid = target_executor.get("pid")
    token = target_executor.get("process_start_token")
    if type(pid) is not int or not isinstance(token, str) or not token:
        raise ExecutorStartUnknownError("Executor ownership 无法确认；不会终止进程")
    status = _process_binding_status(pid, token)
    if status == "absent":
        return
    if status != "matches":
        raise ExecutorStartUnknownError("Executor ownership 无法确认；不会终止进程")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + min(0.2, max(0.01, timeout))
    while time.monotonic() < deadline and _process_is_live_binding(pid, token):
        time.sleep(0.01)
    if _process_is_live_binding(pid, token):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_for_process_exit(pid, token, timeout=timeout)


def validate_executor_exit(target_executor: Mapping[str, Any]) -> None:
    """Require independent process proof before admitting an explicit Resume."""
    for label, process in (
        ("Worker", target_executor.get("worker")),
        ("Executor", target_executor),
    ):
        if process is None and label == "Worker":
            continue
        if not isinstance(process, Mapping):
            raise ExecutorStartUnknownError(f"{label} ownership 无法确认")
        pid, token = process.get("pid"), process.get("process_start_token")
        if type(pid) is not int or not isinstance(token, str) or not token:
            raise ExecutorStartUnknownError(f"{label} 退出证据不足；不会启动第二个 Executor")
        status = _process_binding_status(pid, token)
        if status == "matches":
            raise ExecutorStartUnknownError(f"{label} 仍在运行；请先执行 stop")
        if status != "absent":
            raise ExecutorStartUnknownError(f"{label} ownership 无法确认；不会启动第二个 Executor")


def validate_control_target(target_executor: Mapping[str, Any]) -> None:
    """Fail closed unless the Worker or Executor target has exact live identity."""

    worker = target_executor.get("worker")
    if isinstance(worker, Mapping):
        worker_pid = worker.get("pid")
        worker_token = worker.get("process_start_token")
        if type(worker_pid) is int and isinstance(worker_token, str) and worker_token:
            status = _process_binding_status(worker_pid, worker_token)
            if status == "matches":
                return
            if status == "unknown":
                raise ExecutorStartUnknownError(
                    "Worker ownership 无法确认；不会提交控制栅栏"
                )
    pid = target_executor.get("pid")
    token = target_executor.get("process_start_token")
    if type(pid) is not int or not isinstance(token, str) or not token:
        raise ExecutorStartUnknownError(
            "Executor ownership 无法确认；不会提交控制栅栏"
        )
    if _process_binding_status(pid, token) != "matches":
        raise ExecutorStartUnknownError(
            "Executor ownership 无法确认；不会提交控制栅栏"
        )


def _wait_for_process_exit(pid: int, token: str, *, timeout: float) -> None:
    deadline = time.monotonic() + max(0.01, timeout)
    while time.monotonic() < deadline:
        if not _process_is_live_binding(pid, token):
            return
        time.sleep(0.01)
    if _process_is_live_binding(pid, token):
        raise ExecutorStartUnknownError("已发送终止信号，但进程退出结果无法确认")


def _process_is_live_binding(pid: int, token: str) -> bool:
    if _process_binding_status(pid, token) != "matches":
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return False
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    return bool(fields) and fields[0] != "Z"
