"""Stop's empty-execution result uses the original Task and real control storage."""
from __future__ import annotations

import fcntl
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

from agent_run.cli.main import _parser, _print_receipt
from agent_run.contracts.task import TaskKey
from agent_run.errors import KernelError
from agent_run.storage.sqlite import Transaction
from agent_run.storage.tasks import task_lock, task_path
from support.core_operations import task_run
from support.environment import durable_snapshot


@pytest.fixture
def idle(tmp_path: Path):
    task = TaskKey(tmp_path / "repository", "example/project", 1)
    # The public creation path owns this same lock before publishing the Run.
    with task_lock(task):
        kernel, _ = task_run(tmp_path, "idle")
    return kernel, task


def test_unstarted_unfinished_stop_is_read_only_and_keeps_the_original_lock(idle, tmp_path):
    kernel, task = idle
    before = kernel.source_status(), kernel.status("parent"), kernel.history("parent")
    original = durable_snapshot(tmp_path)
    lock = task_path(task).with_suffix(".lock")
    inode = lock.stat().st_dev, lock.stat().st_ino
    assert before[1]["phase"] != "complete"
    for action in ("first-stop", "second-stop"):
        assert kernel.stop_task(action) == "not_running"
        assert (kernel.source_status(), kernel.status("parent"), kernel.history("parent")) == before
        assert durable_snapshot(tmp_path) == original
        assert (lock.stat().st_dev, lock.stat().st_ino) == inode


def test_allocated_host_without_a_pid_still_receives_the_durable_stop_fence(idle, tmp_path):
    kernel, _ = idle
    request = kernel.admit_host("original", kind="run", command=("unlaunched-host",),
                               environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    before = kernel.host_usage("original")
    assert kernel.stop_task("stop-original") == "applied"
    assert kernel.source_status()["stopped"] is True
    assert kernel.host_request("original") == request and kernel.host_usage("original") == before
    original = durable_snapshot(tmp_path)
    assert kernel.stop_task("stop-original") == "applied"
    assert durable_snapshot(tmp_path) == original


def test_stop_keeps_same_action_identity_validation_and_targeted_cancellation(idle, tmp_path):
    kernel, _ = idle
    assert kernel.stop_task("targeted", subject="parent") == "applied"
    assert kernel.status("parent")["gates"] == ["operator_stopped"]
    original = durable_snapshot(tmp_path)
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.stop_task("targeted")
    assert durable_snapshot(tmp_path) == original
    assert kernel.stop_task("targeted", subject="parent") == "applied"
    assert durable_snapshot(tmp_path) == original
    # A historical applied Action is not a claim that an Executor still exists.
    assert kernel.stop_task("whole-run") == "not_running"
    assert durable_snapshot(tmp_path) == original


def test_abandon_remains_a_permanent_boundary_for_idle_stop(idle, tmp_path):
    kernel, _ = idle
    assert kernel.abandon_task("abandon", resource_inspections=()) == "pending"
    original = durable_snapshot(tmp_path)
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.stop_task("after-abandon")
    assert durable_snapshot(tmp_path) == original


def test_unreadable_task_binding_cannot_be_reported_as_idle(idle, tmp_path, monkeypatch):
    kernel, _ = idle
    original = durable_snapshot(tmp_path)
    rows = Transaction.rows

    def unavailable(transaction, sql, parameters=(), *, limit=128):
        if sql.startswith("SELECT * FROM tasks WHERE id="):
            raise OSError("Original Task binding cannot be read")
        return rows(transaction, sql, parameters, limit=limit)

    monkeypatch.setattr(Transaction, "rows", unavailable)
    with pytest.raises(KernelError, match="task_location_unavailable"):
        kernel.stop_task("unreadable-stop")
    assert durable_snapshot(tmp_path) == original


@pytest.mark.parametrize("start_first", [False, True])
def test_idle_stop_and_host_admission_use_the_original_task_lock(
    idle, tmp_path, monkeypatch, start_first,
):
    kernel, task = idle
    entered, finished = Event(), Event()
    results, failures = [], []
    original_flock = fcntl.flock

    def flock(descriptor, operation):
        if current_thread().name == "waiting-stop" and operation & fcntl.LOCK_EX:
            entered.set()
        return original_flock(descriptor, operation)

    def stop():
        try:
            results.append(kernel.stop_task("ordered-stop"))
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(fcntl, "flock", flock)
    if start_first:
        kernel.admit_host("first-start", kind="run", command=("unlaunched-host",),
                          environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    thread = Thread(target=stop, name="waiting-stop", daemon=True)
    try:
        with task_lock(task):
            original = durable_snapshot(tmp_path)
            thread.start()
            assert entered.wait(5)
            assert not finished.is_set()
            assert durable_snapshot(tmp_path) == original
        assert finished.wait(5)
        thread.join(5)
        assert not thread.is_alive() and not failures
        assert results == ["applied" if start_first else "not_running"]
        if not start_first:
            assert durable_snapshot(tmp_path) == original
            kernel.admit_host("later-start", kind="run", command=("unlaunched-host",),
                              environment_carrier=tmp_path / "carrier", preflight=lambda: None)
            assert kernel.source_status()["stopped"] is False
    finally:
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_no_execution_receipt_does_not_claim_an_action_or_business_completion(language, capsys):
    parsed = _parser(language).parse_args(["stop", "idle", "--repo", "example/project"])
    output = {"result": "no_op", "status": "not_running", "run_id": "idle", "action_id": None,
              "action": "stop", "host": "exited"}
    _print_receipt(parsed, output, language)
    shown = capsys.readouterr()
    assert shown.err == ""
    assert ("当前没有正在运行的 Agent；未创建 Action" if language == "zh" else
            "No Agent is running; no Action was created") in shown.out
    assert "agent-run runs --repo example/project" in shown.out
    assert not any(word in shown.out for word in ("已接受", "已完成", "Request accepted", "Business completed"))
    assert "The existing Host was confirmed exited" not in shown.out
