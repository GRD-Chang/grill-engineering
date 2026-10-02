from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_run.executor_host import ExecutorSpec, FakeExecutorHost
from agent_run.run_lifecycle import ActionReceipt, LifecycleRequest, RunLifecycle
from agent_run.task_control import ActionBusyError, ActionReconciliationError, TaskControlStore
from support.seeding import seed_idle_control
from support.lifecycle import _InMemoryRunState, _task


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
                    task=task, kind=command, payload={"parent": 156, "run_id": "run-1"},
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
        task=task, kind=kind, payload={"parent": 156, "run_id": "run-1"},
    ))
    assert state["status"] == "completed"
    assert receipt.status == "completed"
    assert control.load(task)["action"]["result_status"] == "completed"  # type: ignore[index]


@pytest.mark.parametrize("kind,change_at,stop_update", [
    ("approve", "selection", False),
    ("resume", "selection", False),
    ("approve", "execution", False),
    ("resume", "execution", False),
    ("approve", "selection", True),
])
def test_parent_update_releases_admission_and_preserves_executor(
    tmp_path: Path, kind: str, change_at: str, stop_update: bool,
) -> None:
    from agent_run.cli_presentation import public_action_receipt
    from agent_run.final_approval_operation import approval_update_pending

    class UpdateStopped(Exception):
        pass

    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "delivery_type": "parent_only",
        "status": "waiting_checks",
        "parent_job": {
            "phase": "waiting_checks",
            "approval_grant": {"granted_at": "approved"},
        },
    })
    control = TaskControlStore(tmp_path / "control")
    seed_idle_control(control, task, "run-1")
    host = FakeExecutorHost()
    binding: tuple[str, int] | None = None

    def record_update() -> None:
        states.value.update({
            "status": "parent_delivery_pending",
            "parent_job": {
                "phase": "accepted",
                "approval_update": {"granted_at": "approved"},
            },
        })

    def select(_action: Mapping[str, Any]) -> tuple[dict[str, object], bool]:
        nonlocal binding
        record = control.load(task)
        assert record is not None
        binding = (record["executor"]["action_id"], record["executor"]["generation"])
        if change_at == "selection":
            record_update()
        return dict(states.value), True

    def execute(run_id: str) -> Mapping[str, Any]:
        assert binding is not None
        record = control.load(task)
        assert record is not None
        if not approval_update_pending(states.value):
            assert record["action"]["status"] == "applying"
            record_update()
            return dict(states.value)
        assert record["action"]["status"] == "completed"
        assert record["action"]["result_status"] == "updating"
        assert record["action"]["failure"] is None
        assert (record["executor"]["action_id"], record["executor"]["generation"]) == binding
        control.assert_executor_current(
            task, action_id=binding[0], generation=binding[1], run_id=run_id,
        )
        if stop_update:
            # Exercise the same admission transaction used by public Stop.
            stop = control.claim_control_action(
                task, kind="stop", payload={"parent": 156, "run_id": run_id},
                run_id=run_id, state_dir=tmp_path / "states",
            )
            assert stop is not None and stop.target_executor is not None
            assert stop.target_executor["action_id"] == binding[0]
            with pytest.raises(ActionReconciliationError):
                control.assert_executor_current(
                    task, action_id=binding[0], generation=binding[1], run_id=run_id,
                )
            raise UpdateStopped
        states.value["status"] = "parent_approval_pending"
        states.value["parent_job"] = {"phase": "ready_for_approval"}
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=host, task=task,
        preflight=lambda: dict(states.value), select_run=select, initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ), execute=execute,
    )
    request = LifecycleRequest(task, kind, {"parent": 156, "run_id": "run-1"})
    if stop_update:
        with pytest.raises(UpdateStopped):
            lifecycle.submit(request)
    else:
        state, _, receipt = lifecycle.submit(request)
        assert state["status"] == "parent_approval_pending"
        assert receipt.status == "completed"
        assert receipt.result_status == "updating"
        assert public_action_receipt(
            receipt, repository="example/project", parent={"number": 156},
            next_action="approve",
        )["status"] == "updating"
    assert host.start_count == 1


@pytest.mark.parametrize("original_kind", ["approve", "resume"])
def test_resume_reconciles_saved_update_before_starting_a_successor(
    tmp_path: Path, original_kind: str,
) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt

    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value.update({
        "delivery_type": "parent_only", "status": "parent_delivery_pending",
        "parent_job": {"phase": "accepted", "approval_update": {"granted_at": "approved"}},
    })
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(
        task, kind=original_kind, payload={"parent": 156, "run_id": "run-1"},
    )
    assert claim.action_id is not None and claim.action is not None
    control.bind_run(task, claim.action_id, "run-1")
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1")
    control.mark_final_approval_operation(
        task, action_id=claim.action_id, generation=reservation.generation,
    )
    prepare_action_application_receipt(states.value, claim.action)
    control.record_application(
        task, action_id=claim.action_id, run_id="run-1",
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.mark_executor_absent(task, action_id=claim.action_id, generation=reservation.generation)

    def execute(_run_id: str) -> Mapping[str, Any]:
        old = control.snapshot(task, claim.action_id)
        assert old is not None
        assert old["action"]["status"] == "completed"
        assert old["action"]["result_status"] == "updating"
        assert old["action"]["failure"] is None
        assert "approval_grant" not in states.value["parent_job"]
        states.value["status"] = "parent_approval_pending"
        return dict(states.value)

    host = FakeExecutorHost()
    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control, host=host, task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True), initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ), execute=execute,
    )
    state, _, receipt = lifecycle.submit(LifecycleRequest(
        task, "resume", {"parent": 156, "run_id": "run-1"}, allow_terminal_successor=True,
    ))
    assert state["status"] == "parent_approval_pending"
    assert receipt.action_id != claim.action_id
    assert receipt.status == "completed"
    assert host.start_count == 1


@pytest.mark.parametrize("language,notice", [
    ("zh", "远端分支发生变化，正在更新与复验，完成后需要重新批准"),
    ("en", "Remote branches changed; updating and revalidating. A new approval is required afterward"),
])
def test_update_receipt_does_not_report_final_delivery_completed(
    capsys: pytest.CaptureFixture[str], language: str, notice: str,
) -> None:
    from agent_run.cli import _print_lifecycle_result
    from agent_run.messages import text

    state = {
        "run_id": "run-1", "repository": "example/project", "language": language,
        "parent": {"number": 156}, "delivery_type": "parent_only",
        "status": "parent_delivery_pending",
        "parent_job": {"phase": "accepted", "approval_update": {"granted_at": "approved"}},
    }
    receipt = ActionReceipt(
        action_id="approval", kind="approve", run_id="run-1", status="completed",
        attached=False, executor_status="running", executor_generation=1,
        handshake=True, payload_digest="payload", result_status="updating",
    )
    _print_lifecycle_result(state, {}, receipt)
    rendered = capsys.readouterr().out
    assert notice in rendered
    assert text("cli.delivery_completed_action", language=language) not in rendered


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
    claim = control.claim_action(
        task, kind="approve", payload={"parent": 156, "run_id": "run-1"}
    )
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
        task=task, kind="resume", payload={"parent": 156, "run_id": "run-1"},
        allow_terminal_successor=True,
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
        task=task, kind="resume", payload={"parent": 156, "run_id": "run-1"},
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
    claim = control.claim_action(
        task, kind="approve", payload={"parent": 156, "run_id": "run-1"}
    )
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
        lifecycle.submit(LifecycleRequest(task, "resume", {"parent": 156, "run_id": "run-1"}))
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
