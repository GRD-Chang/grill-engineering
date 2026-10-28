"""Pure durable Stop uses the exact Task even without a running Host."""

from support.repository_fixture import create_kernel
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from support.repository_fixture import foreign_task_run, read_binding
import sqlite3
from support.requirements_scenarios import independent_source
from support.core_operations import CoreOperations
from support.control_scenarios import managed_parent
from support.environment import durable_snapshot
from agent_run.storage.tasks import task_lock


def test_completed_task_stop_preserves_the_terminal_result_without_an_action(tmp_path: Path) -> None:
    scenario = managed_parent(tmp_path)
    assert scenario.run(approve=True).kind == "complete"
    kernel = scenario.kernel
    request = kernel.host_request("start")
    assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
        exit_status=0, ended_at=111.0, usage_released=True)) == "exited"
    before = kernel.source_status(), kernel.status(scenario.delivery.key), kernel.history(scenario.delivery.key)
    assert before[1]["resource_cleanup"]["state"] == "complete"
    assert kernel.active_host_action() is None and kernel.operation_state().active == ()
    with kernel.storage.transaction(write=False) as tx:
        actions = [dict(row) for row in tx.all("SELECT * FROM actions ORDER BY rowid")]
        assert tx.one("SELECT final_action,executor FROM runs")[0] is None
        assert tx.one("SELECT final_action,executor FROM runs")[1] is None
    assert kernel.stop_task("late-stop") == "complete"
    assert kernel.stop_task("another-late-stop") == "complete"
    assert (kernel.source_status(), kernel.status(scenario.delivery.key), kernel.history(scenario.delivery.key)) == before
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.all("SELECT * FROM actions ORDER BY rowid")] == actions



@pytest.mark.parametrize("host_state", ["unlaunched", "unknown", "rebound"])
def test_task_stop_uses_the_exact_pointer_without_launching_a_host(tmp_path: Path, host_state: str) -> None:
    scenario = GraphScenario(tmp_path / "source", independent_source())
    kernel = create_kernel(tmp_path / "published.sqlite3", scenario.spec, requirements=scenario.requirements)
    task = TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, scenario.spec.parent_issue)
    request = None
    active_before = ()
    usage_before = None
    if host_state == "unknown":
        request = kernel.admit_host("original-launch", kind="run", command=("native-host",),
                                    environment_carrier=tmp_path / "carrier", preflight=lambda: None)
        assert kernel.dispatch_host(request)
        assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                           42, "start", usage_ready=True)) == "running"
        scenario.kernel = kernel
        scenario.execution = Execution(request.run_id, request.owner, request.epoch)
        operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
        for _ in range(20):
            step = operations.once()
            if step.call is not None:
                assert kernel.dispatch_agent(step.call)
                break
        else:
            raise AssertionError("The original Host never allocated its Worker lease")
        active_before = kernel.operation_state().active
        assert active_before == (step.call.lease_id,)
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT invocation FROM leases WHERE id=?", (active_before[0],))[0] == step.call.invocation_id
        usage_before = kernel.host_usage("original-launch")
        assert usage_before is not None and usage_before.usage_ready and not usage_before.usage_released
        assert kernel.observe_host(request, HostObservation("unknown", request.unit_name)) == "unknown"
    if host_state == "rebound":
        other = foreign_task_run(kernel, scenario.spec, scenario.requirements)
        # The final schema rejects the old JSON-rebinding scenario at its
        # canonical composite FK, before any control or cross-Run mutation.
        from agent_run.storage.routing import RepositoryRoute
        with pytest.raises(sqlite3.IntegrityError):
            with RepositoryRoute.from_database(kernel.storage.path).metadata().transaction(ordered=False) as tx:
                tx.execute("UPDATE tasks SET current_run=? WHERE id=?", ('other-run', task.fingerprint))
        assert kernel.source_status()["stopped"] is False
        assert other.source_status()["stopped"] is False
        return
    before_refs = scenario.port.remote_refs()
    before_writes = scenario.port.writes()
    if host_state == "unlaunched":
        # Formal publication already owns this original Task mutex.
        with task_lock(task):
            pass
        binding = read_binding(task, kernel.storage.path)
        assert binding == (scenario.spec.run_id, kernel.storage.path.resolve())
        original = durable_snapshot(tmp_path)
    expected = "not_running" if host_state == "unlaunched" else "applied"
    assert kernel.stop_task("stop-original-task") == expected
    assert kernel.stop_task("stop-original-task") == expected
    assert kernel.source_status()["stopped"] is (host_state != "unlaunched")
    if host_state == "unlaunched":
        assert read_binding(task, kernel.storage.path) == binding
        assert durable_snapshot(tmp_path) == original
    assert scenario.port.remote_refs() == before_refs
    assert scenario.port.writes() == before_writes
    assert kernel.active_host_action() == (None if request is None else "original-launch")
    if request is not None:
        assert kernel.host_usage("original-launch") == usage_before
        assert kernel.operation_state().active == active_before
        with pytest.raises(KernelError, match="executor_exit_unproven"):
            kernel.admit_host("cannot-replace-unknown", kind="resume", command=("another-host",),
                              environment_carrier=tmp_path / "another-carrier", preflight=lambda: None)
