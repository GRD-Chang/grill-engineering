"""Host exit facts share notification identity, validity and finite delivery budgets."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_run.executor_exit import exit_event, exit_properties, observe_exit
from agent_run.executor_host import ExecutorSpec, _process_start_token
from agent_run.notifications import read_notifications
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlError, TaskControlStore
from support.executor_exit import create_exit_run


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 0)


def _observe(spec: ExecutorSpec) -> None:
    observe_exit(spec, service_result="signal", exit_code="killed", exit_status="KILL")


def _business_files(states: StateStore) -> dict[Path, bytes]:
    return {path.relative_to(states.root): path.read_bytes()
            for directory in ("runs", "task-control")
            for path in (states.root / directory).rglob("*") if path.is_file()}


def _resume(states: StateStore, control: TaskControlStore, spec: ExecutorSpec) -> ExecutorSpec:
    """A real successor Action fences the observed generation before delivery."""
    assert spec.run_id is not None
    control.finish_executor(spec.task, action_id=spec.action_id, generation=spec.generation,
                            failure="session interrupted")
    claim = control.claim_action(spec.task, kind="resume", payload={"run_id": spec.run_id})
    assert claim.action is not None and claim.action_id is not None
    reservation = control.begin_executor(spec.task, action_id=claim.action_id,
                                         run_id=spec.run_id, runner_binding=spec.runner_binding)
    successor = replace(spec, action_id=claim.action_id, generation=reservation.generation)
    control.bind_run(spec.task, successor.action_id, spec.run_id,
                     generation=successor.generation, state_dir=states.root)
    control.mark_handshake(spec.task, action_id=successor.action_id,
                           generation=successor.generation, pid=os.getpid(),
                           process_start_token=_process_start_token(os.getpid()))
    state = states.load_current_run(spec.run_id)
    assert state is not None
    prepare_action_application_receipt(state, claim.action)
    states.save_run(spec.run_id, state)
    control.record_application(spec.task, action_id=successor.action_id, run_id=spec.run_id,
                               generation=successor.generation,
                               payload_digest=claim.action["payload_digest"])
    control.complete_action(spec.task, action_id=successor.action_id,
                            generation=successor.generation, result_status="active")
    return successor


@pytest.mark.parametrize("result,code,status", [
    ("signal", "killed", "KILL"), ("oom-kill", "killed", "KILL"),
    ("core-dump", "dumped", "SEGV"), ("exit-code", "exited", "1"),
])
def test_abnormal_exit_records_exact_identity_without_business_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str, code: str, status: str,
) -> None:
    states, control, spec = create_exit_run(tmp_path)
    calls: list[str] = []

    def sent(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", sent)
    before = _business_files(states)
    observe_exit(spec, service_result=result, exit_code=code, exit_status=status)
    document = read_notifications(states.root, str(spec.run_id))
    evidence = document["executor_exit"]
    current = control.load(spec.task)
    assert current is not None
    assert evidence["pid"] == current["executor"]["pid"]
    assert evidence["process_start_token"] == current["executor"]["process_start_token"]
    assert {key: evidence[key] for key in ("run_id", "action_id", "generation", "runner_binding")} == {
        "run_id": spec.run_id, "action_id": spec.action_id,
        "generation": spec.generation, "runner_binding": spec.runner_binding,
    }
    assert (evidence["service_result"], evidence["exit_code"], evidence["exit_status"]) == (result, code, status)
    record, = [item for item in document["records"] if item["kind"] == "executor_exit"]
    assert record["exit_evidence"] == evidence
    assert record["outcome"] == "success" and record["attempts"] == 1
    assert hashlib.sha256(record["id"].encode()).hexdigest()[:32] in calls
    assert document["pending"] == []
    assert _business_files(states) == before  # Includes policy, attempts and recovery receipts.


@pytest.mark.parametrize("case", [
    "normal", "completed", "abandoned", "operator_stopped", "stop", "waiting_approval",
    "waiting_human", "handled_failure", "handled_exception", "wrong_action", "wrong_generation", "wrong_runner",
    "wrong_run", "wrong_action_generation", "unbound_action", "recovered", "disabled",
])
def test_exit_observer_suppresses_non_crashes_and_obsolete_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    states, control, spec = create_exit_run(tmp_path, enabled=case != "disabled")
    state = states.load_current_run(str(spec.run_id))
    assert state is not None
    if case in {"completed", "abandoned", "operator_stopped"}:
        state["status"] = case
        states.save_run(str(spec.run_id), state)
    elif case in {"waiting_approval", "waiting_human", "handled_failure", "handled_exception"}:
        state["status"] = {"waiting_approval": "ready_for_approval", "waiting_human": "ready_for_human",
                           "handled_failure": "execution_failed", "handled_exception": "execution_failed"}[case]
        states.save_run(str(spec.run_id), state)
        control.finish_executor(spec.task, action_id=spec.action_id, generation=spec.generation,
                                result_status=state["status"],
                                failure="handled invocation failure" if case == "handled_exception" else None)
    elif case == "stop":
        control.claim_control_action(spec.task, kind="stop", payload={"run_id": spec.run_id},
                                     run_id=str(spec.run_id), state_dir=states.root)
    elif case == "wrong_action":
        spec = replace(spec, action_id="previous-action")
    elif case == "wrong_generation":
        spec = replace(spec, generation=spec.generation + 1)
    elif case == "wrong_runner":
        spec = replace(spec, runner_binding="fedcba9876543210")
    elif case == "wrong_run":
        state["run_id"] = "another-run"
        state.pop("action_application_receipt")
        states.save_run("another-run", state)
        spec = replace(spec, run_id="another-run")
    elif case in {"wrong_action_generation", "unbound_action"}:
        # These negative records retain valid field types, but no longer prove
        # the current Action belongs to the observed Executor and Run.
        record = control.load(spec.task)
        assert record is not None
        if case == "wrong_action_generation":
            record["action"]["executor_generation"] += 1
        else:
            record["action"]["run_id"] = None
        control.path_for(spec.task).write_text(json.dumps(record), encoding="utf-8")
    elif case == "recovered":
        successor = _resume(states, control, spec)
        assert successor.generation > spec.generation

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("non-crash or obsolete exit invoked the notification transport")

    monkeypatch.setattr("agent_run.notifications.send", unexpected)
    before = _business_files(states)
    if case == "normal":
        observe_exit(spec, service_result="success", exit_code="exited", exit_status="0")
    elif case == "wrong_action_generation":
        with pytest.raises(TaskControlError, match="Action/Executor generation"):
            _observe(spec)
    else:
        _observe(spec)
    assert not (states.root / "notifications").exists()
    assert _business_files(states) == before


def test_unknown_exit_delivery_keeps_identity_and_exhausted_budget_on_duplicate_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    states, _, spec = create_exit_run(tmp_path)
    calls: list[str] = []

    def unknown(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "unknown", "reason": "response lost"}

    monkeypatch.setattr("agent_run.notifications.send", unknown)
    before = _business_files(states)
    _observe(spec)
    first = read_notifications(states.root, str(spec.run_id))
    record, = [item for item in first["records"] if item["kind"] == "executor_exit"]
    key = hashlib.sha256(record["id"].encode()).hexdigest()[:32]
    assert calls.count(key) == 3
    assert record["attempts"] == 3 and record["outcome"] == "unknown" and record["exhausted"]
    assert record["exhaustion_reason"] == "max_attempts"
    assert [attempt["outcome"] for attempt in record["attempt_history"]] == ["unknown"] * 3
    initial_calls = calls.copy()
    for _ in range(3):
        _observe(spec)
    assert calls == initial_calls
    assert read_notifications(states.root, str(spec.run_id)) == first
    assert _business_files(states) == before


@pytest.mark.parametrize("resume_before_exit_send", [True, False])
def test_new_generation_cancels_old_exit_before_submission_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume_before_exit_send: bool,
) -> None:
    states, control, spec = create_exit_run(tmp_path)
    exit_calls: list[str] = []
    resumed = False

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal resumed
        is_exit = rendered["header"]["title"]["content"] == "执行会话意外退出"
        if is_exit:
            exit_calls.append(identity)
        if not resumed and (resume_before_exit_send or is_exit):
            successor = _resume(states, control, spec)
            assert successor.generation > spec.generation
            resumed = True
        return {"outcome": "unknown" if is_exit else "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    _observe(spec)
    document = read_notifications(states.root, str(spec.run_id))
    record, = [item for item in document["records"] if item["kind"] == "executor_exit"]
    assert resumed and len(exit_calls) == (0 if resume_before_exit_send else 1)
    assert record["attempts"] == len(exit_calls)
    assert record["cancelled"] and record["cancel_reason"] == "no_longer_current"
    assert record["outcome"] == ("cancelled" if resume_before_exit_send else "unknown")
    assert document["pending"] == []
    current = states.load_current_run(str(spec.run_id))
    assert current is not None
    before = deepcopy(document)
    assert exit_event(states.root, current, document) == []
    assert document == before


def test_exit_hook_properties_are_absent_when_notifications_are_disabled(tmp_path: Path) -> None:
    states, control, spec = create_exit_run(tmp_path, enabled=False)
    before = _business_files(states)
    assert exit_properties(spec, control, Path(sys.executable), tmp_path / "environment") == ()
    assert _business_files(states) == before


def test_creation_action_arms_before_run_binding_then_observes_the_bound_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    states, control, spec = create_exit_run(tmp_path, reserved=False)
    # A different Task models the admitted creation request before a Run exists.
    task = replace(spec.task, parent_number=2)
    claim = control.claim_action(task, kind="run", payload={"notifications": {"enabled": True}})
    assert claim.action_id is not None
    creation = replace(spec, task=task, run_id=None, action_id=claim.action_id)
    properties = exit_properties(creation, control, Path(sys.executable), tmp_path / "private-environment")
    assert any(item.startswith("ExecStopPost=-:") and "agent_run.executor_exit" in item for item in properties)
    assert any(item.startswith("LoadCredential=") for item in properties)
    assert {"TimeoutStopSec=70s", "TimeoutStopFailureMode=kill", "FileDescriptorStoreMax=1",
            "NotifyAccess=main", "OOMPolicy=continue"}.issubset(properties)
    assert not any("--run-id" in item for item in properties)
    control.begin_executor(task, action_id=creation.action_id, run_id=None,
                           runner_binding=creation.runner_binding)
    control.mark_handshake(task, action_id=creation.action_id, generation=creation.generation,
                           pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    state = states.load_current_run(str(spec.run_id))
    assert state is not None
    state["run_id"] = "run-created"
    state["parent"]["number"] = task.parent_number
    states.save_run("run-created", state)
    control.bind_run(task, creation.action_id, "run-created", generation=creation.generation,
                     state_dir=states.root)
    monkeypatch.setattr("agent_run.notifications.send", lambda *args, **kwargs: {"outcome": "success", "reason": None})
    before = _business_files(states)
    _observe(creation)
    document = read_notifications(states.root, "run-created")
    assert document["executor_exit"]["run_id"] == "run-created"
    assert document["executor_exit"]["parent"] == task.parent_number
    assert any(item["kind"] == "executor_exit" and item["outcome"] == "success"
               for item in document["records"])
    assert _business_files(states) == before


def test_a_recovered_generations_later_crash_has_its_own_event_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    states, control, spec = create_exit_run(tmp_path)
    monkeypatch.setattr("agent_run.notifications.send", lambda *args, **kwargs: {"outcome": "success", "reason": None})
    _observe(spec)
    successor = _resume(states, control, spec)
    _observe(successor)
    document = read_notifications(states.root, str(spec.run_id))
    records = [item for item in document["records"] if item["kind"] == "executor_exit"]
    assert [item["generation"] for item in records] == [spec.generation, successor.generation]
    assert len({item["id"] for item in records}) == 2
    assert all(item["attempts"] == 1 and item["outcome"] == "success" for item in records)
    _observe(spec)
    assert read_notifications(states.root, str(spec.run_id)) == document


def test_resume_during_transport_identity_check_prevents_old_exit_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import notification_transport as transport

    states, control, spec = create_exit_run(tmp_path)
    identity_checks = 0
    submissions: list[str] = []

    def run(argv: list[str], deadline: float, cancel: Any) -> transport._CommandResult:
        nonlocal identity_checks
        if "whoami" in argv:
            identity_checks += 1
            if identity_checks == 2:  # The exit card, after ordinary run_start.
                assert _resume(states, control, spec).generation > spec.generation
            return transport._CommandResult(0, json.dumps({"profile": "work", "appId": "cli_test",
                "identity": "bot", "available": True}).encode())
        submissions.append(argv[argv.index("--content") + 1])
        return transport._CommandResult(0, b'{"data":{"message_id":"fixture"}}')

    monkeypatch.setattr(transport, "_run", run)
    _observe(spec)  # Use the production send(), including whoami and callback.
    assert identity_checks == 2 and len(submissions) == 1
    assert "执行会话意外退出" not in submissions[0]
    record, = [item for item in read_notifications(states.root, str(spec.run_id))["records"]
               if item["kind"] == "executor_exit"]
    assert record["attempts"] == 1 and record["outcome"] == "failed"
    assert record["cancelled"] and record["cancel_reason"] == "no_longer_current"
