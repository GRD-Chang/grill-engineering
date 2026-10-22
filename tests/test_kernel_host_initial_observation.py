"""An admitted Host observes every original gated intent before quiescing.

Real Git, SQLite and executor.run; controlled manager evidence, no live systemd
or Agent. The shared offline adapter supplies bounded physical observations.
"""
import os
from pathlib import Path

import pytest

from agent_run_kernel import executor
from agent_run_kernel.model import EffectResult, Execution, HostObservation
from agent_run_kernel.offline import GraphScenario
from test_kernel_requirements import independent_source


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
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex",
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
