from __future__ import annotations

from collections.abc import Sequence
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.executor_environment import (
    DEFAULT_MAX_ENVIRONMENT_BYTES,
    EnvironmentCarrierError,
    capture_executor_environment,
    cleanup_expired_carriers,
    consume_environment_carrier,
    write_environment_carrier,
)
from agent_run.executor_host import ExecutorSpec
from agent_run.runner_lease import (
    RunnerLeaseBusy,
    runner_management_lease,
    runner_usage_lease,
)
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdStartResult,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
    _run_bounded,
)
from agent_run.task_control import (
    ActionReconciliationError,
    TaskControlStore,
    TaskKey,
)


def _spec(
    tmp_path: Path, *, generation: int = 1, run_id: str | None = None
) -> ExecutorSpec:
    return ExecutorSpec(
        task=TaskKey(tmp_path / "repo", "owner/repo", 189),
        action_id="action-1",
        run_id=run_id,
        generation=generation,
        command=("run", "189"),
        cwd=tmp_path / "repo",
        state_root=tmp_path / "custom-state",
    )


def _admit(control: TaskControlStore, spec: ExecutorSpec) -> ExecutorSpec:
    control.claim_action(spec.task, kind="run", payload={"parent": 189})
    record = control.load(spec.task)
    assert record is not None
    action = record["action"]
    assert isinstance(action, dict)
    action_id = action["action_id"]
    assert isinstance(action_id, str)
    return replace(spec, action_id=action_id)


def test_environment_carrier_is_bounded_private_exact_and_single_use(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, run_id="run-1")
    captured = capture_executor_environment(
        {"PATH": "/project/bin:/usr/bin", "VIRTUAL_ENV": "/project/.venv"},
        command=spec.command,
        max_bytes=4096,
    )
    carrier = write_environment_carrier(tmp_path / "runtime", spec, captured)

    assert carrier.stat().st_mode & 0o777 == 0o600
    restored, command = consume_environment_carrier(carrier, spec)
    assert restored["PATH"] == "/project/bin:/usr/bin"
    assert restored["VIRTUAL_ENV"] == "/project/.venv"
    assert command == spec.command
    assert not carrier.exists()
    with pytest.raises(EnvironmentCarrierError, match="不存在"):
        consume_environment_carrier(carrier, spec)


def test_environment_carrier_rejects_oversize_before_creating_a_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(EnvironmentCarrierError, match="过大"):
        capture_executor_environment(
            {"PATH": "x" * 1024}, command=("run", "189"), max_bytes=128
        )
    assert list(tmp_path.iterdir()) == []


def test_wrong_generation_cannot_consume_environment_carrier(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    carrier = write_environment_carrier(
        tmp_path / "runtime",
        spec,
        capture_executor_environment({"PATH": "/bin"}, command=spec.command),
    )

    with pytest.raises(EnvironmentCarrierError, match="binding"):
        consume_environment_carrier(carrier, _spec(tmp_path, generation=2))
    assert carrier.exists()
    consume_environment_carrier(carrier, spec)


def test_wrong_state_root_cannot_consume_environment_carrier(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    carrier = write_environment_carrier(
        tmp_path / "runtime",
        spec,
        capture_executor_environment({"PATH": "/bin"}, command=spec.command),
    )

    with pytest.raises(EnvironmentCarrierError, match="binding"):
        consume_environment_carrier(
            carrier, replace(spec, state_root=tmp_path / "other-state")
        )
    assert carrier.exists()
    consume_environment_carrier(carrier, spec)


def test_oversize_carrier_is_never_read_without_a_limit(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    carrier = runtime / "environment-corrupt.json"
    carrier.write_bytes(b"x" * (DEFAULT_MAX_ENVIRONMENT_BYTES + 1))
    carrier.chmod(0o600)

    with pytest.raises(EnvironmentCarrierError, match="过大"):
        consume_environment_carrier(carrier, _spec(tmp_path))
    cleanup_expired_carriers(runtime)
    assert not carrier.exists()


def test_systemd_host_accepts_launch_without_putting_environment_in_unit_metadata(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, run_id="run-1")
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={
            "PATH": "/secret/project/bin",
            "PUBLISHER_TOKEN": "secret",
            "PYTHONHOME": "/untrusted/python",
        },
        executor_python=Path("/usr/bin/python3"),
    )
    host.check_readiness(command=spec.command)

    observation = host.ensure(spec, control)

    assert observation.status == "starting"
    assert transport.start_count == 1
    launch = transport.launches[0]
    rendered = json.dumps(launch, default=str)
    assert "PUBLISHER_TOKEN" not in rendered
    assert "/secret/project/bin" not in rendered
    assert "/untrusted/python" not in rendered
    command = launch["command"]
    assert isinstance(command, tuple)
    assert Path(command[0]).name == "env"
    assert command[1:3] == ("-i", "PYTHONNOUSERSITE=1")
    assert "-I" in command
    assert command[command.index("--run-id") + 1] == "run-1"
    assert command[command.index("--state-root") + 1] == str(spec.state_root)
    assert "Restart=no" in launch["properties"]
    assert "Type=exec" in launch["properties"]
    assert launch["scope"] == "user"
    assert launch["unit"].endswith(".service")
    assert ":run-1:" in launch["description"]
    assert ":runner-" in launch["description"]
    assert len(transport.cleanups) == 1
    cleanup = transport.cleanups[0]
    assert cleanup["delay_seconds"] == 30.0
    cleanup_paths = cleanup["paths"]
    assert isinstance(cleanup_paths, tuple)
    assert cleanup_paths[0].exists()
    transport.fire_cleanups()
    assert not any(path.exists() for path in cleanup_paths)


def test_environment_carrier_has_exactly_one_successful_consumer(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    carrier = write_environment_carrier(
        tmp_path / "runtime",
        spec,
        capture_executor_environment({"PATH": "/bin"}, command=spec.command),
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def consume() -> None:
        barrier.wait()
        try:
            consume_environment_carrier(carrier, spec)
        except EnvironmentCarrierError:
            outcomes.append("rejected")
        else:
            outcomes.append("consumed")

    workers = [threading.Thread(target=consume) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert sorted(outcomes) == ["consumed", "rejected"]


def test_internal_executor_consumes_carrier_and_calls_original_cli(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.task.workspace.mkdir(parents=True)
    carrier = write_environment_carrier(
        tmp_path / "runtime",
        spec,
        capture_executor_environment(
            {
                "PATH": os.environ["PATH"],
                "AGENT_RUN_EXECUTOR_ACTION_ID": "spoofed",
            },
            command=spec.command,
        ),
    )
    lock = tmp_path / "runner" / "install.lock"
    script = (
        "import json, os, sys; import agent_run.cli as cli; "
        "import agent_run.executor as executor; "
        "cli.main = lambda args: (print(json.dumps({"
        "'args': args, 'action': os.environ['AGENT_RUN_EXECUTOR_ACTION_ID'], "
        "'state_root': os.environ['AGENT_RUN_INTERNAL_STATE_ROOT']})) or 0); "
        "raise SystemExit(executor.main(sys.argv[1:]))"
    )
    process_environment = os.environ.copy()
    process_environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
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
            str(spec.state_root),
            "--carrier",
            str(carrier),
            "--runner-lock",
            str(lock),
        ],
        text=True,
        capture_output=True,
        env=process_environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "action": spec.action_id,
        "args": list(spec.command),
        "state_root": str(spec.state_root),
    }
    assert not carrier.exists()


def test_internal_executor_consumes_carrier_bound_to_run(tmp_path: Path) -> None:
    spec = _spec(tmp_path, run_id="run-1")
    spec.task.workspace.mkdir(parents=True)
    carrier = write_environment_carrier(
        tmp_path / "runtime",
        spec,
        capture_executor_environment(
            {"PATH": os.environ["PATH"]}, command=spec.command
        ),
    )
    lock = tmp_path / "runner" / "install.lock"
    script = (
        "import sys; import agent_run.cli as cli; import agent_run.executor as executor; "
        "cli.main = lambda args: 0; raise SystemExit(executor.main(sys.argv[1:]))"
    )
    process_environment = os.environ.copy()
    process_environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
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
            "--run-id",
            "run-1",
            "--state-root",
            str(spec.state_root),
            "--carrier",
            str(carrier),
            "--runner-lock",
            str(lock),
        ],
        text=True,
        capture_output=True,
        env=process_environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not carrier.exists()


def test_observer_does_not_fail_reservation_before_unit_is_visible(
    tmp_path: Path,
) -> None:
    class PausingTransport(FakeSystemdTransport):
        def __init__(self) -> None:
            super().__init__()
            self.owner_waiting = threading.Event()
            self.release_owner = threading.Event()
            self._paused = False

        def inspect(self, unit: str) -> SystemdUnitObservation:
            if threading.current_thread().name == "owner" and not self._paused:
                self._paused = True
                self.owner_waiting.set()
                assert self.release_owner.wait(timeout=5)
            return super().inspect(unit)

    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = PausingTransport()
    owner = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    observer = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    owner.check_readiness(command=spec.command)
    outcome: list[object] = []

    thread = threading.Thread(
        name="owner", target=lambda: outcome.append(owner.ensure(spec, control))
    )
    thread.start()
    assert transport.owner_waiting.wait(timeout=5)

    observation = observer.inspect(spec, control)
    record = control.load(spec.task)

    assert observation.status == "unknown"
    assert record is not None
    assert record["action"]["status"] == "accepted"
    assert record["executor"]["status"] == "starting"

    transport.release_owner.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert transport.start_count == 1


def test_observer_closes_absent_reservation_after_startup_owner_crashes(
    tmp_path: Path,
) -> None:
    class CrashingTransport(FakeSystemdTransport):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        def inspect(self, unit: str) -> SystemdUnitObservation:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("owner crashed before launch")
            return super().inspect(unit)

    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = CrashingTransport()
    owner = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    observer = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    owner.check_readiness(command=spec.command)

    with pytest.raises(RuntimeError, match="owner crashed"):
        owner.ensure(spec, control)

    observation = observer.inspect(spec, control)
    record = control.load(spec.task)

    assert observation.status == "absent"
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert record["executor"]["status"] == "exited"


def test_lease_guardian_holds_parent_lease_until_deadline_cleanup(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport()
    lock = tmp_path / "runner" / "install.lock"

    with runner_usage_lease(lock) as lease:
        host = SystemdUserExecutorHost(
            transport=transport,
            runtime_directory=tmp_path / "runtime",
            environment={"PATH": "/bin", "XDG_DATA_HOME": str(tmp_path / "runner")},
            executor_python=Path(sys.executable),
            runner_lease_fd=lease.fileno(),
        )
        host.check_readiness(command=spec.command)
        host.ensure(spec, control)

    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(lock):
            pass
    transport.fire_cleanups()
    deadline = time.monotonic() + 5
    while True:
        try:
            with runner_management_lease(lock):
                break
        except RunnerLeaseBusy:
            if time.monotonic() >= deadline:
                pytest.fail("lease guardian did not release after carrier cleanup")
            time.sleep(0.05)


def test_systemd_host_fails_closed_on_unavailable_manager_before_action(
    tmp_path: Path,
) -> None:
    transport = FakeSystemdTransport(available=False)
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    control = TaskControlStore(tmp_path / "control")
    spec = _spec(tmp_path)

    with pytest.raises(Exception, match="systemd"):
        host.check_readiness()
    assert control.load(spec.task) is None
    assert not (tmp_path / "runtime").exists()


def test_systemd_host_rejects_oversize_environment_before_action(
    tmp_path: Path,
) -> None:
    host = SystemdUserExecutorHost(
        transport=FakeSystemdTransport(),
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "x" * 1024},
        executor_python=Path("/usr/bin/python3"),
        max_environment_bytes=128,
    )
    control = TaskControlStore(tmp_path / "control")
    spec = _spec(tmp_path)

    host.check_readiness()
    with pytest.raises(Exception, match="过大"):
        control.claim_action(
            spec.task,
            kind="run",
            payload={"parent": 189},
            before_create=lambda: host.prepare_environment(spec.command),
        )
    assert control.load(spec.task) is None
    assert not (tmp_path / "runtime").exists()


def test_systemd_host_rejects_oversize_environment_before_executor_reservation(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    before = control.path_for(spec.task).read_bytes()
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "x" * 1024},
        executor_python=Path("/usr/bin/python3"),
        max_environment_bytes=128,
    )

    with pytest.raises(Exception, match="过大"):
        host.ensure(spec, control)

    assert control.path_for(spec.task).read_bytes() == before
    assert transport.start_count == 0
    assert not list((tmp_path / "runtime").glob("environment-*.json"))


def test_systemd_host_reports_binding_conflict_without_replacing_unit(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport(
        unit=SystemdUnitObservation(
            status="running",
            description="agent-run-executor:other-binding",
            pid=os.getpid(),
            reason=None,
        )
    )
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    observation = host.ensure(spec, control)

    assert observation.status == "conflict"
    assert transport.start_count == 0
    assert not list((tmp_path / "runtime").glob("*.json"))
    record = control.load(spec.task)
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert record["executor"]["status"] == "exited"


def test_systemd_host_does_not_treat_transient_inspection_failure_as_conflict(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport(
        unit=SystemdUnitObservation(
            status="unknown",
            description=None,
            pid=None,
            reason="systemctl timed out",
        )
    )
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    observation = host.ensure(spec, control)
    record = control.load(spec.task)

    assert observation.status == "unknown"
    assert observation.reason == "systemctl timed out"
    assert transport.start_count == 0
    assert record is not None
    assert record["action"]["status"] == "accepted"
    assert record["executor"]["status"] == "starting"


def test_systemd_host_closes_original_generation_after_executor_signal(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )
    host.check_readiness(command=spec.command)
    host.ensure(spec, control)
    transport.unit = SystemdUnitObservation(
        "exited",
        str(transport.launches[0]["description"]),
        None,
        "signal:SIGTERM",
    )

    observation = host.inspect(spec, control)

    assert observation.status == "exited"
    record = control.load(spec.task)
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert record["executor"]["status"] == "exited"
    assert record["executor"]["generation"] == spec.generation
    assert record["executor"]["failure"] == "signal:SIGTERM"
    assert transport.start_count == 1
    assert not list((tmp_path / "runtime").glob("*.json"))
    with pytest.raises(ActionReconciliationError):
        host.ensure(spec, control)
    assert transport.start_count == 1


def test_systemd_host_closes_original_generation_when_handshake_is_lost(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    assert host.ensure(spec, control).status == "starting"
    description = str(transport.launches[0]["description"])
    transport.unit = SystemdUnitObservation(
        "running", description, 1234, None
    )
    still_waiting = host.inspect(spec, control)
    assert still_waiting.status == "starting"
    assert still_waiting.handshake is False

    transport.unit = SystemdUnitObservation(
        "exited", description, None, "executor-handshake-lost"
    )
    closed = host.inspect(spec, control)
    record = control.load(spec.task)

    assert closed.status == "exited"
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert record["executor"]["status"] == "exited"
    assert record["executor"]["generation"] == spec.generation
    assert record["executor"]["failure"] == "executor-handshake-lost"
    assert transport.start_count == 1
    assert not list((tmp_path / "runtime").glob("*.json"))
    with pytest.raises(ActionReconciliationError):
        host.ensure(spec, control)
    assert transport.start_count == 1


def test_systemd_host_bounds_journal_diagnostics_and_cleans_failed_launch(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport(
        start_error="launch rejected",
        journal_error="journal unavailable " + "x" * 100_000,
    )
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    with pytest.raises(Exception, match="launch rejected"):
        host.ensure(spec, control)
    record = control.load(spec.task)
    assert record is not None
    assert len(json.dumps(record, ensure_ascii=False).encode()) < 32 * 1024
    assert not list((tmp_path / "runtime").glob("*.json"))


def test_systemd_host_preserves_unknown_launch_until_deadline_reconciliation(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = FakeSystemdTransport(
        start_error="systemd-run response lost", start_unknown=True
    )
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    observation = host.ensure(spec, control)
    host.cleanup_startup(spec)
    record = control.load(spec.task)

    assert observation.status == "unknown"
    assert record is not None
    assert record["action"]["status"] == "accepted"
    assert record["executor"]["status"] == "starting"
    pending = list((tmp_path / "runtime").glob("launch-*.pending"))
    assert len(pending) == 1
    assert pending[0].stat().st_size == 0

    transport.fire_cleanups()
    reconciled = host.inspect(spec, control)
    record = control.load(spec.task)

    assert reconciled.status == "absent"
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert not list((tmp_path / "runtime").glob("launch-*.pending"))


def test_systemd_host_reconciles_response_loss_with_accepted_unit(
    tmp_path: Path,
) -> None:
    class AcceptedUnknownTransport(FakeSystemdTransport):
        def start(
            self,
            *,
            unit: str,
            description: str,
            cwd: Path,
            command: Sequence[str],
            properties: Sequence[str],
        ) -> SystemdStartResult:
            super().start(
                unit=unit,
                description=description,
                cwd=cwd,
                command=command,
                properties=properties,
            )
            return SystemdStartResult("unknown", "response lost")

    spec = _spec(tmp_path)
    spec.cwd.mkdir(parents=True)
    control = TaskControlStore(tmp_path / "control")
    spec = _admit(control, spec)
    transport = AcceptedUnknownTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "/bin"},
        executor_python=Path("/usr/bin/python3"),
    )

    observation = host.ensure(spec, control)
    host.cleanup_startup(spec)
    record = control.load(spec.task)

    assert observation.status == "starting"
    assert record is not None
    assert record["action"]["status"] == "accepted"
    assert not list((tmp_path / "runtime").glob("launch-*.pending"))
    cleanup_paths = transport.cleanups[0]["paths"]
    assert isinstance(cleanup_paths, tuple)
    assert cleanup_paths[0].exists()


def test_systemd_helper_drains_large_streams_with_bounded_capture() -> None:
    result = _run_bounded(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('o' * 2_000_000); "
                "sys.stderr.write('e' * 2_000_000)"
            ),
        ],
        max_output=8192,
    )

    assert result.returncode == 0
    assert result.stdout == "o" * 8192
    assert result.stderr == "e" * 8192
