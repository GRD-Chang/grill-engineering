from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from support.workspace import managed_repo, managed_state

from agent_run.executor_host import ExecutorSpec, FakeExecutorHost, HostObservation
from agent_run.run_lifecycle import (
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
)
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from conftest import seed_run, write_fixture


@pytest.mark.parametrize("action_completed", [True, False])
@pytest.mark.parametrize("final_status", [
    "completed", "run_approval_pending", "ready_for_human", "parent_delivery_pending",
])
@pytest.mark.parametrize("close_action", [True, False])
@pytest.mark.parametrize("initial_status", ["parent_delivery_pending", "waiting_checks"])
def test_attach_preserves_executor_completion_at_host_inspection(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_completed: bool,
    final_status: str,
    close_action: bool,
    initial_status: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = state["run_id"]
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    control = TaskControlStore(states.root)
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action is not None and claim.action_id is not None
    action_id = claim.action_id
    control.bind_run(task, action_id, run_id)
    state["status"] = initial_status
    prepare_action_application_receipt(state, claim.action)
    states.save_run(run_id, state)
    control.record_application(task, action_id=action_id, run_id=run_id,
                               payload_digest=claim.action["payload_digest"])
    reservation = control.begin_executor(task, action_id=action_id, run_id=run_id)
    control.mark_process_started(task, action_id=action_id, generation=reservation.generation,
                                pid=os.getpid(), process_start_token=None)
    control.mark_handshake(task, action_id=action_id, generation=reservation.generation,
                           pid=os.getpid(), process_start_token=None)
    if action_completed:
        control.complete_action(task, action_id=action_id, result_status=state["status"])
    inspected = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    final_bytes: list[bytes] = []

    def finish_at_barrier() -> None:
        try:
            assert inspected.wait(3)
            latest = states.load_current_run(run_id)
            assert latest is not None
            latest["status"] = final_status
            latest["terminal_kind"] = (
                "completed" if final_status == "completed" else
                None if final_status == "parent_delivery_pending" else "waiting_human"
            )
            states.save_run(run_id, latest)
            if close_action:
                control.finish_executor(task, action_id=action_id, generation=reservation.generation,
                                        result_status=final_status)
            else:
                control.mark_executor_absent(task, action_id=action_id, generation=reservation.generation)
            final_bytes.append((states.root / "runs" / f"{run_id}.json").read_bytes())
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    class CompletingHost(FakeExecutorHost):
        def inspect(self, spec: ExecutorSpec, store: TaskControlStore) -> HostObservation:
            inspected.set()
            assert finished.wait(3)
            return HostObservation("exited", spec.generation, None, True)

    host = CompletingHost()
    lifecycle = RunLifecycle(
        states=states, control=control, host=host, task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: pytest.fail("must not select or create another Run"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation),
        execute=lambda _run_id: pytest.fail("must not replay business work"),
        prepare_executor_session=lambda: pytest.fail("must not create an environment carrier"),
    )
    executor = threading.Thread(target=finish_at_barrier)
    executor.start()
    try:
        result, resumed, receipt = lifecycle.submit(
            LifecycleRequest(task=task, kind="run", payload={"parent": 1}))
    finally:
        inspected.set()
        executor.join(timeout=3)
    assert not executor.is_alive()
    assert not errors
    abnormal_exit = final_status == "parent_delivery_pending"
    assert result["status"] == ("execution_failed" if abnormal_exit else final_status)
    assert resumed and receipt.attached
    if not abnormal_exit:
        assert receipt.status == "completed"
    assert receipt.action_id == action_id and receipt.executor_generation == 1
    if not abnormal_exit:
        assert (states.root / "runs" / f"{run_id}.json").read_bytes() == final_bytes[0]
    else:
        assert result["diagnostics"][0]["code"] == "session_interrupted"
        persisted = (states.root / "runs" / f"{run_id}.json").read_bytes()
        again, _, again_receipt = lifecycle.submit(
            LifecycleRequest(task=task, kind="run", payload={"parent": 1}))
        assert again["status"] == "execution_failed"
        assert again_receipt.executor_generation == 1
        assert (states.root / "runs" / f"{run_id}.json").read_bytes() == persisted
    assert host.start_count == 0
    record = control.load(task)
    assert record is not None and record["next_generation"] == 2
    expected_failure = "session_interrupted" if abnormal_exit else (
        None if close_action else "host proved the recorded process absent")
    assert record["executor"].get("failure") == expected_failure
