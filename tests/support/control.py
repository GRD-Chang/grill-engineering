"""共享测试支撑：control；每次调用保持独立状态。"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from support.workspace import managed_repo, managed_state
from support.state import persist_run_fixture
from agent_run.executor_host import ExecutorSpec, FakeExecutorHost, HostObservation
from agent_run.run_lifecycle import RunLifecycle, prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey


def _run_cli(
    repo: Path, fixture: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_run",
            *arguments,
            "--json",
            "--github-fixture",
            str(fixture),
        ],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class _ControlReceiptHost(FakeExecutorHost):
    """Complete the fake Executor record while keeping observer semantics."""

    def __init__(
        self,
        *,
        before_failed_receipt: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.before_failed_receipt = before_failed_receipt

    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        recover: bool = False,
    ) -> HostObservation:
        del recover
        assert execute is not None
        reservation = control.begin_executor(
            spec.task,
            action_id=spec.action_id,
            run_id=spec.run_id,
        )
        control.mark_process_started(
            spec.task,
            action_id=spec.action_id,
            generation=reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        control.mark_handshake(
            spec.task,
            action_id=spec.action_id,
            generation=reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        try:
            result = execute()
        except BaseException as error:
            if self.before_failed_receipt is not None:
                self.before_failed_receipt()
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                failure=str(error),
            )
        else:
            result_status = result.get("status")
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                result_status=(
                    result_status if isinstance(result_status, str) else None
                ),
            )
        return HostObservation("running", reservation.generation, os.getpid(), True)


def _bind_running_executor(
    repo: Path, run_id: str
) -> tuple[TaskControlStore, TaskKey, subprocess.Popen[bytes]]:
    states = StateStore(managed_state(repo))
    task = TaskKey(managed_repo(repo), "example/project", 1)
    control = TaskControlStore(managed_state(repo))
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id=None, runner_binding="b" * 16
    )
    token = (
        Path(f"/proc/{os.getpid()}/stat")
        .read_text(encoding="ascii")
        .rsplit(")", 1)[1]
        .split()[19]
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=token,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=token,
    )
    control.bind_run(
        task,
        claim.action_id,
        run_id,
        generation=reservation.generation,
        state_dir=states.root,
    )
    run = states.load_run(run_id)
    assert run is not None
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    record = control.load(task)
    assert record is not None and isinstance(record.get("action"), dict)
    prepare_action_application_receipt(run, record["action"])
    persist_run_fixture(states, run_id, run)
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status=str(run["status"]),
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    worker_token = (
        Path(f"/proc/{worker.pid}/stat")
        .read_text(encoding="ascii")
        .rsplit(")", 1)[1]
        .split()[19]
    )
    control.mark_worker_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=worker.pid,
        process_start_token=worker_token,
    )
    return control, task, worker


def _running_executor(
    tmp_path: Path,
) -> tuple[TaskControlStore, TaskKey, dict[str, object]]:
    task = TaskKey(tmp_path / "repo", "owner/repo", 194)
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="run", payload={"parent": 194})
    assert claim.action_id is not None
    reservation = control.begin_executor(
        task,
        action_id=claim.action_id,
        run_id=None,
        runner_binding="a" * 16,
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.bind_run(
        task,
        claim.action_id,
        "run-194",
        generation=reservation.generation,
        state_dir=tmp_path / "state",
    )
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id="run-194",
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status="active",
    )
    record = control.load(task)
    assert record is not None
    return control, task, record


def _control_lifecycle(
    *,
    states: StateStore,
    control: TaskControlStore,
    task: TaskKey,
    current: dict[str, object],
    host: FakeExecutorHost,
) -> RunLifecycle:
    run_id = str(current["run_id"])
    return RunLifecycle(
        states=states,
        control=control,
        host=host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: (current, True),
        initialize_profile=None,
        executor_spec=lambda bound_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=bound_run_id,
            generation=generation,
            cwd=task.workspace,
            state_root=states.root,
        ),
        execute=lambda _run_id: current,
        execute_with_binding=lambda _run_id, _action_id, _generation: current,
    )
