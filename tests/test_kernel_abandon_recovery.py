"""The original Abandon Action owns two finite, separately settled effects."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import KernelError
from agent_run_kernel.model import Effect, EffectResult, Execution, HostObservation
from test_kernel_abandon import managed_abandon_scenario


def recovery_effect(tmp_path: Path, operation: str):
    scenario, request = managed_abandon_scenario(tmp_path)
    assert scenario.run().kind == "complete"
    kernel = scenario.kernel
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    for _ in range(40):
        step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        scenario.select(step.subject)
        if step.effect is not None and step.effect.operation == operation:
            return scenario, step.effect
        scenario.handle(step)
    raise AssertionError(f"Abandon did not prepare {operation}")


def dispatch_recovery(scenario, effect: Effect) -> None:
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world(),
        resource_inspections=scenario.port.inspect_resources(scenario.kernel.effect_resources(effect)))


@pytest.mark.parametrize("operation", ["reopen_ticket", "recovery_comment"])
def test_unproven_sent_recovery_never_replays_and_uses_original_read_window(tmp_path: Path, operation: str) -> None:
    scenario, effect = recovery_effect(tmp_path, operation)
    kernel = scenario.kernel
    now = [1000.25]
    kernel.clock = lambda: now[0]
    dispatch_recovery(scenario, effect)
    original_writes = scenario.port.writes()
    for disposition in ("prior", "unknown"):
        waiting = kernel.reconcile(effect, EffectResult(disposition), scenario.execution, scenario.world())
        assert waiting.kind == "waiting" and waiting.retry_at == 1005.25
        assert kernel.scheduling()["disposition"] == "waiting"
        assert kernel.scheduling()["next_deadline_at"] == waiting.retry_at
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
            assert row["status"] == "sent" and row["attempts"] == 1
            wait = tx.one("SELECT * FROM waits WHERE subject=?", (effect.subject,))
            assert wait["started_at"] == 1000.25 and wait["deadline"] == 1600.25
        assert scenario.port.writes() == original_writes
    token = kernel.begin_automatic_step(scenario.execution, effect.subject)
    assert kernel.guard_automatic_step(token, scenario.execution, readback=effect).kind == "waiting"
    assert kernel.guard_automatic_step(token, scenario.execution, after_read=True, readback=effect) is None
    now[0] = 1600.25
    assert kernel.reconcile(effect, EffectResult("prior"), scenario.execution, scenario.world()).reason == "supervision_timeout"
    assert kernel.scheduling()["disposition"] == "quiescent"
    for after_read in (False, True):
        assert kernel.guard_automatic_step(token, scenario.execution, after_read=after_read, readback=effect).reason == "supervision_timeout"
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.guard_effect(effect, scenario.execution, scenario.world(),
            resource_inspections=scenario.port.inspect_resources(kernel.effect_resources(effect)))
    assert scenario.port.writes() == original_writes
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.admit_host("wrong-action", kind="abandon", continuation_of="foreign-abandon", command=("native-host",),
                          environment_carrier=tmp_path / "wrong-carrier", preflight=lambda: None)
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.admit_host("still-running", kind="abandon", continuation_of="original-abandon", command=("native-host",),
                          environment_carrier=tmp_path / "running-carrier", preflight=lambda: None)
    request = kernel.host_request("start")
    exited = HostObservation("exited", request.unit_name, "original", "/host", "boot", 42, "start",
                             exit_status=0, ended_at=now[0])
    assert kernel.observe_host(request, exited) == "exited"
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.admit_host("exit-unreleased", kind="abandon", continuation_of="original-abandon", command=("native-host",),
                          environment_carrier=tmp_path / "unreleased-carrier", preflight=lambda: None)
    assert kernel.observe_host(request, replace(exited, usage_released=True)) == "exited"
    continuation = kernel.admit_host("explicit-continue", kind="abandon", continuation_of="original-abandon",
        command=("native-host",), environment_carrier=tmp_path / "continued-carrier", preflight=lambda: None)
    assert kernel.status("ticket:4")["gates"] == ["operator_stopped"]
    assert "supervision_timeout" not in kernel.status(effect.subject)["gates"]
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "continued", "/continued", "boot",
                                                           43, "continued-start", usage_ready=True)) == "running"
    scenario.execution = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    original = kernel.advance(effect.subject, scenario.execution, scenario.world())
    assert original.kind == "reconcile" and original.effect.effect_id == effect.effect_id
    waiting = kernel.reconcile(original.effect, EffectResult("prior"), scenario.execution, scenario.world())
    assert waiting.kind == "waiting" and waiting.retry_at == 1605.25
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
        assert row["status"] == "sent" and row["attempts"] == 1
        wait = tx.one("SELECT * FROM waits WHERE subject=?", (effect.subject,))
        assert wait["started_at"] == 1600.25 and wait["deadline"] == 2200.25
    assert scenario.port.writes() == original_writes


def test_abandon_read_wait_preserves_action_scope_without_bypassing_ordinary_stop(tmp_path: Path) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    assert scenario.run().kind == "complete"
    kernel = scenario.kernel
    before_abandon = kernel.begin_automatic_step(scenario.execution, scenario.delivery.key)
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    assert kernel.wait_for_read(before_abandon, scenario.execution,
        "github_recovery_observation_pending", "Previous business scope").reason == "observation_superseded"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")
    token = kernel.begin_automatic_step(scenario.execution, scenario.delivery.key)
    assert kernel.source_status()["stopped"] is True
    waiting = kernel.wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Timeline is not current")
    assert waiting.kind == "waiting" and waiting.retry_at is not None
    assert kernel.guard_automatic_step(token, scenario.execution).kind == "waiting"
    assert kernel.guard_automatic_step(token, scenario.execution, after_read=True) is None
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.wait_for_read(token, replace(scenario.execution, epoch=scenario.execution.epoch + 1),
                             "github_recovery_observation_pending", "Foreign Executor")
    ordinary, _ = managed_abandon_scenario(tmp_path / "ordinary")
    before_stop = ordinary.kernel.begin_automatic_step(ordinary.execution, ordinary.delivery.key)
    ordinary.kernel.control("stop", "ordinary-local-stop", subject=ordinary.delivery.key)
    assert ordinary.kernel.wait_for_read(before_stop, ordinary.execution,
        "github_recovery_observation_pending", "Ordinary Stop").kind == "paused"
    with ordinary.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")


def test_explicit_abandon_continuation_clears_only_its_run_scoped_timeout(tmp_path: Path) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    now = [1000.25]
    kernel.clock = lambda: now[0]
    token = kernel.begin_automatic_step(scenario.execution)
    assert kernel.wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Run read").kind == "waiting"
    now[0] = 1600.25
    assert kernel.wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Run read").reason == "supervision_timeout"
    assert kernel.source_status()["blocked"] == "supervision_timeout"
    assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot", 42, "start",
        exit_status=0, ended_at=now[0], usage_released=True)) == "exited"
    continuation = kernel.admit_host("explicit-continue", kind="abandon", continuation_of="original-abandon",
        command=("native-host",), environment_carrier=tmp_path / "continued-carrier", preflight=lambda: None)
    assert kernel.source_status()["blocked"] is None
    assert kernel.status("ticket:4")["gates"] == ["operator_stopped"]
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "continued", "/continued", "boot",
                                                           43, "continued-start", usage_ready=True)) == "running"
    execution = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    token = kernel.begin_automatic_step(execution)
    assert kernel.wait_for_read(token, execution, "github_recovery_observation_pending", "Original Run read").retry_at == 1605.25


@pytest.mark.parametrize("invalid_event", [None, {"id": 999999, "event": "reopened", "actor": "foreign"}])
def test_recovery_comment_requires_original_confirmed_own_reopen(tmp_path: Path, invalid_event) -> None:
    scenario, effect = recovery_effect(tmp_path, "reopen_ticket")
    kernel = scenario.kernel
    dispatch_recovery(scenario, effect)
    result = scenario.port.execute(effect, lambda: kernel.guard_effect(effect, scenario.execution, scenario.world(),
        resource_inspections=scenario.port.inspect_resources(kernel.effect_resources(effect))))
    assert result.disposition == "target"
    invalid = replace(result, raw={**result.raw, "already_open": True, "reopened_event": invalid_event})
    assert kernel.reconcile(effect, invalid, scenario.execution, scenario.world()).kind == "progress"
    with pytest.raises(KernelError, match="foreign_ticket_recovery_receipt"):
        kernel.advance(effect.subject, scenario.execution, scenario.world())
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        original = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
        assert json.loads(tx.get(original["receipt_id"], effect.subject))["raw"]["reopened_event"] == invalid_event


def test_later_close_ends_original_recovery_ownership_even_after_another_own_reopen(tmp_path: Path) -> None:
    scenario, effect = recovery_effect(tmp_path, "reopen_ticket")
    kernel = scenario.kernel
    dispatch_recovery(scenario, effect)
    original = effect.payload["close_receipt"]["raw"]["event"]
    latest_close = {**original, "id": original["id"] + 1}
    latest_reopen = {**original, "id": original["id"] + 2, "event": "reopened"}
    result = EffectResult("third", raw={"issue": effect.payload["issue"], "state": "OPEN", "prior_close": original,
        "latest_close": latest_close, "latest_reopen": latest_reopen, "reason": "not_owned"})
    assert kernel.reconcile(effect, result, scenario.execution, scenario.world()).kind == "progress"
    assert kernel.advance(effect.subject, scenario.execution, scenario.world()).kind == "progress"
    next_step = kernel.advance(effect.subject, scenario.execution, scenario.world())
    assert next_step.effect is not None and next_step.effect.operation == "abandon_worktree"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        receipt = tx.one("SELECT receipt_id,status FROM effects WHERE id=?", (effect.effect_id,))
        assert receipt["status"] == "confirmed"
        assert json.loads(tx.get(receipt["receipt_id"], effect.subject))["raw"] == result.raw
    assert not [write for write in scenario.port.writes() if write["operation"] in {"reopen_ticket", "recovery_comment"}]
