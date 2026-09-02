"""Linux user-systemd adapter for one transient Executor Session."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import shutil
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

from agent_run.error_safety import bounded_error
from agent_run.executor_environment import (
    DEFAULT_CARRIER_TTL_SECONDS,
    CapturedExecutorEnvironment,
    EnvironmentCarrierError,
    capture_executor_environment,
    claimed_environment_carrier_path,
    cleanup_expired_carriers,
    write_environment_carrier,
)
from agent_run.executor_host import (
    ExecutorHostError,
    ExecutorSpec,
    HostObservation,
    _terminate_control_target,
)
from agent_run.runner_lease import default_runner_lock_path
from agent_run.task_control import TaskControlStore


class SystemdExecutionReadinessError(ExecutorHostError):
    """The Linux user service manager cannot host an Executor."""


@dataclass(frozen=True)
class SystemdUnitObservation:
    status: Literal["absent", "starting", "running", "exited", "unknown"]
    description: str | None
    pid: int | None
    reason: str | None


@dataclass(frozen=True)
class SystemdStartResult:
    status: Literal["accepted", "rejected", "unknown"]
    reason: str | None = None


class SystemdTransport(Protocol):
    def check_available(self) -> tuple[bool, str | None]: ...

    def start(
        self,
        *,
        unit: str,
        description: str,
        cwd: Path,
        command: Sequence[str],
        properties: Sequence[str],
    ) -> SystemdStartResult: ...

    def inspect(self, unit: str) -> SystemdUnitObservation: ...

    def journal(self, unit: str) -> str | None: ...

    def schedule_cleanup(
        self, *, unit: str, paths: Sequence[Path], delay_seconds: float
    ) -> str | None: ...


class SubprocessSystemdTransport:
    """Small bounded subprocess transport; no shell and no inherited payload."""

    def __init__(self) -> None:
        self.systemd_run = shutil.which("systemd-run")
        self.systemctl = shutil.which("systemctl")
        self.journalctl = shutil.which("journalctl")
        self.rm = shutil.which("rm")
        self._journal_cache: dict[str, str | None] = {}

    def check_available(self) -> tuple[bool, str | None]:
        if self.systemd_run is None or self.systemctl is None or self.rm is None:
            return False, "systemd-run、systemctl 或 rm 不可用"
        result = _run_bounded(
            [self.systemctl, "--user", "show-environment"], max_output=4096
        )
        if result.returncode != 0:
            return False, result.stderr or "user systemd manager 不可用"
        return True, None

    def start(
        self,
        *,
        unit: str,
        description: str,
        cwd: Path,
        command: Sequence[str],
        properties: Sequence[str],
    ) -> SystemdStartResult:
        assert self.systemd_run is not None
        arguments = [
            self.systemd_run,
            "--user",
            f"--unit={unit}",
            "--collect",
            f"--description={description}",
            f"--working-directory={cwd}",
        ]
        arguments.extend(f"--property={property_value}" for property_value in properties)
        arguments.extend(["--", *command])
        result = _run_bounded(arguments, max_output=8192)
        if result.returncode == 0:
            return SystemdStartResult("accepted")
        return SystemdStartResult(
            "unknown", result.stderr or "systemd-run 启动结果未知"
        )

    def inspect(self, unit: str) -> SystemdUnitObservation:
        if self.systemctl is None:
            return SystemdUnitObservation(
                "unknown", None, None, "systemctl 不可用"
            )
        result = _run_bounded(
            [
                self.systemctl,
                "--user",
                "show",
                unit,
                "--property=ActiveState",
                "--property=SubState",
                "--property=ExecMainPID",
                "--property=Description",
                "--property=Result",
            ],
            max_output=16 * 1024,
        )
        if result.returncode in {4, 5}:
            return SystemdUnitObservation("absent", None, None, None)
        if result.returncode != 0:
            return SystemdUnitObservation(
                "unknown", None, None, result.stderr or "systemctl show 失败"
            )
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                fields[key] = value
        active = fields.get("ActiveState")
        substate = fields.get("SubState")
        status: Literal["absent", "starting", "running", "exited", "unknown"]
        if active in {"active", "reloading"}:
            status = "running" if substate == "running" else "starting"
        elif active in {"inactive", "failed", "deactivating"}:
            status = "exited"
        elif active == "activating":
            status = "starting"
        else:
            status = "unknown"
        raw_pid = fields.get("ExecMainPID")
        pid = int(raw_pid) if raw_pid and raw_pid.isdigit() and int(raw_pid) > 0 else None
        reason = fields.get("Result") or None
        if status in {"exited", "unknown"}:
            reason = reason or self.journal(unit)
        return SystemdUnitObservation(
            status, fields.get("Description") or None, pid, reason
        )

    def journal(self, unit: str) -> str | None:
        if unit not in self._journal_cache:
            self._journal_cache[unit] = self._read_journal(unit)
        return self._journal_cache[unit]

    def schedule_cleanup(
        self, *, unit: str, paths: Sequence[Path], delay_seconds: float
    ) -> str | None:
        assert self.systemd_run is not None
        assert self.rm is not None
        result = _run_bounded(
            [
                self.systemd_run,
                "--user",
                f"--unit={unit}",
                "--collect",
                f"--on-active={delay_seconds}s",
                "--timer-property=AccuracySec=1s",
                "--",
                self.rm,
                "-f",
                "--",
                *(str(path) for path in paths),
            ],
            max_output=8192,
        )
        return None if result.returncode == 0 else result.stderr or "清理 timer 创建失败"

    def _read_journal(self, unit: str) -> str | None:
        if self.journalctl is None:
            return "journalctl 不可用"
        result = _run_bounded(
            [
                self.journalctl,
                "--user",
                "--unit",
                unit,
                "--no-pager",
                "--lines=20",
                "--output=cat",
            ],
            max_output=8192,
        )
        if result.returncode != 0:
            return result.stderr or "journal unavailable"
        return result.stdout or None


class FakeSystemdTransport:
    """Deterministic contract transport; never talks to the current user manager."""

    def __init__(
        self,
        *,
        available: bool = True,
        unit: SystemdUnitObservation | None = None,
        start_error: str | None = None,
        start_unknown: bool = False,
        journal_error: str | None = None,
        cleanup_error: str | None = None,
    ) -> None:
        self.available = available
        self.unit = unit or SystemdUnitObservation("absent", None, None, None)
        self.start_error = start_error
        self.start_unknown = start_unknown
        self.journal_error = journal_error
        self.cleanup_error = cleanup_error
        self.start_count = 0
        self.launches: list[dict[str, object]] = []
        self.cleanups: list[dict[str, object]] = []

    def check_available(self) -> tuple[bool, str | None]:
        return self.available, None if self.available else "user systemd unavailable"

    def start(
        self,
        *,
        unit: str,
        description: str,
        cwd: Path,
        command: Sequence[str],
        properties: Sequence[str],
    ) -> SystemdStartResult:
        self.start_count += 1
        self.launches.append(
            {
                "scope": "user",
                "unit": unit,
                "description": description,
                "cwd": cwd,
                "command": tuple(command),
                "properties": tuple(properties),
            }
        )
        if self.start_error is None:
            self.unit = SystemdUnitObservation(
                "starting", description, None, None
            )
            return SystemdStartResult("accepted")
        return SystemdStartResult(
            "unknown" if self.start_unknown else "rejected", self.start_error
        )

    def inspect(self, unit: str) -> SystemdUnitObservation:
        return self.unit

    def journal(self, unit: str) -> str | None:
        return self.journal_error

    def schedule_cleanup(
        self, *, unit: str, paths: Sequence[Path], delay_seconds: float
    ) -> str | None:
        self.cleanups.append(
            {"unit": unit, "paths": tuple(paths), "delay_seconds": delay_seconds}
        )
        return self.cleanup_error

    def fire_cleanups(self) -> None:
        for cleanup in self.cleanups:
            paths = cleanup["paths"]
            assert isinstance(paths, tuple)
            for path in paths:
                assert isinstance(path, Path)
                path.unlink(missing_ok=True)


class SystemdUserExecutorHost:
    """Start an exact generation in a transient user service."""

    _PROPERTIES = ("Type=exec", "Restart=no", "StandardInput=null")

    def __init__(
        self,
        *,
        runtime_directory: Path,
        environment: Mapping[str, str],
        executor_python: Path,
        transport: SystemdTransport | None = None,
        max_environment_bytes: int = 1024 * 1024,
        runner_lease_fd: int | None = None,
    ) -> None:
        self.transport = transport or SubprocessSystemdTransport()
        self.runtime_directory = Path(runtime_directory)
        self.executor_python = Path(executor_python).resolve()
        self.environment: Mapping[str, str] | None = environment
        self.max_environment_bytes = max_environment_bytes
        self.runner_lease_fd = runner_lease_fd
        self._env_program = Path(
            shutil.which("env", path=os.defpath) or "/usr/bin/env"
        )
        self._captured: CapturedExecutorEnvironment | None = None
        self._carriers: dict[tuple[str, str, int], Path] = {}
        self._accepted_launches: set[tuple[str, str, int]] = set()

    def check_readiness(self, *, command: Sequence[str] | None = None) -> None:
        available, reason = self.transport.check_available()
        if not available:
            raise SystemdExecutionReadinessError(
                "Runner Execution Readiness 不满足：" + (reason or "user systemd 不可用")
            )
        if not self._env_program.is_file() or not os.access(self._env_program, os.X_OK):
            raise SystemdExecutionReadinessError(
                "Runner Execution Readiness 不满足：env 不可用"
            )
        if command is not None:
            self.prepare_environment(command)
        cleanup_expired_carriers(self.runtime_directory)

    def prepare_environment(self, command: Sequence[str]) -> None:
        """Capture one new Executor session before durable admission."""

        try:
            self._capture(tuple(command))
        except EnvironmentCarrierError as error:
            raise SystemdExecutionReadinessError(str(error)) from error

    def terminate_control_target(
        self, target_executor: Mapping[str, object], *, timeout: float = 1.0
    ) -> None:
        """Terminate the exact Linux process identity captured by the fence."""

        _terminate_control_target(target_executor, timeout=timeout)

    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: object | None = None,
        recover: bool = False,
    ) -> HostObservation:
        del execute
        self._state_root(spec)
        startup_lease = self._acquire_startup_lease(spec, control)
        if startup_lease is None:
            self._release_capture()
            return self.inspect(spec, control)
        try:
            current_runner_binding = self._runner_binding()
            reservation = control.begin_executor(
                spec.task,
                action_id=spec.action_id,
                run_id=spec.run_id,
                reclaim=recover,
                runner_binding=current_runner_binding,
                before_create=lambda: self.prepare_environment(spec.command),
            )
            if reservation.generation != spec.generation:
                spec = replace(spec, generation=reservation.generation)
            executor = reservation.record.get("executor")
            reserved_runner_binding = (
                executor.get("runner_binding")
                if isinstance(executor, Mapping)
                else None
            )
            spec = replace(
                spec,
                runner_binding=(
                    reserved_runner_binding
                    if isinstance(reserved_runner_binding, str)
                    else current_runner_binding
                ),
            )
            if not reservation.created:
                self._release_capture()
                control.assert_executor_current(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    run_id=spec.run_id,
                )
                return self._inspect(spec, control)
            control.assert_executor_current(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                run_id=spec.run_id,
            )
            native = self.transport.inspect(self._unit(spec))
            if native.status not in {"absent", "exited"}:
                self._release_capture()
                observation = self._from_native(spec, native)
                if observation.status == "conflict":
                    self._finish_terminal(spec, control, observation.reason)
                return observation
            try:
                control.assert_executor_current(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    run_id=spec.run_id,
                )
                captured = self._capture(spec.command)
                runner_lock = default_runner_lock_path(captured.environment)
                control.assert_executor_current(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    run_id=spec.run_id,
                )
                carrier = write_environment_carrier(
                    self.runtime_directory, spec, captured
                )
            except (OSError, EnvironmentCarrierError) as error:
                self._release_capture()
                failure = bounded_error(str(error))
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    failure=failure,
                )
                raise ExecutorHostError(failure) from error
            self._release_capture()
            key = self._key(spec)
            self._carriers[key] = carrier
            control.assert_executor_current(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                run_id=spec.run_id,
            )
            cleanup_error = self.transport.schedule_cleanup(
                unit=self._cleanup_unit(spec),
                paths=(
                    carrier,
                    claimed_environment_carrier_path(carrier),
                    self._launch_pending_path(spec),
                ),
                delay_seconds=DEFAULT_CARRIER_TTL_SECONDS,
            )
            if cleanup_error is not None:
                self.cleanup_startup(spec)
                failure = bounded_error(cleanup_error)
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    failure=failure,
                )
                raise ExecutorHostError(failure)
            try:
                control.assert_executor_current(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    run_id=spec.run_id,
                )
                self._mark_launch_pending(spec)
                self._start_lease_guardian(carrier)
            except (OSError, ExecutorHostError) as error:
                self._launch_pending_path(spec).unlink(missing_ok=True)
                self.cleanup_startup(spec)
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    failure=str(error),
                )
                raise
            control.assert_executor_current(
                spec.task,
                action_id=spec.action_id,
                generation=spec.generation,
                run_id=spec.run_id,
            )
            start_result = self.transport.start(
                unit=self._unit(spec),
                description=self._description(spec),
                cwd=spec.cwd or spec.task.workspace,
                command=self._executor_command(spec, carrier, runner_lock),
                properties=self._PROPERTIES,
            )
            if start_result.status == "rejected":
                self._launch_pending_path(spec).unlink(missing_ok=True)
                self.cleanup_startup(spec)
                diagnostic = self.transport.journal(self._unit(spec))
                failure = bounded_error(
                    (start_result.reason or "systemd-run 拒绝启动")
                    + (f"; {diagnostic}" if diagnostic else "")
                )
                control.finish_executor(
                    spec.task,
                    action_id=spec.action_id,
                    generation=spec.generation,
                    failure=failure,
                )
                raise ExecutorHostError(failure)
            if start_result.status == "unknown":
                observation = self._inspect(spec, control)
                if observation.status == "unknown" and start_result.reason:
                    return replace(
                        observation,
                        reason=bounded_error(start_result.reason),
                    )
                return observation
            self._launch_pending_path(spec).unlink(missing_ok=True)
            self._accepted_launches.add(self._key(spec))
            return self._inspect(spec, control)
        finally:
            os.close(startup_lease)

    def inspect(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation:
        if self._key(spec) in self._accepted_launches:
            return self._inspect(spec, control)
        startup_lease = self._acquire_startup_lease(spec, control)
        if startup_lease is None:
            return HostObservation(
                "unknown",
                spec.generation,
                None,
                False,
                "Executor startup ownership 仍由另一进程持有",
            )
        try:
            return self._inspect(spec, control)
        finally:
            os.close(startup_lease)

    def observe(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> HostObservation:
        """Read exact systemd ownership without cleanup or control writes."""

        spec = self._bind_historical_runner(spec, control)
        observation = self._from_native(spec, self.transport.inspect(self._unit(spec)))
        if (
            observation.status == "absent"
            and self._launch_pending_path(spec).exists()
        ):
            return HostObservation(
                "unknown",
                spec.generation,
                observation.pid,
                False,
                "systemd 启动结果尚未确认",
            )
        return observation

    def _inspect(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
    ) -> HostObservation:
        spec = self._bind_historical_runner(spec, control)
        native = self.transport.inspect(self._unit(spec))
        observation = self._from_native(spec, native)
        if observation.status in {"starting", "running"}:
            key = self._key(spec)
            self._accepted_launches.add(key)
            self._launch_pending_path(spec).unlink(missing_ok=True)
        if observation.status in {"absent", "unknown"} and self._launch_pending_path(
            spec
        ).exists():
            return HostObservation(
                "unknown",
                spec.generation,
                observation.pid,
                False,
                observation.reason or "systemd 启动结果尚未确认",
            )
        if observation.status in {"absent", "exited", "conflict"}:
            key = self._key(spec)
            self._accepted_launches.discard(key)
            self._launch_pending_path(spec).unlink(missing_ok=True)
            self.cleanup_startup(spec)
            self._finish_terminal(spec, control, observation.reason)
        if observation.status == "running":
            record = control.load(spec.task)
            executor = record.get("executor") if isinstance(record, dict) else None
            handshake = isinstance(executor, dict) and isinstance(
                executor.get("handshake_at"), str
            )
            if handshake:
                self._accepted_launches.discard(self._key(spec))
                self.cleanup_startup(spec)
            return HostObservation(
                "running" if handshake else "starting",
                spec.generation,
                observation.pid,
                handshake,
                observation.reason,
            )
        return observation

    def _acquire_startup_lease(
        self, spec: ExecutorSpec, control: TaskControlStore
    ) -> int | None:
        path = self._startup_lease_path(spec, control)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _startup_lease_path(spec: ExecutorSpec, control: TaskControlStore) -> Path:
        return control.path_for(spec.task).with_suffix(".executor-startup.lock")

    def cleanup_startup(self, spec: ExecutorSpec) -> None:
        self._release_capture()
        key = self._key(spec)
        if self._launch_pending_path(spec).exists() or key in self._accepted_launches:
            return
        self._accepted_launches.discard(key)
        path = self._carriers.pop(key, None)
        if path is not None:
            path.unlink(missing_ok=True)

    def _mark_launch_pending(self, spec: ExecutorSpec) -> None:
        path = self._launch_pending_path(spec)
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _launch_pending_path(self, spec: ExecutorSpec) -> Path:
        return self.runtime_directory / (
            f"launch-{spec.task.fingerprint[:16]}-{spec.action_id}-"
            f"{spec.generation}.pending"
        )

    def _capture(self, command: tuple[str, ...]) -> CapturedExecutorEnvironment:
        captured = self._captured
        if captured is not None:
            if captured.command != command:
                raise SystemdExecutionReadinessError(
                    "Executor 命令与已验证的环境交接不匹配"
                )
            return captured
        if self.environment is None:
            raise SystemdExecutionReadinessError("Executor 环境交接已经释放")
        captured = capture_executor_environment(
            self.environment,
            command=command,
            max_bytes=self.max_environment_bytes,
        )
        self.environment = None
        self._captured = captured
        return captured

    def _release_capture(self) -> None:
        self.environment = None
        self._captured = None

    def _start_lease_guardian(self, carrier: Path) -> None:
        if self.runner_lease_fd is None:
            return
        try:
            guardian = subprocess.Popen(
                [
                    str(self._env_program),
                    "-i",
                    "PYTHONNOUSERSITE=1",
                    str(self.executor_python),
                    "-I",
                    "-m",
                    "agent_run.runner_lease",
                    "guard",
                    str(carrier),
                    str(DEFAULT_CARRIER_TTL_SECONDS),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(self.runner_lease_fd,),
                start_new_session=True,
            )
        except OSError as error:
            raise ExecutorHostError("无法建立 Runner 租约交接") from error
        threading.Thread(target=guardian.wait, daemon=True).start()

    def _from_native(
        self, spec: ExecutorSpec, native: SystemdUnitObservation
    ) -> HostObservation:
        if native.status != "absent" and native.description is None:
            return HostObservation(
                "unknown",
                spec.generation,
                native.pid,
                False,
                bounded_error(native.reason or "systemd unit binding 无法确认"),
            )
        observed_runner_binding = None
        if native.status != "absent":
            observed_runner_binding = self._runner_binding_from_description(
                spec, native.description
            )
        if native.status != "absent" and observed_runner_binding is None:
            return HostObservation(
                "conflict",
                spec.generation,
                native.pid,
                False,
                "systemd unit binding 与当前 execution generation 不匹配",
            )
        if (
            native.status != "absent"
            and spec.runner_binding is not None
            and observed_runner_binding != spec.runner_binding
        ):
            return HostObservation(
                "conflict",
                spec.generation,
                native.pid,
                False,
                "systemd unit Runner binding 与原 execution generation 不匹配",
            )
        return HostObservation(
            native.status,
            spec.generation if native.status != "absent" else None,
            native.pid,
            False,
            bounded_error(native.reason) if native.reason else None,
            observed_runner_binding,
        )

    @staticmethod
    def _bind_historical_runner(
        spec: ExecutorSpec, control: TaskControlStore
    ) -> ExecutorSpec:
        record = control.load(spec.task)
        executor = record.get("executor") if isinstance(record, Mapping) else None
        if not isinstance(executor, Mapping):
            return spec
        if (
            executor.get("action_id") != spec.action_id
            or executor.get("generation") != spec.generation
            or executor.get("run_id") not in {None, spec.run_id}
        ):
            return spec
        runner_binding = executor.get("runner_binding")
        return (
            replace(spec, runner_binding=runner_binding)
            if isinstance(runner_binding, str)
            else spec
        )

    @staticmethod
    def _finish_terminal(
        spec: ExecutorSpec,
        control: TaskControlStore,
        reason: str | None,
    ) -> None:
        record = control.load(spec.task)
        action = record.get("action") if isinstance(record, dict) else None
        if not isinstance(action, dict) or action.get("status") not in {
            "accepted",
            "applying",
        }:
            return
        control.finish_executor(
            spec.task,
            action_id=spec.action_id,
            generation=spec.generation,
            failure=bounded_error(
                reason or "Executor host terminated before Action completion"
            ),
        )

    def _executor_command(
        self, spec: ExecutorSpec, carrier: Path, runner_lock: Path
    ) -> tuple[str, ...]:
        command = [
            str(self._env_program),
            "-i",
            "PYTHONNOUSERSITE=1",
            str(self.executor_python),
            "-I",
            "-m",
            "agent_run.executor",
            "--workspace",
            str(spec.task.workspace),
            "--repository",
            spec.task.repository,
            "--parent",
            str(spec.task.parent_number),
            "--action-id",
            spec.action_id,
            "--generation",
            str(spec.generation),
            "--state-root",
            str(self._state_root(spec)),
        ]
        if spec.run_id is not None:
            command.extend(("--run-id", spec.run_id))
        command.extend(
            (
                "--carrier",
                str(carrier),
                "--runner-lock",
                str(runner_lock),
            )
        )
        return tuple(command)

    @staticmethod
    def _key(spec: ExecutorSpec) -> tuple[str, str, int]:
        return spec.task.fingerprint, spec.action_id, spec.generation

    @staticmethod
    def _unit(spec: ExecutorSpec) -> str:
        return (
            f"agent-run-{spec.task.fingerprint[:32]}-"
            f"{spec.action_id[:16]}-{spec.generation}.service"
        )

    @staticmethod
    def _cleanup_unit(spec: ExecutorSpec) -> str:
        return (
            f"agent-run-env-cleanup-{spec.task.fingerprint[:24]}-"
            f"{spec.action_id[:12]}-{spec.generation}"
        )

    def _description(self, spec: ExecutorSpec) -> str:
        runner_binding = spec.runner_binding or self._runner_binding()
        state_binding = hashlib.sha256(
            str(self._state_root(spec)).encode("utf-8")
        ).hexdigest()[:16]
        return (
            f"agent-run-executor:{spec.task.fingerprint}:"
            f"{spec.action_id}:{spec.run_id or '-'}:{spec.generation}:"
            f"runner-{runner_binding}:state-{state_binding}"
        )

    def _runner_binding_from_description(
        self, spec: ExecutorSpec, description: str | None
    ) -> str | None:
        if description is None:
            return None
        state_binding = hashlib.sha256(
            str(self._state_root(spec)).encode("utf-8")
        ).hexdigest()[:16]
        prefix = (
            f"agent-run-executor:{spec.task.fingerprint}:"
            f"{spec.action_id}:{spec.run_id or '-'}:{spec.generation}:runner-"
        )
        suffix = f":state-{state_binding}"
        if not description.startswith(prefix) or not description.endswith(suffix):
            return None
        runner_binding = description[len(prefix) : -len(suffix)]
        if len(runner_binding) != 16 or any(
            character not in "0123456789abcdef" for character in runner_binding
        ):
            return None
        return runner_binding

    def _runner_binding(self) -> str:
        return hashlib.sha256(
            str(self.executor_python).encode("utf-8")
        ).hexdigest()[:16]

    @staticmethod
    def _state_root(spec: ExecutorSpec) -> Path:
        if spec.state_root is None or not spec.state_root.is_absolute():
            raise ExecutorHostError("Executor state root 必须是绝对路径")
        return spec.state_root


def execution_readiness(
    transport: SystemdTransport | None = None,
) -> dict[str, object]:
    selected = transport or SubprocessSystemdTransport()
    available, reason = selected.check_available()
    return {
        "status": "ok" if available else "unavailable",
        "host": "systemd-user",
        "linger_required": False,
        "reason": bounded_error(reason) if reason else None,
    }


def observe_systemd_executor(
    spec: ExecutorSpec,
    control: TaskControlStore,
    *,
    runtime_directory: Path,
    executor_python: Path,
    transport: SystemdTransport | None = None,
) -> HostObservation:
    """Observe one exact production Host binding without reconciliation writes."""

    host = SystemdUserExecutorHost(
        runtime_directory=runtime_directory,
        environment={},
        executor_python=executor_python,
        transport=transport,
    )
    return host.observe(spec, control)


@dataclass(frozen=True)
class _BoundedResult:
    returncode: int
    stdout: str
    stderr: str


def _run_bounded(arguments: list[str], *, max_output: int) -> _BoundedResult:
    try:
        process = subprocess.Popen(
            arguments,
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return _BoundedResult(1, "", bounded_error(str(error)))

    stdout = bytearray()
    stderr = bytearray()

    def drain(stream: object, destination: bytearray) -> None:
        fileno = getattr(stream, "fileno", None)
        if not callable(fileno):
            return
        try:
            descriptor = fileno()
            while chunk := os.read(descriptor, 64 * 1024):
                destination.extend(chunk)
                if len(destination) > max_output:
                    del destination[:-max_output]
        except OSError:
            return

    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        returncode = process.wait()
    finally:
        for reader in readers:
            reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
    decoded_stdout = bytes(stdout).decode("utf-8", errors="replace")
    decoded_stderr = bytes(stderr).decode("utf-8", errors="replace")
    if timed_out:
        decoded_stderr = bounded_error(
            decoded_stderr + "; systemd helper timed out"
        )
    return _BoundedResult(returncode, decoded_stdout, decoded_stderr)
