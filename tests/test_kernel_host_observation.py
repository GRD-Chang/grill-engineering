"""An admitted Host gets one finite observation pass, never new write authority."""
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, KernelError
from agent_run_kernel.model import Execution, HostObservation
from agent_run_kernel.offline import Scenario
from agent_run_kernel.query import read


def managed_parent(tmp_path):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("synthetic-native-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    return scenario


def test_activation_rejects_direct_claim_without_external_reads(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    runner = scenario.runner()
    monkeypatch.setattr(runner.ports, "source", lambda parent: pytest.fail("activation is not a physical read"))
    with pytest.raises(KernelError, match="managed_observation_required"):
        runner.activate_observation()
    assert scenario.kernel.status(scenario.delivery.key)["pending_attempt"] is None


def test_managed_activation_reads_the_original_sent_target_once(tmp_path, monkeypatch):
    scenario = managed_parent(tmp_path)
    kernel = scenario.kernel
    now = [100.25]
    kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("push")
    assert scenario.run().kind == "waiting"
    pending = kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == "push" and pending["status"] == "sent"
    scenario.port.force_read["push"] = "third"
    now[0] = 105.25
    runner = scenario.runner()
    assert runner.run_once().kind == "paused"
    scenario.port.force_read.clear()
    assert runner.state().disposition == "quiescent"
    reads = []
    reconcile = runner.ports.reconcile

    def physical(effect):
        reads.append(effect)
        return reconcile(effect)

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("activation grants no new write"))
    monkeypatch.setattr(runner.ports, "source", lambda *a: pytest.fail("original sent readback precedes source"))
    runner.activate_observation()
    assert runner.state().disposition == "ready"
    assert kernel.operation_state().disposition == "quiescent"
    assert runner.run_once().kind == "progress"
    assert len(reads) == 1 and reads[0].effect_id == pending["id"]
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    read(kernel.storage.path, "history")
    read(kernel.storage.path, "status")
    assert len(reads) == 1
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (pending["id"],))) == ("observed", 1)
    assert "write_conflict" in kernel.status(scenario.delivery.key)["gates"]
    assert sum(item["operation"] == "push" for item in scenario.port.writes()) == 1


def test_managed_approval_observation_is_finite_and_passive_afterwards(tmp_path, monkeypatch):
    scenario = managed_parent(tmp_path)
    assert scenario.run().kind == "approval"
    before = scenario.kernel.status(scenario.delivery.key)["budgets"]
    runner = scenario.runner()
    source, delivery = runner.ports.source, runner.ports.delivery
    calls = []

    def original_source(parent):
        calls.append("source")
        return source(parent)

    def original_delivery(request):
        calls.append("delivery")
        return delivery(request)

    monkeypatch.setattr(runner.ports, "source", original_source)
    monkeypatch.setattr(runner.ports, "delivery", original_delivery)
    runner.activate_observation()
    assert calls == [] and runner.state().disposition == "ready"
    assert runner.run_once().kind == "approval"
    assert calls == ["source", "delivery"]
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == before
    assert calls == ["source", "delivery"]
