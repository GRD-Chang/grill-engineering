"""Real control-lock contention must not abort an already authorized Worker."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_run import task_control as control_module
from agent_run.task_control import ActionReconciliationError, TaskControlBusyError, TaskControlStore, TaskKey
from agent_run.object_state import save_owned


@pytest.mark.parametrize("operation", ["start", "finish"])
@pytest.mark.parametrize("revocation", ["none", "stop", "generation"])
def test_worker_control_write_waits_and_revalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, revocation: str,
) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    identity = dict(action_id=claim.action_id, generation=generation)
    control.bind_run(task, claim.action_id, "run-1", generation=generation)
    control.mark_handshake(task, **identity, pid=900, process_start_token="executor")
    control.record_application(task, **identity, run_id="run-1", payload_digest=claim.action["payload_digest"])
    control.complete_action(task, **identity, result_status="active")
    worker = dict(pid=1001, process_start_token="worker-1")
    if operation == "finish":
        control.mark_worker_started(task, **identity, **worker)
    contended, release_retry = threading.Event(), threading.Event()
    real_flock = control_module.fcntl.flock

    def observed_flock(fd, flags):
        try:
            return real_flock(fd, flags)
        except BlockingIOError:
            contended.set()
            raise

    def retry_sleep(_delay):
        assert release_retry.wait(3)

    monkeypatch.setattr(control_module.fcntl, "flock", observed_flock)
    monkeypatch.setattr(control_module, "sleep", retry_sleep)
    method = control.mark_worker_started if operation == "start" else control.mark_worker_finished
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with control._locked(task):
                pending = pool.submit(method, task, **identity, **worker)
                assert contended.wait(3)
                with pytest.raises(TaskControlBusyError):
                    control.claim_control_action(task, kind="stop", payload={}, run_id="run-1", state_dir=control.root)
            if revocation == "stop":
                control.claim_control_action(task, kind="stop", payload={}, run_id="run-1", state_dir=control.root)
            elif revocation == "generation":
                control.finish_executor(task, **identity)
                successor = control.claim_action(task, kind="resume", payload={})
                control.begin_executor(task, action_id=successor.action_id, run_id="run-1")
            before = control.path_for(task).read_bytes()
        finally:
            release_retry.set()
        if revocation == "none":
            pending.result(timeout=3)
            executor = control.load(task)["executor"]
            assert (executor.get("worker", {}).get("pid") == 1001) == (operation == "start")
        else:
            with pytest.raises(ActionReconciliationError):
                pending.result(timeout=3)
            assert control.path_for(task).read_bytes() == before


@pytest.mark.parametrize("operation", ["start", "finish"])
def test_worker_control_wait_has_one_bounded_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    before = control.path_for(task).read_bytes()
    clock = iter([0.0, 0.4, 1.0])
    intervals: list[float] = []
    monkeypatch.setattr(control_module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(control_module, "sleep", intervals.append)
    method = control.mark_worker_started if operation == "start" else control.mark_worker_finished
    with control._locked(task):
        with pytest.raises(TaskControlBusyError):
            method(task, action_id=claim.action_id, generation=generation, pid=1001, process_start_token="worker-1")
    assert intervals == [0.01]
    assert control.path_for(task).read_bytes() == before


def test_state_commit_does_not_replay_observer_after_persisting(tmp_path: Path) -> None:
    from agent_run.state import StateStore

    states = StateStore(tmp_path / "state")
    observations: list[str] = []

    def observer(state):
        observations.append(state["status"])
        raise TaskControlBusyError("observer failed after commit")

    states.run_saved_observer = observer
    states.save_run("run-1", {"run_id": "run-1", "status": "active"})
    assert observations == ["active"]
    assert states.load_run("run-1")["status"] == "active"


@pytest.mark.parametrize("transaction_name", ["_executor_current_transaction", "ticket_resume_transaction"])
def test_state_commit_uses_one_control_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transaction_name: str,
) -> None:
    from functools import partial
    from agent_run.state import StateStore

    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    states = StateStore(control.root)
    states.save_run("run-1", {"run_id": "run-1", "status": "active"})
    states._set_write_guard(None, transaction=partial(
        getattr(control, transaction_name), task, action_id=claim.action_id,
        generation=generation, run_id="run-1",
    ))
    before_control = control.path_for(task).read_bytes()
    before_run = states.load_run("run-1")
    clock = iter([0.0, 0.4, 1.0])
    intervals: list[float] = []
    monkeypatch.setattr(control_module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(control_module, "sleep", intervals.append)
    with control._locked(task):
        with pytest.raises(TaskControlBusyError):
            save_owned(states, "run-1", {"run_id": "run-1", "status": "continued"}, fields={'status'})
    assert intervals == [0.01]
    assert control.path_for(task).read_bytes() == before_control
    assert states.load_run("run-1") == before_run
