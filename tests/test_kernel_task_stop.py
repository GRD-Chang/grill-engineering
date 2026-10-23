"""Pure durable Stop uses the exact Task even without a running Host."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from agent_run.storage.tasks import bind_task, locked_task
from test_kernel_requirements import independent_source
from support.core_operations import CoreOperations


@pytest.mark.parametrize("host_state", ["unlaunched", "unknown", "rebound"])
def test_task_stop_uses_the_exact_pointer_without_launching_a_host(tmp_path: Path, host_state: str) -> None:
    scenario = GraphScenario(tmp_path / "source", independent_source())
    kernel = Kernel.create_task(tmp_path / "published.sqlite3", scenario.spec, requirements=scenario.requirements)
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
        other = Kernel.create(tmp_path / "other.sqlite3", replace(scenario.spec, run_id="other-run"), requirements=scenario.requirements)
        with locked_task(task) as location:
            bind_task(location, "other-run", other.storage.path)
        with pytest.raises(KernelError, match="task_location_identity"):
            kernel.stop_task("stop-original-task")
        assert kernel.source_status()["stopped"] is False
        assert other.source_status()["stopped"] is False
        return
    before_refs = scenario.port.remote_refs()
    before_writes = scenario.port.writes()
    assert kernel.stop_task("stop-original-task") == "applied"
    assert kernel.stop_task("stop-original-task") == "applied"
    assert kernel.source_status()["stopped"] is True
    assert scenario.port.remote_refs() == before_refs
    assert scenario.port.writes() == before_writes
    assert kernel.active_host_action() == (None if request is None else "original-launch")
    if request is not None:
        assert kernel.host_usage("original-launch") == usage_before
        assert kernel.operation_state().active == active_before
        with pytest.raises(KernelError, match="executor_exit_unproven"):
            kernel.admit_host("cannot-replace-unknown", kind="resume", command=("another-host",),
                              environment_carrier=tmp_path / "another-carrier", preflight=lambda: None)
