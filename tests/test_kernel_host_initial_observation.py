"""An admitted Host observes every original gated intent before quiescing.

Real Git, SQLite and executor.run; controlled manager evidence, no live systemd
or Agent. The shared offline adapter supplies bounded physical observations.
"""
import os
from pathlib import Path

import pytest

import agent_run.runtime.executor as executor
from agent_run.model import DeliveryKind, Policy
from agent_run.model import EffectResult, Execution, HostObservation
from support.offline import GraphScenario, Scenario
from test_kernel_requirements import independent_source


def _admit(scenario: Scenario, tmp_path: Path) -> None:
    kernel = scenario.kernel
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "resume-admission")
    request = kernel.admit_host("observe-originals", kind="run", command=("fixture-native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    group = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::"))
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    assert kernel.observe_host(request, HostObservation(
        "running", request.unit_name, "controlled-" + request.nonce, group,
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(), os.getpid(), start,
        usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)


def test_admitted_run_does_not_starve_later_gated_intent_behind_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path / "graph", independent_source())
    kernel = scenario.kernel
    subjects = tuple(f"ticket:{number}" for number in scenario.requirements.parent.sub_issue_numbers)
    assert len(subjects) == 2
    # Each independent Ticket's real workspace operation succeeds physically,
    # while its response/readback reports a third value. No Agent is needed.
    scenario.port.force_read["workspace"] = "third"
    for _ in range(12):
        step = scenario.step()
        assert step.call is None
        if all(kernel.status(subject)["gates"] == ["write_conflict"] for subject in subjects):
            break
    else:
        raise AssertionError("Both original workspace intents must reach their conflict gates")
    originals = tuple(kernel.status(subject)["pending_write"] for subject in subjects)
    assert all(item["operation"] == "workspace" and item["status"] == "sent" for item in originals)
    scenario.port.force_read.clear()
    writes = scenario.port.writes()
    _admit(scenario, tmp_path)
    readbacks: list[str] = []
    reconcile = scenario.port.reconcile

    def original_readback(effect):
        readbacks.append(effect.effect_id)
        assert len(readbacks) <= 2, "One Host activation must not busy-poll a retained gate"
        if effect.effect_id == originals[0]["id"]:
            return EffectResult("unknown")
        return reconcile(effect)

    monkeypatch.setattr(scenario.port, "reconcile", original_readback)
    monkeypatch.setattr(executor, "_make_ports", scenario.make_ports)
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex',
                        lambda *a, **kw: pytest.fail("Gated intent observation allocates no Agent"))
    assert kernel.operation_state().disposition == "quiescent"
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert len(readbacks) == 2 and set(readbacks) == {item["id"] for item in originals}
    assert scenario.port.writes() == writes and scenario.role_calls == []
    assert all("write_conflict" in kernel.status(subject)["gates"] for subject in subjects)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (originals[0]["id"],))) == ("sent", 1)
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (originals[1]["id"],))) == ("observed", 1)


@pytest.mark.parametrize("target_append", [False, True], ids=["unchanged", "target_append"])
def test_admitted_run_observes_approval_without_pending_intent_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_append: bool,
) -> None:
    scenario = Scenario(tmp_path / "approval", DeliveryKind.PARENT_ONLY, policy=Policy(resync_limit=0))
    assert scenario.run().kind == "approval"
    kernel, subject = scenario.kernel, scenario.delivery.key
    before = kernel.status(subject)
    assert before["pending_write"] is None
    _admit(scenario, tmp_path)
    if target_append:
        scenario.port.external_remote_append(scenario.delivery.target_ref, {"external.txt": "Retained work\n"}, "append")
    writes, calls = scenario.port.writes(), list(scenario.role_calls)
    with kernel.storage.transaction(write=False) as tx:
        attempts = tx.one("SELECT count(*) FROM attempts")[0]
    reads: list[str] = []
    source, delivery = scenario.port.source, scenario.port.delivery

    def source_read(*args, **kwargs):
        reads.append("source")
        assert len(reads) <= 2, "Approval is one finite observation, not a background poll"
        return source(*args, **kwargs)

    def delivery_read(request, *args, **kwargs):
        reads.append("delivery")
        assert len(reads) <= 2
        assert request.source_ref == scenario.delivery.source_ref
        assert request.target_ref == scenario.delivery.target_ref and request.read_pr
        facts = delivery(request, *args, **kwargs)
        assert facts.pr is not None and facts.pr.head == facts.source
        return facts

    monkeypatch.setattr(scenario.port, "source", source_read)
    monkeypatch.setattr(scenario.port, "delivery", delivery_read)
    monkeypatch.setattr(executor, "_make_ports", scenario.make_ports)
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex',
                        lambda *a, **kw: pytest.fail("Approval observation allocates no Agent"))
    assert kernel.operation_state().disposition == "quiescent"
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert reads == ["source", "delivery"]
    after = kernel.status(subject)
    assert after["budgets"] == before["budgets"]
    assert scenario.port.writes() == writes and scenario.role_calls == calls
    assert after["gates"] == (["integration_resync_exhausted"] if target_append else [])
    assert kernel.operation_state().disposition == "quiescent"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts")[0] == attempts
        assert not tx.rows("SELECT id FROM leases")
