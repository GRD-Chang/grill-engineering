"""Exact process ownership for parallel Workers in one Executor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.executor_host import (
    ExecutorStartUnknownError,
    FakeExecutorHost,
    _process_start_token,
    validate_control_target,
    validate_executor_exit,
)
from agent_run.task_control import (
    ActionReconciliationError,
    TaskControlStore,
    TaskKey,
)


def test_worker_bindings_scale_with_policy_and_finish_by_exact_identity(tmp_path: Path) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path / "repo", "owner/repo", 311)
    claim = control.claim_action(task, kind="run", payload={"parent": 311})
    assert claim.action_id is not None
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=None).generation

    def start(pid: int, token: str) -> dict[str, object]:
        return control.mark_worker_started(
            task, action_id=claim.action_id, generation=generation,
            pid=pid, process_start_token=token,
        )["executor"]

    first = start(1001, "a")
    assert first["worker"]["pid"] == 1001
    assert "workers" not in first
    second = start(1002, "b")
    assert "worker" not in second
    assert [worker["pid"] for worker in second["workers"]] == [1001, 1002]
    assert start(1002, "b") == second
    with pytest.raises(ActionReconciliationError, match="start token"):
        start(1002, "different")

    for offset in range(3, 34):
        start(1000 + offset, str(offset))
    assert len(control.load(task)["executor"]["workers"]) == 33
    with pytest.raises(ActionReconciliationError, match="completion binding"):
        control.mark_worker_finished(
            task, action_id=claim.action_id, generation=generation,
            pid=1002, process_start_token="wrong",
        )
    control.mark_worker_finished(
        task, action_id=claim.action_id, generation=generation,
        pid=1001, process_start_token="a",
    )
    remaining = control.load(task)["executor"]["workers"]
    assert len(remaining) == 32
    assert all(worker["pid"] != 1001 for worker in remaining)


def test_stop_terminates_every_live_worker_without_touching_unrelated_process() -> None:
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            start_new_session=True,
        )
        for _ in range(4)
    ]
    workers, unrelated = processes[:3], processes[3]
    try:
        target = {
            "pid": os.getpid(),
            "process_start_token": _process_start_token(os.getpid()),
            "workers": [
                {"pid": process.pid, "process_start_token": _process_start_token(process.pid)}
                for process in workers
            ],
        }
        validate_control_target(target)
        with pytest.raises(ExecutorStartUnknownError, match="仍在运行"):
            validate_executor_exit(target)
        FakeExecutorHost().terminate_control_target(target)
        assert all(process.wait(timeout=3) == -signal.SIGKILL for process in workers)
        assert unrelated.poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_unknown_worker_identity_blocks_signals_before_any_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import executor_host

    target = {
        "pid": 3003,
        "process_start_token": "executor",
        "workers": [
            {"pid": 3001, "process_start_token": "first"},
            {"pid": 3002, "process_start_token": "second"},
        ],
    }
    signaled: list[int] = []
    monkeypatch.setattr(
        executor_host, "_process_binding_status",
        lambda pid, _token: "unknown" if pid == 3002 else "matches",
    )
    monkeypatch.setattr(
        executor_host.os, "killpg", lambda pid, _signal: signaled.append(pid),
    )
    with pytest.raises(ExecutorStartUnknownError, match="ownership"):
        validate_control_target(target)
    with pytest.raises(ExecutorStartUnknownError, match="ownership"):
        FakeExecutorHost().terminate_control_target(target)
    assert signaled == []
