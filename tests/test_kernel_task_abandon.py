"""Permanent Task revocation does not reclaim an unknown Host or Worker."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Kernel, KernelError
from agent_run_kernel.model import Execution, HostObservation
from agent_run_kernel.offline import GraphScenario
from agent_run_kernel.task_location import bind_task, locked_task
from test_kernel_requirements import independent_source


@pytest.mark.parametrize("host_state", ["unlaunched", "unknown", "rebound"])
def test_task_abandon_fences_the_exact_task_without_reclaiming_its_host(tmp_path: Path, host_state: str) -> None:
    scenario = GraphScenario(tmp_path / "source", independent_source())
    kernel = Kernel.create_task(tmp_path / "published.sqlite3", scenario.spec, requirements=scenario.requirements)
    scenario.kernel = kernel
    task = TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, scenario.spec.parent_issue)
    request = None
    active_before = []
    usage_before = None
    if host_state == "unknown":
        request = kernel.admit_host("original-launch", kind="run", command=("native-host",),
            environment_carrier=tmp_path / "carrier", preflight=lambda: None)
        assert kernel.dispatch_host(request)
        assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                           42, "start", usage_ready=True)) == "running"
        scenario.execution = Execution(request.run_id, request.owner, request.epoch)
        for _ in range(20):
            step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
            if step.call is not None:
                assert kernel.dispatch_agent(step.call)
                break
            scenario.handle(step)
        else:
            raise AssertionError("The original Host never allocated its Worker lease")
        active_before = kernel.scheduling()["active"]
        assert len(active_before) == 1
        usage_before = kernel.host_usage("original-launch")
        assert usage_before is not None and not usage_before.usage_released
        assert kernel.observe_host(request, HostObservation("unknown", request.unit_name)) == "unknown"
    inspections = scenario.port.inspect_resources(kernel.generation_resources())
    if host_state == "rebound":
        other = Kernel.create(tmp_path / "other.sqlite3", replace(scenario.spec, run_id="other-run"), requirements=scenario.requirements)
        with locked_task(task) as location:
            bind_task(location, "other-run", other.storage.path)
        with pytest.raises(KernelError, match="task_location_identity"):
            kernel.abandon_task("abandon-original-task", resource_inspections=inspections)
        assert kernel.source_status()["abandonment"] is None
        assert other.source_status()["abandonment"] is None
        return
    before_refs, before_writes = scenario.port.remote_refs(), scenario.port.writes()
    assert kernel.abandon_task("abandon-original-task", resource_inspections=inspections) == "pending"
    assert kernel.abandon_task("abandon-original-task", resource_inspections=inspections) == "pending"
    assert kernel.source_status()["abandonment"] == {"action": "abandon-original-task", "status": "pending"}
    assert scenario.port.remote_refs() == before_refs and scenario.port.writes() == before_writes
    assert kernel.active_host_action() == (None if request is None else "original-launch")
    assert kernel.scheduling()["active"] == active_before
    assert kernel.scheduling()["cancel_workers"] is True
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
