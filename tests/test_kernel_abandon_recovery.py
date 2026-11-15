"""The original Abandon Action owns two finite, separately settled effects."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agent_run.errors import KernelError
from agent_run.model import EffectResult, Execution, HostObservation, World, wire_json
from agent_run.queries.run import read
from agent_run.core.operation_contract import EffectIO, Work
from support.core_operations import CoreOperations
from support.requirements_scenarios import independent_source, source_run


def recovery_effect(managed_completed_ticket, monkeypatch: pytest.MonkeyPatch):
    """Reach a real recovery effect; interrupt or script only the physical port."""
    scenario, request = managed_completed_ticket
    kernel = scenario.kernel
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    ports = operations.runner.ports
    execute = ports.execute
    sent = []

    def interrupt(effect, authorize):
        if effect.operation == "recovery_comment":
            authorize.prepare()()
            sent.append(effect)
            raise OSError("Original recovery call interrupted before its physical write")
        return execute(effect, authorize)

    with monkeypatch.context() as patch:
        patch.setattr(ports, "execute", interrupt)
        boundary = operations.until(lambda step: bool(sent))
    assert boundary.reason == "original_write_readback_required"
    effect, = sent
    return scenario, effect, operations


def test_unproven_sent_recovery_never_replays_and_uses_original_read_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subtests: pytest.Subtests, managed_completed_ticket,
) -> None:
    scenario, effect, operations = recovery_effect(managed_completed_ticket, monkeypatch)
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


def managed_read_kernel(tmp_path):
    """Fresh source plus explicit Host observations; no workspace or completion."""
    kernel, _ = source_run(tmp_path / "kernel.sqlite3", independent_source())
    request = kernel.admit_host("start", kind="run", command=("native-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "start", usage_ready=True)) == "running"
    return kernel, request, Execution(request.run_id, request.owner, request.epoch)


def test_abandon_read_wait_preserves_action_scope_without_bypassing_ordinary_stop(tmp_path: Path) -> None:
    """Core read-request consumer: superseded scope, stop and guard semantics.

    The public runner recovery/physical-port integration is exercised above;
    this test deliberately inspects its internal read-wait protocol only.
    """
    kernel, request, execution = managed_read_kernel(tmp_path)
    before_abandon = kernel.begin_observation("ticket:3", execution=execution).fence
    kernel.control("stop", "retain-subject-stop", subject="ticket:3")
    assert kernel.control_host(request, "abandon", "original-abandon",
        resource_inspections=()) == "pending"
    assert kernel._wait_for_read(before_abandon, execution,
        "github_recovery_observation_pending", "Previous business scope").reason == "observation_superseded"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")
    token = kernel.begin_observation("ticket:3", execution=execution).fence
    assert kernel.source_status()["abandonment"] == {"action": "original-abandon", "status": "pending"}
    assert kernel.status("ticket:3")["gates"] == ["operator_stopped"]
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data["status"] == "abandonment_pending"
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.control_host(request, "resume", "cannot-resume-business")
    waiting = kernel._wait_for_read(token, execution, "github_recovery_observation_pending", "Timeline is not current")
    assert waiting.kind == "waiting" and waiting.retry_at is not None
    assert kernel._guard_automatic_step(token, execution).kind == "waiting"
    assert kernel._guard_automatic_step(token, execution, after_read=True) is None
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel._wait_for_read(token, replace(execution, epoch=execution.epoch + 1),
                             "github_recovery_observation_pending", "Foreign Executor")
    ordinary, _, ordinary_execution = managed_read_kernel(tmp_path / "ordinary")
    before_stop = ordinary.begin_observation("ticket:3", execution=ordinary_execution).fence
    ordinary.control("stop", "ordinary-local-stop", subject="ticket:3")
    assert ordinary._wait_for_read(before_stop, ordinary_execution,
        "github_recovery_observation_pending", "Ordinary Stop").kind == "paused"
    with ordinary.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT * FROM waits")


def test_explicit_abandon_continuation_clears_only_its_run_scoped_timeout(tmp_path: Path) -> None:
    """Internal run-scope wait consumer; Host lifecycle is explicit fixture evidence."""
    kernel, request, execution = managed_read_kernel(tmp_path)
    kernel.control("stop", "retain-sibling-gate", subject="ticket:4")
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=())
    now = [1000.25]
    kernel.clock = lambda: now[0]
    token = kernel.begin_observation(None, execution=execution).fence
    assert kernel._wait_for_read(token, execution, "github_recovery_observation_pending", "Run read").kind == "waiting"
    now[0] = 1600.25
    assert kernel._wait_for_read(token, execution, "github_recovery_observation_pending", "Run read").reason == "supervision_timeout"
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


def advance_recovery(kernel, work, world):
    with kernel.storage.transaction() as tx:
        return kernel._advance_abandon(tx, tx.one("SELECT * FROM runs"),
            kernel._abandon_action(tx), work.subject, work, world)


@pytest.fixture
def recorded_recovery(tmp_path):
    """Recorded close + Completion are inputs to recovery, not lifecycle proof.

    The main abandonment journey produces these through real Git/API calls.
    Here Core creates the Action, inspection and recovery intent itself.
    """
    kernel, _ = source_run(tmp_path / "recorded.sqlite3", independent_source())
    execution = kernel.claim_executor("recovery-reader")
    subject = "ticket:3"
    delivery = kernel.delivery(subject)
    event = {"id": 11, "event": "closed", "actor": "publisher"}
    with kernel.storage.transaction() as tx:
        run = tx.one("SELECT * FROM runs")
        payload = tx.put(subject, "original_close_intent", wire_json({"close_intent": {"actor": "publisher"}}))
        receipt = tx.put(subject, "original_close_receipt", wire_json(asdict(EffectResult("target", raw={"event": event}))))
        for operation in ("completion_comment", "close"):
            tx.execute("INSERT INTO effects(run_id,id,subject,operation,status,parent_revision,graph_revision,revision,"
                "generation,source_ref,target_ref,before_sha,base_sha,base_tree,actor,payload_id,receipt_id) "
                "VALUES (?,?,?,?,'confirmed',?,?,?,1,?,?,?,?,?,'publisher',?,?)",
                (run["id"], operation, subject, operation, run["parent_revision"], run["graph_revision"], delivery.revision,
                 delivery.source_ref, delivery.target_ref, delivery.base.sha, delivery.base.sha, delivery.base.tree, payload, receipt))
        tx.execute("INSERT INTO completions(run_id,subject,generation,revision,parent_revision,graph_revision,source_sha,"
            "merge_sha,tree,pr_number,gate_mode,comment_effect,close_effect) VALUES (?,?,1,?,?,?,?,?,?,11,'ci_waiver','completion_comment','close')",
            (run["id"], subject, delivery.revision, run["parent_revision"], run["graph_revision"],
             delivery.base.sha, delivery.base.sha, delivery.base.tree))
        tx.execute("UPDATE subjects SET phase='complete',merge_sha=? WHERE id=?", (delivery.base.sha, subject))
        identity = kernel._inputs(tx, run)["runner_identity"]
        world = World(run["parent_revision"], run["graph_revision"], delivery.revision,
            delivery.base, delivery.base, publisher_actor="publisher", publisher_identity=identity)
    assert kernel.control("abandon", "original-abandon", resource_inspections=()) == "pending"
    work = Work(execution, "abandon", subject, False, True, None, None)
    inspection = advance_recovery(kernel, work, world)
    assert inspection.effect.operation == "inspect_resources"
    assert kernel._dispatch(inspection.effect, execution, world, token=inspection.token)
    assert kernel._reconcile(inspection, EffectResult("target", raw={"resources": []})).kind == "progress"
    assert advance_recovery(kernel, work, world).kind == "progress"
    request = advance_recovery(kernel, work, world)
    assert request.effect.operation == "reopen_ticket"
    assert kernel._dispatch(request.effect, execution, world, token=request.token, resource_inspections=())
    return kernel, request, world


def test_unproven_reopen_keeps_one_read_window_without_replay(recorded_recovery):
    kernel, request, world = recorded_recovery
    kernel.clock = lambda: 100.25
    for disposition in ("prior", "unknown"):
        waiting = kernel._reconcile(request, EffectResult(disposition))
        assert (waiting.kind, waiting.retry_at) == ("waiting", 105.25)
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE operation='reopen_ticket'")) == ("sent", 1)
    kernel.clock = lambda: 700.25
    assert kernel._reconcile(request, EffectResult("prior")).reason == "supervision_timeout"
    assert advance_recovery(kernel, request.work, world).reason == "abandon_outcome_unproven"
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE operation='reopen_ticket'")) == ("sent", 1)


@pytest.mark.parametrize("invalid_event", [None, {"id": 999999, "event": "reopened", "actor": "foreign"}])
def test_recovery_comment_requires_original_confirmed_own_reopen(recorded_recovery, invalid_event):
    kernel, request, world = recorded_recovery
    original = request.effect.payload["close_receipt"]["raw"]["event"]
    receipt = EffectResult("target", raw={"issue": 3, "state": "OPEN", "prior_close": original,
        "already_open": True, "reopened_event": invalid_event})
    assert kernel._reconcile(request, receipt).kind == "progress"
    with pytest.raises(KernelError, match="foreign_ticket_recovery_receipt"):
        advance_recovery(kernel, request.work, world)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        row = tx.one("SELECT * FROM effects WHERE id=?", (request.effect.effect_id,))
        assert row["status"] == "observed"
        assert json.loads(tx.get(row["receipt_id"], request.effect.subject))["raw"]["reopened_event"] == invalid_event


def test_later_close_ends_original_recovery_ownership_even_after_another_own_reopen(recorded_recovery):
    kernel, request, world = recorded_recovery
    original = request.effect.payload["close_receipt"]["raw"]["event"]
    receipt = EffectResult("third", raw={"issue": 3, "state": "OPEN", "prior_close": original,
        "latest_close": {**original, "id": original["id"] + 1},
        "latest_reopen": {**original, "id": original["id"] + 2, "event": "reopened"}, "reason": "not_owned"})
    assert kernel._reconcile(request, receipt).kind == "progress"
    assert advance_recovery(kernel, request.work, world).kind == "progress"
    assert advance_recovery(kernel, request.work, world).kind == "complete"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='recovery_comment'")
        row = tx.one("SELECT status,receipt_id FROM effects WHERE id=?", (request.effect.effect_id,))
        assert row["status"] == "confirmed"
        assert json.loads(tx.get(row["receipt_id"], request.effect.subject))["raw"] == receipt.raw
