"""The original Abandon Action owns two finite, separately settled effects."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.errors import KernelError
from agent_run.model import Effect, EffectResult, Execution, HostObservation
from agent_run.queries.run import read
from agent_run.core.operation_contract import EffectIO
from support.control_scenarios import managed_abandon_scenario
from support.core_operations import CoreOperations


def recovery_effect(tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch, response=None):
    """Reach a real recovery effect; interrupt or script only the physical port."""
    scenario, request = managed_abandon_scenario(tmp_path)
    assert scenario.run().kind == "complete"
    kernel = scenario.kernel
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    ports = operations.runner.ports
    execute = ports.execute
    sent = []

    def interrupt(effect, authorize):
        if effect.operation == operation:
            authorize.prepare()()
            sent.append(effect)
            if response is not None:
                return response(effect, authorize, execute)
            raise OSError("Original recovery call interrupted before its physical write")
        return execute(effect, authorize)

    with monkeypatch.context() as patch:
        patch.setattr(ports, "execute", interrupt)
        boundary = operations.until(lambda step: bool(sent))
    if response is None:
        assert boundary.reason == "original_write_readback_required"
    else:
        assert boundary.kind == "progress"
    effect, = sent
    return scenario, effect, operations


@pytest.mark.parametrize("operation", ["reopen_ticket", "recovery_comment"])
def test_unproven_sent_recovery_never_replays_and_uses_original_read_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, subtests: pytest.Subtests,
) -> None:
    scenario, effect, operations = recovery_effect(tmp_path, operation, monkeypatch)
    kernel = scenario.kernel
    now = [1000.25]
    kernel.clock = lambda: now[0]
    original_writes = scenario.port.writes()
    request = kernel.next_work(scenario.execution)
    assert isinstance(request, EffectIO) and request.kind == "readback"
    assert request.effect == effect
    token = request.token
    for disposition in ("prior", "unknown"):
        scenario.port.read_faults.clear()
        if disposition == "unknown":
            scenario.port.read_faults.add("*")
        waiting = operations.once()
        assert waiting.kind == "waiting" and waiting.retry_at == 1005.25
        with subtests.test(projection="read_wait_disposition", disposition=disposition):
            assert kernel.operation_state().disposition == "waiting"
        assert kernel.operation_state().wake_at == waiting.retry_at
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
            assert row["status"] == "sent" and row["attempts"] == 1
            wait = tx.one("SELECT * FROM waits WHERE subject=?", (effect.subject,))
            assert wait["started_at"] == 1000.25 and wait["deadline"] == 1600.25
        assert scenario.port.writes() == original_writes
    # The real selected readback supplied its token before the read window
    # opened. Internal guard assertions allocate no additional work.
    assert kernel._guard_automatic_step(token, scenario.execution, readback=effect).kind == "waiting"
    assert kernel._guard_automatic_step(token, scenario.execution, after_read=True, readback=effect) is None
    now[0] = 1600.25
    scenario.port.read_faults.clear()
    assert operations.once().reason == "supervision_timeout"
    with subtests.test(projection="timeout_disposition"):
        assert kernel.operation_state().disposition == "quiescent"
    for after_read in (False, True):
        assert kernel._guard_automatic_step(token, scenario.execution, after_read=after_read, readback=effect).reason == "supervision_timeout"
    # The public runner cannot grant another write after the timeout.
    with subtests.test(projection="timeout_operation"):
        assert operations.once().kind == "quiescent"
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
    reads = []
    reconcile = scenario.port.reconcile

    def original_read(effect):
        reads.append(effect)
        return reconcile(effect)

    monkeypatch.setattr(scenario.port, "reconcile", original_read)
    continued = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    waiting = continued.once()
    assert reads[0].effect_id == effect.effect_id
    assert waiting.kind == "waiting" and waiting.retry_at == 1605.25
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
        assert row["status"] == "sent" and row["attempts"] == 1
        wait = tx.one("SELECT * FROM waits WHERE subject=?", (effect.subject,))
        assert wait["started_at"] == 1600.25 and wait["deadline"] == 2200.25
    assert scenario.port.writes() == original_writes


def test_abandon_read_wait_preserves_action_scope_without_bypassing_ordinary_stop(tmp_path: Path) -> None:
    """Core read-request consumer: superseded scope, stop and guard semantics.

    The public runner recovery/physical-port integration is exercised above;
    this test deliberately inspects its internal read-wait protocol only.
    """
    scenario, request = managed_abandon_scenario(tmp_path)
    assert scenario.run().kind == "complete"
    kernel = scenario.kernel
    before_abandon = kernel.begin_observation(scenario.delivery.key, execution=scenario.execution).fence
    kernel.control("stop", "retain-subject-stop", subject=scenario.delivery.key)
    assert kernel.control_host(request, "abandon", "original-abandon",
        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources())) == "pending"
    assert kernel._wait_for_read(before_abandon, scenario.execution,
        "github_recovery_observation_pending", "Previous business scope").reason == "observation_superseded"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")
    token = kernel.begin_observation(scenario.delivery.key, execution=scenario.execution).fence
    assert kernel.source_status()["abandonment"] == {"action": "original-abandon", "status": "pending"}
    assert kernel.status(scenario.delivery.key)["gates"] == ["operator_stopped"]
    assert read(kernel.storage.path, "status").data["status"] == "abandonment_pending"
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.control_host(request, "resume", "cannot-resume-business")
    waiting = kernel._wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Timeline is not current")
    assert waiting.kind == "waiting" and waiting.retry_at is not None
    assert kernel._guard_automatic_step(token, scenario.execution).kind == "waiting"
    assert kernel._guard_automatic_step(token, scenario.execution, after_read=True) is None
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel._wait_for_read(token, replace(scenario.execution, epoch=scenario.execution.epoch + 1),
                             "github_recovery_observation_pending", "Foreign Executor")
    ordinary, _ = managed_abandon_scenario(tmp_path / "ordinary")
    before_stop = ordinary.kernel.begin_observation(ordinary.delivery.key, execution=ordinary.execution).fence
    ordinary.kernel.control("stop", "ordinary-local-stop", subject=ordinary.delivery.key)
    assert ordinary.kernel._wait_for_read(before_stop, ordinary.execution,
        "github_recovery_observation_pending", "Ordinary Stop").kind == "paused"
    with ordinary.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")


def test_explicit_abandon_continuation_clears_only_its_run_scoped_timeout(tmp_path: Path) -> None:
    """Internal run-scope wait consumer; Host lifecycle is explicit fixture evidence."""
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    now = [1000.25]
    kernel.clock = lambda: now[0]
    token = kernel.begin_observation(None, execution=scenario.execution).fence
    assert kernel._wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Run read").kind == "waiting"
    now[0] = 1600.25
    assert kernel._wait_for_read(token, scenario.execution, "github_recovery_observation_pending", "Run read").reason == "supervision_timeout"
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
    token = kernel.begin_observation(None, execution=execution).fence
    assert kernel._wait_for_read(token, execution, "github_recovery_observation_pending", "Original Run read").retry_at == 1605.25


@pytest.mark.parametrize("invalid_event", [None, {"id": 999999, "event": "reopened", "actor": "foreign"}])
def test_recovery_comment_requires_original_confirmed_own_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_event,
) -> None:
    def invalid_receipt(effect, authorize, execute):
        result = execute(effect, authorize)
        assert result.disposition == "target"
        return replace(result, raw={**result.raw, "already_open": True, "reopened_event": invalid_event})

    scenario, effect, operations = recovery_effect(tmp_path, "reopen_ticket", monkeypatch, invalid_receipt)
    kernel = scenario.kernel
    with pytest.raises(KernelError, match="foreign_ticket_recovery_receipt"):
        operations.once()
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        original = tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))
        assert json.loads(tx.get(original["receipt_id"], effect.subject))["raw"]["reopened_event"] == invalid_event


def test_later_close_ends_original_recovery_ownership_even_after_another_own_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = []

    def later_close(effect, authorize, execute):
        original = effect.payload["close_receipt"]["raw"]["event"]
        latest_close = {**original, "id": original["id"] + 1}
        latest_reopen = {**original, "id": original["id"] + 2, "event": "reopened"}
        result = EffectResult("third", raw={"issue": effect.payload["issue"], "state": "OPEN", "prior_close": original,
            "latest_close": latest_close, "latest_reopen": latest_reopen, "reason": "not_owned"})
        responses.append(result)
        return result

    scenario, effect, operations = recovery_effect(tmp_path, "reopen_ticket", monkeypatch, later_close)
    kernel = scenario.kernel
    assert operations.once().kind == "progress"
    executed = []
    execute = operations.runner.ports.execute

    def recorded(effect, authorize):
        executed.append(effect)
        return execute(effect, authorize)

    monkeypatch.setattr(operations.runner.ports, "execute", recorded)
    assert operations.once().kind == "progress"
    assert executed[0].operation == "abandon_worktree"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        receipt = tx.one("SELECT receipt_id,status FROM effects WHERE id=?", (effect.effect_id,))
        assert receipt["status"] == "confirmed"
        assert json.loads(tx.get(receipt["receipt_id"], effect.subject))["raw"] == responses[0].raw
    assert not [write for write in scenario.port.writes() if write["operation"] in {"reopen_ticket", "recovery_comment"}]
