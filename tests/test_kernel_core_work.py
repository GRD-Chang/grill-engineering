"""A Core request retains the real initial pass and the original Executor fence."""
import pytest

from agent_run.errors import KernelError
from agent_run.model import HostObservation
from agent_run.core.operation_contract import EffectIO
from support.control_scenarios import sent_workspaces


def test_work_keeps_initial_members_and_rejects_a_retired_executor(tmp_path):
    scenario, runner, sent = sent_workspaces(tmp_path, count=1)
    kernel = scenario.kernel
    observation = kernel.activate_observation(scenario.execution)
    assert kernel.activate_observation(scenario.execution) is observation
    request = kernel.next_work(scenario.execution, observation=observation)
    assert isinstance(request, EffectIO)
    assert request.effect == sent[0]
    assert request.work.observation is observation
    assert request.work.member is observation.pending[0]
    result = runner.ports.reconcile(request.effect)
    assert result.disposition == "target"

    kernel.control("stop", "stop-original-reader")
    launch = kernel.host_request("start")
    assert kernel.observe_host(launch, HostObservation("exited", launch.unit_name, "original", "/host", "boot",
        exit_status=0, ended_at=kernel.clock(), usage_released=True, evidence="Controlled manager and usage guardian exited")) == "exited"
    with kernel.storage.transaction(write=False) as tx:
        current_epoch = tx.one("SELECT epoch FROM runs")[0]
    assert current_epoch != request.work.execution.epoch
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.accept_work(request, result)
    assert kernel.status(request.effect.subject)["pending_write"]["status"] == "sent"
    assert kernel.status(request.effect.subject)["pending_attempt"] is None
    assert kernel.fail_work(request.work, KernelError("executor_fenced"), request=request).reason == "executor_fenced"


def test_one_request_chain_never_selects_a_sibling(tmp_path):
    from support.core_operations import setup

    scenario, ports, runner = setup(tmp_path)
    execute = ports.execute
    writes = []

    def physical(effect, authorize):
        writes.append((effect.subject, effect.operation))
        return execute(effect, authorize)

    ports.execute = physical
    step = runner.run_once()
    assert (step.kind, step.subject) == ("progress", "ticket:3")
    assert writes == [("ticket:3", "workspace")]
    assert scenario.kernel.status("ticket:4")["phase"] == "workspace"
    assert scenario.kernel.status("ticket:4")["pending_attempt"] is None
