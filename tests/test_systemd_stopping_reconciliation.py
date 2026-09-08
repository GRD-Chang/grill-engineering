from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.executor_host import ExecutorSpec
from agent_run.systemd_executor_host import (
    SubprocessSystemdTransport,
    SystemdUserExecutorHost,
)
from agent_run.task_control import ActionBusyError, TaskControlStore, TaskKey


@pytest.mark.parametrize("substate", ["stop-sigterm", "stop-sigkill"])
@pytest.mark.parametrize("terminal", ["inactive", "failed"])
def test_stopping_executor_requires_terminal_host_proof_before_successor(
    tmp_path: Path, substate: str, terminal: str
) -> None:
    # Exercise the production parser using a local executable, never user systemd.
    response = tmp_path / "systemctl-output"
    executable = tmp_path / "systemctl"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "assert sys.argv[1:3] == ['--user', 'show']\n"
        f"sys.stdout.write(pathlib.Path({str(response)!r}).read_text())\n"
    )
    executable.chmod(0o700)
    transport = SubprocessSystemdTransport()
    transport.systemctl = str(executable)
    transport.journalctl = None
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={},
        executor_python=Path(sys.executable),
    )
    task = TaskKey(tmp_path, "owner/repo", 156)
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    action_id = claim.action["action_id"]
    reservation = control.begin_executor(
        task, action_id=action_id, run_id="run-156", runner_binding="a" * 16
    )
    spec = ExecutorSpec(
        task=task,
        action_id=action_id,
        generation=reservation.generation,
        run_id="run-156",
        command=("run", "156"),
        cwd=tmp_path,
        state_root=tmp_path / "state",
        runner_binding="a" * 16,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        control.mark_handshake(
            task,
            action_id=action_id,
            generation=spec.generation,
            pid=process.pid,
            process_start_token=None,
        )
        description = host._description(spec)
        response.write_text(
            f"ActiveState=deactivating\nSubState={substate}\n"
            f"ExecMainPID={process.pid}\nDescription={description}\nResult=success\n"
        )
        before = control.path_for(task).read_bytes()
        for _ in range(2):
            # Observation stays strictly read-only; reconciliation also cannot
            # close the Action while the original process is still stopping.
            assert host.observe(spec, control).status == "unknown"
            assert host.inspect(spec, control).status == "unknown"
            with pytest.raises(ActionBusyError):
                control.claim_action(task, kind="resume", payload={"run_id": "run-156"})
            assert control.path_for(task).read_bytes() == before
            assert process.poll() is None

        process.terminate()
        process.wait(timeout=5)
        response.write_text(
            f"ActiveState={terminal}\nSubState=dead\nExecMainPID=0\n"
            f"Description={description}\nResult=signal\n"
        )
        assert host.observe(spec, control).status == "exited"
        assert control.path_for(task).read_bytes() == before
        assert host.inspect(spec, control).status == "exited"
        closed = control.path_for(task).read_bytes()
        record = control.load(task)
        assert record is not None
        assert record["action"]["status"] == "failed"
        assert record["executor"]["status"] == "exited"
        assert host.inspect(spec, control).status == "exited"
        assert control.path_for(task).read_bytes() == closed
        successor = control.claim_action(
            task, kind="resume", payload={"run_id": "run-156"}
        )
        assert successor.action is not None
        assert successor.action["executor_generation"] == spec.generation + 1
        admitted = control.path_for(task).read_bytes()
        attached = control.claim_action(
            task, kind="resume", payload={"run_id": "run-156"}
        )
        assert attached.attached
        assert control.path_for(task).read_bytes() == admitted
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
