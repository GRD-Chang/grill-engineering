"""Actual Host supervision at a manual write gate and explicit resume.

Agent calls before the cut are fixture files/raw; no Agent starts during either
Host run. Git/SQLite and the executor.run supervision loop remain real. A
controlled systemd process and OS sandbox acceptance are separate obligations.
"""
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, executor
from agent_run_kernel.offline import Scenario


class _NoNotifications:
    def __init__(self, *_arguments):
        pass

    def observe_committed(self):
        pass

    def close(self, **_arguments):
        pass


def test_host_quiesces_at_unknown_push_and_resumes_only_its_original_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.25]
    kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("push")
    assert scenario.run().kind == "waiting"
    original = kernel.status(scenario.delivery.key)["pending_write"]
    assert original["operation"] == "push" and original["status"] == "sent"
    scenario.port.force_read["push"] = "unknown"
    now[0] = 106.25
    assert scenario.run().kind == "paused"
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_result_unknown"]
    assert scenario.runner().state().disposition == "quiescent"
    with kernel.storage.transaction(write=False) as tx:
        attempts = tx.one("SELECT count(*) FROM attempts")[0]
    calls, writes = list(scenario.role_calls), scenario.port.writes()
    reconciled: list[str] = []
    reconcile = scenario.port.reconcile

    def original_readback(effect):
        reconciled.append(effect.effect_id)
        return reconcile(effect)

    monkeypatch.setattr(scenario.port, "reconcile", original_readback)
    monkeypatch.setattr(executor, "_make_ports", scenario.make_ports)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", _NoNotifications)
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex",
                        lambda *a, **kw: pytest.fail("Original push recovery allocates no Agent"))
    # The real Host exits at this manual boundary without a background poll or
    # a fresh write. An available target is not itself permission to continue.
    scenario.port.force_read.clear()
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert reconciled == []
    assert scenario.port.writes() == writes
    assert kernel.status(scenario.delivery.key)["pending_write"]["id"] == original["id"]
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_result_unknown"]

    kernel.control("resume", "resume-original-push", subject=scenario.delivery.key)
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert original["id"] in reconciled
    assert kernel.status(scenario.delivery.key)["phase"] == "approval"
    assert scenario.role_calls == calls
    assert sum(row["operation"] == "push" for row in scenario.port.writes()) == 1
    assert any(event["kind"] == "effect_readback" and event["identity"] == original["id"]
               for event in kernel.history(scenario.delivery.key))
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts")[0] == attempts
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original["id"],))) == ("confirmed", 1)
