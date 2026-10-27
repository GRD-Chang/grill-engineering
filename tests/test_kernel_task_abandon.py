"""Permanent Task revocation does not reclaim an unknown Host or Worker."""

from support.repository_fixture import create_kernel
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from support.repository_fixture import foreign_task_run
import sqlite3
from test_kernel_requirements import independent_source
from support.core_operations import CoreOperations


@pytest.mark.parametrize("host_state", ["unlaunched", "unknown", "rebound"])
def test_task_abandon_fences_the_exact_task_without_reclaiming_its_host(tmp_path: Path, host_state: str) -> None:
    scenario = GraphScenario(tmp_path / "source", independent_source())
    kernel = create_kernel(tmp_path / "published.sqlite3", scenario.spec, requirements=scenario.requirements)
    scenario.kernel = kernel
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
        assert len(active_before) == 1
        usage_before = kernel.host_usage("original-launch")
        assert usage_before is not None and not usage_before.usage_released
        assert kernel.observe_host(request, HostObservation("unknown", request.unit_name)) == "unknown"
    inspections = scenario.port.inspect_resources(kernel.generation_resources())
    if host_state == "rebound":
        other = foreign_task_run(kernel, scenario.spec, scenario.requirements)
        # The final schema rejects the old JSON-rebinding scenario at its
        # canonical composite FK, before any control or cross-Run mutation.
        from agent_run.storage.routing import RepositoryRoute
        with pytest.raises(sqlite3.IntegrityError):
            with RepositoryRoute.from_database(kernel.storage.path).metadata().transaction(ordered=False) as tx:
                tx.execute("UPDATE tasks SET current_run=? WHERE id=?", ('other-run', task.fingerprint))
        assert kernel.source_status()["abandonment"] is None
        assert other.source_status()["abandonment"] is None
        return
    before_refs, before_writes = scenario.port.remote_refs(), scenario.port.writes()
    assert kernel.abandon_task("abandon-original-task", resource_inspections=inspections) == "pending"
    assert kernel.abandon_task("abandon-original-task", resource_inspections=inspections) == "pending"
    assert kernel.source_status()["abandonment"] == {"action": "abandon-original-task", "status": "pending"}
    assert scenario.port.remote_refs() == before_refs and scenario.port.writes() == before_writes
    assert kernel.active_host_action() == (None if request is None else "original-launch")
    assert kernel.operation_state().active == active_before
    assert kernel.operation_state().cancel == active_before
    if request is not None:
        assert kernel.host_usage("original-launch") == usage_before
        with pytest.raises(KernelError, match="executor_fenced"):
            kernel.guard_agent(step.call)
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.admit_host("cannot-start-business", kind="run", command=("another-host",),
            environment_carrier=tmp_path / "another-carrier", preflight=lambda: None)
    if request is not None:
        with pytest.raises(KernelError, match="executor_exit_unproven"):
            kernel.admit_host("cannot-reclaim-unknown", kind="abandon", continuation_of="abandon-original-task",
                command=("another-host",), environment_carrier=tmp_path / "another-carrier", preflight=lambda: None)
