"""Direct Core quiescence and admitted Host recovery at a manual write gate.

Agent calls before the cut are fixture files/raw; no Agent starts during either
operational observation. Git/SQLite and the admitted executor.run loop remain real. A
controlled systemd process and OS sandbox acceptance are separate obligations.
"""
import os
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, KernelError, executor
from agent_run_kernel.model import Execution, HostObservation
from support.offline import Scenario


class _NoNotifications:
    def __init__(self, *_arguments):
        pass

    def observe_committed(self):
        pass

    def close(self, **_arguments):
        pass


def test_direct_claim_quiesces_at_unknown_push_and_resume_reads_its_original(
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
    # This fixture has a direct Core claim, not an admitted native Host. Its
    # passive state never activates an operational poll or grants a fresh write.
    scenario.port.force_read.clear()
    runner = scenario.runner()
    with monkeypatch.context() as passive:
        passive.setattr(runner.ports, "source", lambda *a: pytest.fail("passive state reads no source"))
        passive.setattr(runner.ports, "delivery", lambda *a: pytest.fail("passive state reads no delivery"))
        assert runner.state().disposition == "quiescent"
        with pytest.raises(KernelError, match="managed_observation_required"):
            runner.activate_observation()
        assert runner.state().disposition == "quiescent"
    assert reconciled == []
    assert scenario.port.writes() == writes
    assert kernel.status(scenario.delivery.key)["pending_write"]["id"] == original["id"]
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_result_unknown"]

    kernel.control("resume", "resume-original-push", subject=scenario.delivery.key)
    for _ in range(12):
        step = runner.run_once()
        assert step.call is None
        if kernel.status(scenario.delivery.key)["phase"] == "approval":
            break
    else:
        raise AssertionError("Explicit resume did not consume the original push")
    assert original["id"] in reconciled
    assert kernel.status(scenario.delivery.key)["phase"] == "approval"
    assert scenario.role_calls == calls
    assert sum(row["operation"] == "push" for row in scenario.port.writes()) == 1
    assert any(event["kind"] == "effect_readback" and event["identity"] == original["id"]
               for event in kernel.history(scenario.delivery.key))
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts")[0] == attempts
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original["id"],))) == ("confirmed", 1)


def test_public_run_reads_a_restored_third_push_before_conflict_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same expectation on old 259/2078 Host and the shared operation Host.

    Only the physical I/O interface differs between the two source versions.
    This invokes the public admission/observation/control APIs and the real
    Host loop; it never calls run_once/advance/reconcile as a recovery shortcut.
    The manager receipt is controlled, as in the existing native Host tests.
    """
    scenario = Scenario(tmp_path / "third", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    scenario.port.force_read["push"] = "third"
    for _ in range(48):
        scenario.step()
        if kernel.status(scenario.delivery.key)["gates"] == ["write_conflict"]:
            break
    else:
        raise AssertionError("The original push never reached its physical third observation")
    original = kernel.status(scenario.delivery.key)["pending_write"]
    assert original["operation"] == "push" and original["status"] == "sent"
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_conflict"]
    # The scripted external observation now exposes the original exact target.
    # The physical push already happened once, and is never replayed here.
    scenario.port.force_read.clear()
    with pytest.raises(KernelError, match="unresolved_write_conflict"):
        kernel.control("resume", "premature-conflict-resume", subject=scenario.delivery.key)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE id='premature-conflict-resume'")

    # End the fixture's original Executor through public Stop/exit/resume.
    # Whole-Run resume intentionally leaves the independent write_conflict.
    kernel.control("stop", "retire-fixture-host")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "resume-task-admission")
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_conflict"]
    # This is the same kind='run' admission used by lifecycle_cli._run.
    # All Action/Execution identities below originate in Kernel's request.
    request = kernel.admit_host("run-to-read-original", kind="run", command=("fixture-native-host",),
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
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT kind,status FROM actions WHERE id='run-to-read-original'")) == ("run", "applied")
        attempts = tx.one("SELECT count(*) FROM attempts")[0]
    calls, writes = list(scenario.role_calls), scenario.port.writes()
    readbacks: list[str] = []
    physical_reconcile = scenario.port.reconcile

    def read_original(effect):
        readbacks.append(effect.effect_id)
        return physical_reconcile(effect)

    monkeypatch.setattr(scenario.port, "reconcile", read_original)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", _NoNotifications)
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex",
                        lambda *a, **kw: pytest.fail("Original push readback needs no Agent"))
    monkeypatch.setattr(executor, "_make_ports", scenario.make_ports)

    assert executor.run(kernel, scenario.execution, {}) == 0
    # A normal explicit run must accept this late target as observation only.
    # Keep the conflict gate until the separate, now-authorizable Resume.
    with kernel.storage.transaction(write=False) as tx:
        state = tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original["id"],)))
        assert tx.one("SELECT count(*) FROM attempts")[0] == attempts
    assert scenario.port.writes() == writes and scenario.role_calls == calls
    assert kernel.status(scenario.delivery.key)["gates"] == ["write_conflict"]
    if state[0] == "sent":
        # Capture the public recovery obstruction as well as the missing poll;
        # this diagnostic branch grants no work and is not a recovery fallback.
        with pytest.raises(KernelError, match="unresolved_write_conflict"):
            kernel.control_host(request, "resume", "still-unresolved-after-run", subject=scenario.delivery.key)
        with kernel.storage.transaction(write=False) as tx:
            assert not tx.rows("SELECT id FROM actions WHERE id='still-unresolved-after-run'")
    assert state == ("observed", 1), {"effect": state, "physical_readbacks": readbacks}
    assert original["id"] in readbacks
    assert kernel.control_host(request, "resume", "resume-confirmed-original", subject=scenario.delivery.key) == "applied"
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert kernel.status(scenario.delivery.key)["phase"] == "approval"
    assert sum(row["operation"] == "push" for row in scenario.port.writes()) == 1
    assert scenario.role_calls == calls
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original["id"],))) == ("confirmed", 1)
