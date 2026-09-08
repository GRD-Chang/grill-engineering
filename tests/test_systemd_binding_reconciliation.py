from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.executor_host import ExecutorSpec, ExecutorStartUnknownError
from agent_run.run_lifecycle import (
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
)
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
)
from agent_run.task_control import TaskControlStore, TaskKey
from test_run_lifecycle import _InMemoryRunState


@pytest.mark.parametrize("conflict", ["runner", "run", "generation", "state"])
@pytest.mark.parametrize("native_status", ["running", "exited"])
def test_binding_conflict_cannot_release_original_generation(
    git_repo: Path, tmp_path: Path, conflict: str, native_status: str
) -> None:
    task = TaskKey(git_repo, "example/project", 156)
    control = TaskControlStore(tmp_path / "control")
    states = _InMemoryRunState()
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None and claim.action_id is not None
    action_id = claim.action_id
    control.bind_run(task, action_id, "run-1")
    prepare_action_application_receipt(states.value, claim.action)
    control.record_application(
        task, action_id=action_id, run_id="run-1",
        payload_digest=str(claim.action["payload_digest"]),
    )
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport, runtime_directory=tmp_path / "runtime",
        environment={}, executor_python=Path(sys.executable),
    )
    reservation = control.begin_executor(
        task, action_id=action_id, run_id="run-1",
        runner_binding=host._runner_binding(),
    )
    spec = ExecutorSpec(
        task=task, action_id=action_id, run_id="run-1",
        generation=reservation.generation, command=("run", "156"), cwd=git_repo,
        state_root=tmp_path / "state", runner_binding=host._runner_binding(),
    )
    conflicting_spec = {
        "runner": replace(spec, runner_binding="f" * 16),
        "run": replace(spec, run_id="other-run"),
        "generation": replace(spec, generation=spec.generation + 1),
        "state": replace(spec, state_root=tmp_path / "other-state"),
    }[conflict]
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        control.mark_handshake(
            task, action_id=action_id, generation=spec.generation,
            pid=process.pid, process_start_token=None,
        )
        transport.unit = SystemdUnitObservation(
            native_status,  # type: ignore[arg-type]
            host._description(conflicting_spec), process.pid, None,
        )
        lifecycle = RunLifecycle(
            states=states,  # type: ignore[arg-type]
            control=control, host=host, task=task,
            preflight=lambda: dict(states.value),
            select_run=lambda _action: (dict(states.value), False),
            initialize_profile=None,
            executor_spec=lambda run_id, current_action_id, generation: replace(
                spec, run_id=run_id, action_id=current_action_id, generation=generation,
            ),
            execute=lambda _run_id: pytest.fail("must not dispatch Agent or Publisher"),
        )
        request = LifecycleRequest(task=task, kind="run", payload={"parent": 156})
        before = control.path_for(task).read_bytes()
        for _ in range(2):
            with pytest.raises(ExecutorStartUnknownError):
                lifecycle.submit(request)
            assert control.path_for(task).read_bytes() == before
            assert process.poll() is None
            assert transport.start_count == 0
        transport.unit = SystemdUnitObservation(
            "running", host._description(spec), process.pid, None,
        )
        observation = host.ensure(spec, control)
        assert observation.status == "running"
        assert observation.generation == spec.generation
        assert transport.start_count == 0
        process.terminate()
        process.wait(timeout=5)
        transport.unit = SystemdUnitObservation(
            "exited", host._description(spec), None, "signal",
        )
        assert host.inspect(spec, control).status == "exited"
        closed = control.path_for(task).read_bytes()
        assert host.inspect(spec, control).status == "exited"
        assert control.path_for(task).read_bytes() == closed
        successor = control.claim_action(
            task, kind="resume", payload={"run_id": "run-1"},
        )
        assert successor.action is not None
        assert successor.action["executor_generation"] == spec.generation + 1
        assert control.claim_action(
            task, kind="resume", payload={"run_id": "run-1"},
        ).attached
        assert transport.start_count == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
