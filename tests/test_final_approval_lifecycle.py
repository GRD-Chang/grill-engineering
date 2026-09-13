from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_run.executor_host import ExecutorSpec, FakeExecutorHost
from agent_run.run_lifecycle import LifecycleRequest, RunLifecycle
from agent_run.task_control import ActionBusyError, TaskControlStore
from conftest import seed_idle_control
from test_run_lifecycle import _InMemoryRunState, _task


@pytest.mark.parametrize("kind", ["approve", "resume"])
def test_final_approval_holds_admission_until_delivery_finishes(
    tmp_path: Path, kind: str,
) -> None:
    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "status": "waiting_checks",
        "run_publication": {"approval_grant": {"granted_at": "approved"}},
    })
    control = TaskControlStore(tmp_path / "control")
    seed_idle_control(control, task, "run-1")

    def execute(_run_id: str) -> Mapping[str, Any]:
        for command in ("stop", "abandon"):
            with pytest.raises(ActionBusyError, match="最终批准"):
                lifecycle.submit_control(LifecycleRequest(
                    task=task, kind=command, payload={"parent": 156},
                ))
        states.value["status"] = "completed"
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=FakeExecutorHost(), task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ),
        execute=execute,
    )
    state, _, receipt = lifecycle.submit(LifecycleRequest(
        task=task, kind=kind, payload={"parent": 156},
    ))
    assert state["status"] == "completed"
    assert receipt.status == "completed"
    assert control.load(task)["action"]["result_status"] == "completed"  # type: ignore[index]


def test_resume_closes_an_exited_unfinished_approval_as_failed_before_continuing(
    tmp_path: Path,
) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt

    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "status": "waiting_external",
        "run_publication": {"approval_grant": {"granted_at": "approved"}},
    })
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="approve", payload={"parent": 156})
    assert claim.action_id is not None and claim.action is not None
    control.bind_run(task, claim.action_id, "run-1")
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1")
    prepare_action_application_receipt(states.value, claim.action)
    control.record_application(task, action_id=claim.action_id, run_id="run-1",
                               payload_digest=str(claim.action["payload_digest"]))
    control.mark_executor_absent(task, action_id=claim.action_id, generation=reservation.generation)

    def execute(_run_id: str) -> Mapping[str, Any]:
        original = control.snapshot(task, claim.action_id)
        assert original is not None
        assert original["action"]["status"] == "failed"
        states.value["status"] = "completed"
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=FakeExecutorHost(), task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ), execute=execute,
    )
    state, _, receipt = lifecycle.submit(LifecycleRequest(
        task=task, kind="resume", payload={"parent": 156}, allow_terminal_successor=True,
    ))
    assert state["status"] == "completed"
    assert receipt.kind == "resume"
    assert receipt.status == "completed"


def test_resume_stops_when_refresh_invalidates_the_original_approval(tmp_path: Path) -> None:
    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "status": "supervision_timeout",
        "run_publication": {"approval_grant": {"granted_at": "approved"}},
    })
    control = TaskControlStore(tmp_path / "control")
    seed_idle_control(control, task, "run-1")

    def select(_action: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        states.value.update({"status": "run_acceptance_pending", "run_publication": None})
        return dict(states.value), True

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=FakeExecutorHost(), task=task,
        preflight=lambda: dict(states.value), select_run=select, initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ),
        execute=lambda _run_id: pytest.fail("old approval must not authorize new development"),
    )
    final, _, receipt = lifecycle.submit(LifecycleRequest(
        task=task, kind="resume", payload={"parent": 156},
    ))
    assert final["status"] == "run_acceptance_pending"
    assert receipt.status == "failed"


def test_failed_approval_cannot_resume_while_its_executor_is_still_alive(tmp_path: Path) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt

    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "status": "execution_failed",
        "run_publication": {"approval_grant": {"granted_at": "approved"}},
    })
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="approve", payload={"parent": 156})
    assert claim.action_id is not None and claim.action is not None
    control.bind_run(task, claim.action_id, "run-1")
    prepare_action_application_receipt(states.value, claim.action)
    host = FakeExecutorHost()
    host.ensure(ExecutorSpec(task, claim.action_id, "run-1", 1), control)
    control.fail_action(task, action_id=claim.action_id, failure="response lost")

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=host, task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail("must prove the original Executor exited"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ),
        execute=lambda _run_id: pytest.fail("must not start a second Executor"),
    )
    before = control.path_for(task).read_bytes()
    with pytest.raises(ActionBusyError):
        lifecycle.submit(LifecycleRequest(task, "resume", {"parent": 156}))
    assert control.path_for(task).read_bytes() == before
    assert host.start_count == 1


@pytest.mark.parametrize("wrong_field", ["run_id", "payload_digest", "executor_generation"])
def test_unbound_approval_reconciliation_rejects_a_foreign_receipt(
    tmp_path: Path, wrong_field: str,
) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt
    from agent_run.task_control import ActionReconciliationError

    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="approve", payload={"run_id": "run-1"})
    assert claim.action_id is not None and claim.action is not None
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
    state: dict[str, Any] = {"run_id": "run-1"}
    prepare_action_application_receipt(state, claim.action)
    control.mark_executor_absent(task, action_id=claim.action_id, generation=reservation.generation)
    control.fail_action(task, action_id=claim.action_id, failure="process exited")
    receipt = state["action_application_receipt"]
    receipt[wrong_field] = 2 if wrong_field == "executor_generation" else "foreign"
    before = control.path_for(task).read_bytes()
    with pytest.raises(ActionReconciliationError):
        control.complete_action_from_application_receipt(
            task, action_id=claim.action_id, generation=reservation.generation,
            application_receipt=receipt, result_status="completed",
        )
    assert control.path_for(task).read_bytes() == before
