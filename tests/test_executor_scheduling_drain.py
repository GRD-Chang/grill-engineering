"""Dispatch failure must close Resume before waiting for existing workers."""
from pathlib import Path
from threading import Event, Thread
from typing import Any
from types import SimpleNamespace

from agent_run.controller import Controller
from agent_run.executor import DeliveryExecutor
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.notifications import Notifications
from agent_run.notification_events import events

import pytest

from agent_run.delivery import TicketDeliveryEngine
from agent_run.executor_host import ExecutorSpec, FakeExecutorHost
from agent_run.run_lifecycle import LifecycleRequest, RunLifecycle
from agent_run.task_control import ActionBusyError, TaskControlBusyError, TaskControlStore, TaskKey
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("source", ["lane", "supervision", "global_state"])
def test_failed_scheduler_rejects_resume_before_siblings_finish(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = state["run_id"]
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(states.root)
    entered, release, draining, reported = Event(), Event(), Event(), Event()
    failure = TaskControlBusyError("control lock deadline expired")
    errors: list[BaseException] = []
    original_close = control.close_ticket_resume_admission
    observed: list[dict[str, Any]] = []
    recorded: list[str] = []
    controller = Controller(FixtureGitHubReader(fixture), git, states)

    def record(_states: Any, _run_id: str, message: str, _binding: Any) -> bool:
        recorded.append(message)
        return controller.record_execution_failure(_run_id, message)

    def observe(_self: Any, value: dict[str, Any], **_kwargs: Any) -> None:
        observed.extend(events(value))
        if value["status"] == "execution_failed":
            reported.set()

    monkeypatch.setattr(Notifications, "observe", observe)

    def close_admission(*args: Any, **kwargs: Any) -> bool:
        closed = original_close(*args, **kwargs)
        if closed:
            draining.set()
        return closed

    def advance(
        _engine: TicketDeliveryEngine, _run_id: str, number: int, *, complete: bool = True,
    ) -> dict[str, Any]:
        if number == 2:
            entered.set()
            assert release.wait(5), "sibling was not released"
        elif source == "lane":
            assert entered.wait(5)
            raise failure
        else:
            assert release.wait(5), "sibling was not released"
        return state

    def supervise(_lanes: frozenset[int]) -> None:
        assert entered.wait(5)
        if source == "global_state":
            record(states, run_id, str(failure), None)
        else:
            raise failure

    monkeypatch.setattr(control, "close_ticket_resume_admission", close_admission)
    monkeypatch.setattr(TicketDeliveryEngine, "deliver_ticket", advance)
    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), lambda _: None))

    def execute(_run_id: str) -> dict[str, Any]:
        engine.develop_frontier(
            run_id, state, supervise=supervise if source in {"supervision", "global_state"} else None,
        )
        return state

    lifecycle = RunLifecycle(
        states=states, control=control, host=FakeExecutorHost(), task=task,
        preflight=lambda: None, select_run=lambda _action: (state, False),
        initialize_profile=None,
        executor_spec=lambda run, action, generation: ExecutorSpec(
            task=task, run_id=run, action_id=action, generation=generation,
        ), execute=lambda target: DeliveryExecutor(
            states=states, driver_factory=lambda *_: SimpleNamespace(advance=lambda _: execute(target)),
            record_execution_failure=record,
        ).execute(target),
    )

    def run() -> None:
        try:
            lifecycle.submit(LifecycleRequest(task=task, kind="run", payload={"parent": 1}))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5), errors
        assert draining.wait(5), errors
        assert thread.is_alive() and not release.is_set()
        current = control.load(task)
        assert current is not None
        executor = current["executor"]
        assert executor["status"] == "running"
        with pytest.raises(ActionBusyError, match="收口"):
            with control.ticket_resume_transaction(
                task, action_id=executor["action_id"], generation=executor["generation"], run_id=run_id,
            ):
                pytest.fail("a stopped scheduler admitted new work")
        assert executor.get("failure") == (None if source == "global_state" else str(failure))
        assert reported.wait(5), errors
        assert states.load_current_run(run_id)["status"] == "execution_failed"
        assert recorded == [str(failure)]
        assert any(item.get("current") and item.get("action_kind") == "execution_failure"
                   and item.get("status_code") == "execution_failed" for item in observed), observed
        receipt = lifecycle.receipt_from_record(current, action_id=executor["action_id"], attached=True)
        assert receipt.failure == (None if source == "global_state" else str(failure))
        assert receipt.status == "completed" and receipt.executor_status == "running"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == ([] if source == "global_state" else [failure])
    assert recorded == [str(failure)]


@pytest.mark.parametrize("recording_fails", [False, True])
def test_executor_records_exhausted_control_contention(
    tmp_path: Path, recording_fails: bool,
) -> None:
    from types import SimpleNamespace
    from agent_run.executor import DeliveryExecutor

    failure = TaskControlBusyError("control lock deadline expired")
    state = {"run_id": "run-1", "status": "active"}
    states = SimpleNamespace(root=tmp_path, load_current_run=lambda _: state)
    recorded: list[tuple[str, str, Any]] = []

    def advance(_state: Any) -> None:
        raise failure

    def record(_states: Any, run_id: str, message: str, binding: Any) -> bool:
        recorded.append((run_id, message, binding))
        if recording_fails:
            raise OSError("state storage unavailable")
        return True

    executor = DeliveryExecutor(
        states=states,  # type: ignore[arg-type]
        driver_factory=lambda *_: SimpleNamespace(advance=advance),
        record_execution_failure=record,
    )
    with pytest.raises(TaskControlBusyError) as caught:
        executor.execute("run-1", action_id="action-1", generation=2)
    assert caught.value is failure
    assert recorded == [("run-1", str(failure), ("action-1", 2, "run-1"))]
    if recording_fails:
        assert "state storage unavailable" in " ".join(failure.__notes__)


def test_admission_close_failure_retains_original_error() -> None:
    from agent_run.executor_admission import close_admission_on_failure, executor_admission_scope

    failure = RuntimeError("dispatch stopped")

    def close(_error: BaseException) -> None:
        raise OSError("control storage unavailable")

    with pytest.raises(RuntimeError) as caught:
        with executor_admission_scope(close), close_admission_on_failure():
            raise failure
    assert caught.value is failure
    assert "control storage unavailable" in " ".join(failure.__notes__)


@pytest.mark.parametrize("different", ["action_id", "generation"])
def test_receipt_does_not_attribute_another_executor_failure(different: str) -> None:
    executor = {
        "action_id": "a", "generation": 2, "status": "running",
        "ticket_resume_closed": True, "failure": "old failure",
    }
    executor[different] = "other" if different == "action_id" else 3
    record = {
        "action": {"action_id": "a", "executor_generation": 2, "status": "completed"},
        "executor": executor,
    }
    assert RunLifecycle.receipt_from_record(record, action_id="a", attached=True).failure is None


@pytest.mark.parametrize("kind", ["revoked", "concurrent", "graph"])
def test_reconciliation_does_not_manufacture_execution_failure(tmp_path: Path, kind: str) -> None:
    from agent_run.state import ConcurrentRunStateError
    from agent_run.revisions import TicketGraphDriftError
    from agent_run.task_control import ActionReconciliationError
    from agent_run.executor_admission import close_admission_on_failure

    failure = {"revoked": ActionReconciliationError, "concurrent": ConcurrentRunStateError,
               "graph": TicketGraphDriftError}[kind]("ownership or facts changed")
    states = SimpleNamespace(root=tmp_path, load_current_run=lambda _: {"run_id": "run-1", "status": "active"})
    recorded: list[str] = []

    def advance(_state: Any) -> None:
        with close_admission_on_failure():
            raise failure

    executor = DeliveryExecutor(
        states=states,
        driver_factory=lambda *_: SimpleNamespace(advance=advance),
        record_execution_failure=lambda *_: recorded.append("failure") or True,
    )
    with pytest.raises(type(failure)):
        executor.execute("run-1")
    assert recorded == []


@pytest.mark.parametrize("kind", ["concurrent"])
def test_reconcilable_result_keeps_live_executor_resume_admission(tmp_path: Path, kind: str) -> None:
    from agent_run.state import ConcurrentRunStateError
    from agent_run.revisions import TicketGraphDriftError
    from agent_run.executor_admission import close_admission_on_failure, executor_admission_scope

    control = TaskControlStore(tmp_path / "control")
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1").generation
    identity = dict(action_id=claim.action_id, generation=generation, run_id="run-1")
    control.bind_run(task, claim.action_id, "run-1", generation=generation)
    control.mark_handshake(task, action_id=claim.action_id, generation=generation, pid=900, process_start_token="executor")
    control.record_application(task, **identity, payload_digest=claim.action["payload_digest"])
    control.complete_action(task, action_id=claim.action_id, generation=generation, result_status="active")
    failure = (ConcurrentRunStateError if kind == "concurrent" else TicketGraphDriftError)("refresh needed")

    def close(error: BaseException | None) -> None:
        control.close_ticket_resume_admission(task, **identity, can_exit=lambda: True, failure=str(error))

    with executor_admission_scope(close):
        try:
            with close_admission_on_failure():
                raise failure
        except type(failure):
            # Driver / orchestration owns the currentness refresh and continues.
            pass
        with control.ticket_resume_transaction(task, **identity):
            assert control.load(task)["executor"].get("ticket_resume_closed") is not True


def test_supervision_snapshot_race_keeps_dispatch_alive(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_run.state import ConcurrentRunStateError
    import agent_run.delivery as delivery

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.01)
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    started, release, refreshed = Event(), Event(), Event()
    errors: list[BaseException] = []
    observations = 0

    def develop(_number: int) -> None:
        started.set()
        assert release.wait(5)

    def supervise(_lanes: frozenset[int]) -> None:
        nonlocal observations
        assert started.wait(5)
        observations += 1
        if observations == 1:
            raise ConcurrentRunStateError("live Resume replaced snapshot")
        refreshed.set()

    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), develop))

    def run() -> None:
        try:
            engine.develop_frontier(state["run_id"], state, supervise=supervise)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert refreshed.wait(5), errors
        assert thread.is_alive() and not release.is_set()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors, errors
