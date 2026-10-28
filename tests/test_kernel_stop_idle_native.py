"""Stop's native producer boundaries, requiring the unchanged SaveClock guard.

Host observations and role returns are explicit synthetic external inputs; no
Host process, credential provider or network transport is started. This file
must remain unrun where native save-clock evidence is unavailable.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Execution, HostLaunch, HostObservation, wire_json
from agent_run.storage.tasks import task_lock
from support.core_operations import CoreOperations, ReadOnlyPorts, task_run
from support.environment import durable_snapshot
from support.offline import Scenario


def _host(root: Path) -> tuple[Kernel, ReadOnlyPorts, HostLaunch]:
    task = TaskKey(root / "repository", "example/project", 1)
    # Formal creation already holds this same mutex; retain that real setup.
    with task_lock(task):
        kernel, ports = task_run(root, "original-run")
    request = kernel.admit_host("original-host", kind="run", command=("controlled-host",),
        environment_carrier=root / "carrier", preflight=lambda: None)
    return kernel, ports, request


def _running(kernel: Kernel, request: HostLaunch) -> HostObservation:
    assert kernel.dispatch_host(request)
    observation = HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "original-start", usage_ready=True)
    assert kernel.observe_host(request, observation) == "running"
    return observation


def _exit(observation: HostObservation, *, released: bool = True) -> HostObservation:
    return replace(observation, disposition="exited", exit_status=0, ended_at=111.0,
                   usage_ready=False, usage_released=released)


@pytest.mark.parametrize("consume", [False, True], ids=["retained-return", "human-blocker"])
def test_stop_after_confirmed_host_exit_preserves_original_unfinished_work(
    tmp_path: Path, consume: bool,
) -> None:
    kernel, ports, request = _host(tmp_path)
    running = _running(kernel, request)
    execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, execution, lambda _: ports)
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None and call.subject == "parent"
    # The existing synchronous Core fixture supplies a return without claiming
    # a physical Worker launch. The normal receive producer settles its lease.
    kernel.receive(call, wire_json({"result_kind": "human_blocker", "summary": None,
                                   "human_blockers": ["Keep the original explicit decision"]}))
    if consume:
        assert operations.once().reason == "human_blocker"
    assert kernel.observe_host(request, _exit(running)) == "exited"
    assert kernel.active_host_action() is None
    state = kernel.status("parent")
    history = kernel.history("parent")
    assert state["phase"] != "complete" and state["pending_return"]
    assert state["pending_attempt"] == call.attempt_id
    assert state["budgets"]["requirements"]["developments_used"] == 1
    if consume:
        assert state["gates"] == ["human_blocker"]
    before = durable_snapshot(tmp_path)
    assert kernel.stop_task("idle-stop") == "not_running"
    assert kernel.status("parent") == state and kernel.history("parent") == history
    assert durable_snapshot(tmp_path) == before
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE id='idle-stop'")
        assert tx.one("SELECT executor FROM runs")[0] is None


@pytest.mark.parametrize("boundary", ["dispatched", "unknown", "usage-unreleased"])
def test_stop_fences_host_lifetimes_without_complete_exit_and_usage_proof(
    tmp_path: Path, boundary: str,
) -> None:
    kernel, _, request = _host(tmp_path)
    if boundary == "usage-unreleased":
        running = _running(kernel, request)
        assert kernel.observe_host(request, _exit(running, released=False)) == "exited"
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT executor FROM runs")[0] is None
    else:
        assert kernel.dispatch_host(request)
        if boundary == "unknown":
            assert kernel.observe_host(request, HostObservation("unknown", request.unit_name,
                evidence="Original manager reply unavailable")) == "unknown"
    budget = kernel.status("parent")["budgets"]
    assert kernel.stop_task("fence-unproven-host") == "applied"
    assert kernel.status("parent")["stopped"]
    assert kernel.status("parent")["budgets"] == budget
    with pytest.raises(KernelError):
        kernel.authorize_host(request)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM actions WHERE id='fence-unproven-host'")[0] == "applied"
        assert not tx.one("SELECT status='exited' AND usage_released=1 FROM hosts")[0]


@pytest.mark.parametrize("seat", ["preparation", "unbound-dispatch"])
def test_stop_preserves_unsettled_preparation_or_worker_seat(tmp_path: Path, seat: str) -> None:
    kernel, ports, request = _host(tmp_path)
    running = _running(kernel, request)
    execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, execution, lambda _: ports, prepare_agents=False)
    preparation = operations.until(lambda step: step.preparation is not None).preparation
    assert preparation is not None
    identity = preparation.reservation_id
    if seat == "unbound-dispatch":
        call = kernel.admit_agent(preparation).call
        assert call is not None and kernel.dispatch_agent(call)
        # Dispatch has no process identity. Host exit cannot invent that
        # Worker binding or clear its lease, even after the Executor exits.
        assert kernel.observe_host(request, _exit(running)) == "exited"
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT executor FROM runs")[0] is None
    before = kernel.status("parent")["budgets"]
    assert kernel.stop_task("fence-unsettled-seat") == "applied"
    assert kernel.status("parent")["stopped"]
    assert kernel.status("parent")["budgets"] == before
    state = kernel.operation_state()
    assert identity in (state.preparing if seat == "preparation" else state.active)
    assert not kernel.worker_settled(preparation)


def test_stop_checks_retired_epochs_after_the_original_host_is_known_exited(tmp_path: Path) -> None:
    kernel, _, request = _host(tmp_path)
    running = _running(kernel, request)
    execution = Execution(request.run_id, request.owner, request.epoch)
    # The real failure producer revokes the original epoch before drain.
    with kernel.executor_scope(execution) as close_admission:
        close_admission(OSError("Controlled shutdown before exact exit"))
    # A separate operator Stop can fence that later epoch while exit remains
    # unknown. Observing the original Host settles only its original epoch.
    assert kernel.stop_task("stop-during-retirement") == "applied"
    assert kernel.observe_host(request, _exit(running)) == "exited"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] is None
        retired = [tuple(row) for row in tx.all("SELECT epoch,status FROM retired_executors ORDER BY epoch")]
        assert (request.epoch, "exited") in retired
        assert any(epoch != request.epoch and status == "pending" for epoch, status in retired)
    budget = kernel.status("parent")["budgets"]
    assert kernel.stop_task("fence-unsettled-retirement") == "applied"
    assert kernel.status("parent")["budgets"] == budget
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.all("SELECT epoch,status FROM retired_executors ORDER BY epoch")] == retired
        assert tx.one("SELECT status FROM actions WHERE id='fence-unsettled-retirement'")[0] == "applied"


def test_stop_preserves_noncancellable_final_approval(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.spec.repository_root is not None
    task = TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, scenario.spec.parent_issue)
    # Scenario creates through Core; establish the normal Task mutex before
    # measuring durable state, as the formal creation entry point does.
    with task_lock(task):
        pass
    assert scenario.run().kind == "approval"
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert kernel.control("approve", "approve-original", subject=subject, world=scenario.world()) == "pending"
    # Scenario has no OS Host. Retire its synthetic Executor through the
    # failure producer, then consume exact exit evidence before testing Stop.
    with kernel.executor_scope(scenario.execution) as close_admission:
        close_admission(OSError("Controlled exit after final approval was accepted"))
    kernel.confirm_executor_exit(scenario.execution)
    state, history = kernel.status(subject), kernel.history(subject)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT final_action FROM runs")[0] == "approve-original"
        assert tx.one("SELECT executor FROM runs")[0] is None
        assert tx.one("SELECT status FROM retired_executors WHERE owner=? AND epoch=?",
                      (scenario.execution.owner, scenario.execution.epoch))[0] == "exited"
        assert not tx.rows("SELECT owner FROM retired_executors WHERE status!='exited'")
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT scope_id FROM waits WHERE kind='agent_preparation'")
        assert not tx.rows("SELECT owner FROM hosts")
        grants = [tuple(row) for row in tx.all("SELECT * FROM grants ORDER BY id")]
        assert len(grants) == 1
        assert tx.one("SELECT status FROM actions WHERE id='approve-original'")[0] == "pending"
    before = durable_snapshot(tmp_path)
    with pytest.raises(KernelError, match="final_approval_noncancellable"):
        kernel.stop_task("cannot-cancel-final")
    assert kernel.status(subject) == state and kernel.history(subject) == history
    assert durable_snapshot(tmp_path) == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT final_action FROM runs")[0] == "approve-original"
        assert [tuple(row) for row in tx.all("SELECT * FROM grants ORDER BY id")] == grants
        assert tx.one("SELECT status FROM actions WHERE id='approve-original'")[0] == "pending"
        assert not tx.rows("SELECT id FROM actions WHERE id='cannot-cancel-final'")
