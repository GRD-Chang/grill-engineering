"""Managed abandonment retains exact recovery obligations and authority."""
from agent_run.core.operation_runner import OperationRunner

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Execution, HostLaunch, HostObservation, ResourceInspection
from support.offline import GraphScenario, Scenario
from test_kernel_requirements import independent_source
from support.core_operations import CoreOperations, scripted_return


def _merge_response_lost(scenario, monkeypatch: pytest.MonkeyPatch):
    """Send the real approved merge, then interrupt its response at the port."""
    sent = []
    execute = scenario.port.execute

    def lose_merge(effect, authorize):
        result = execute(effect, authorize)
        if effect.operation == "merge":
            sent.append(effect)
            raise OSError("Original merge response lost")
        return result

    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    with monkeypatch.context() as patch:
        patch.setattr(scenario.port, "execute", lose_merge)
        boundary = operations.until(lambda step: bool(sent))
    assert boundary.reason == "original_write_readback_required"
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize("uncertain", ["unknown", "third"])
def test_abandon_cannot_take_over_an_unsettled_final_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: str,
) -> None:
    source = independent_source()
    source = replace(source, parent=replace(source.parent, sub_issue_numbers=(3,)), issues=(source.issues[0],))
    scenario = GraphScenario(tmp_path / "merged-unknown", source)
    assert scenario.run().kind == "complete"
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    operations.until(lambda step: scenario.kernel.source_status()["final_subject"] is not None)
    scenario.select(scenario.kernel.source_status()["final_subject"])
    assert scenario.run().kind == "approval"
    scenario.kernel.control("approve", "original-final-approval", subject=scenario.delivery.key, world=scenario.world())
    merge = _merge_response_lost(scenario, monkeypatch)
    scenario.port.force_read["merge"] = uncertain
    assert operations.once().kind == "paused"
    resources = scenario.kernel.generation_resources()
    inspections = scenario.port.inspect_resources(resources)
    with pytest.raises(KernelError, match="final_merge_unsettled"):
        scenario.kernel.control("abandon", "cannot-abandon-default-merge", resource_inspections=inspections)
    assert scenario.kernel.source_status()["abandonment"] is None
    scenario.port.force_read.clear()
    assert operations.once().kind == "progress"
    scenario.kernel.control("resume", "resume-original-closeout", subject=merge.subject)
    assert scenario.run().kind == "complete"
    assert scenario.port.issue_state(3) == "CLOSED"
    assert not [write for write in scenario.port.writes() if write["operation"] == "reopen_ticket"]


@pytest.mark.parametrize("acknowledgement", ["confirmed", "unknown", "third"])
def test_parent_only_default_merge_cannot_be_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledgement: str,
) -> None:
    scenario = Scenario(tmp_path / "parent-only", DeliveryKind.PARENT_ONLY)
    kernel, subject = scenario.kernel, scenario.delivery.key
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    assert scenario.run().kind == "approval"
    before = scenario.port.remote_commit(scenario.delivery.target_ref)
    kernel.control("approve", "approve-default", subject=subject, world=scenario.world())
    _merge_response_lost(scenario, monkeypatch)
    assert scenario.port.remote_commit(scenario.delivery.target_ref) != before
    if acknowledgement == "confirmed":
        assert operations.once().kind == "progress"
        assert scenario.run().kind == "complete"
        expected = "merged_run_noncancellable"
    else:
        scenario.port.force_read["merge"] = acknowledgement
        assert operations.once().kind == "paused"
        expected = "final_merge_unsettled"
    with pytest.raises(KernelError, match=expected):
        kernel.control("abandon", "reject-default-abandon", resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    assert kernel.source_status()["abandonment"] is None
    scenario.port.force_read.clear()
    if acknowledgement != "confirmed":
        assert operations.once().kind == "progress"
        kernel.control("resume", "resume-default-closeout", subject=subject)
    assert scenario.run().kind == "complete"
    assert len([write for write in scenario.port.writes() if write["operation"] == "merge"]) == 1
    assert not [write for write in scenario.port.writes() if write["operation"] == "reopen_ticket"]


def test_abandon_dirty_observation_is_quiescent_until_explicit_continuation(tmp_path: Path) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    (Path(scenario.delivery.checkout) / "late-work.txt").write_text("Late original work\n")
    inspected = scenario.runner().run_once()
    assert inspected.kind == "progress"
    assert scenario.runner().run_once().kind == "quiescent"
    with kernel.storage.transaction(write=False) as tx:
        receipt = tx.one("SELECT receipt_id,subject FROM effects WHERE operation='inspect_resources' ORDER BY rowid DESC LIMIT 1")
        observed = json.loads(tx.get(receipt["receipt_id"], receipt["subject"]))
        assert any(item["disposition"] == "dirty" for item in observed["raw"]["resources"])
    assert kernel.operation_state().disposition == "quiescent"
    assert kernel.operation_state().disposition == "quiescent"
    assert not [write for write in scenario.port.writes() if write["operation"] in kernel.REMOTE]


@pytest.mark.parametrize("uncertainty", ["unknown", "third", "prior_after_continuation"])
def test_abandon_continuation_settles_the_original_sent_intent_before_fresh_inspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertainty: str) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    for _ in range(100):
        scenario.step()
        if kernel.status(scenario.delivery.key)["phase"] == "checks":
            break
    else:
        raise AssertionError("The original Ticket never published its PR")
    prior_pr = scenario.port.pull_request(scenario.delivery.key)
    assert prior_pr is not None and prior_pr.state == "OPEN"
    kernel.control_host(request, "abandon", "original-abandon",
                        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    sent = []
    execute = scenario.port.execute

    def interrupt_retirement(effect, authorize):
        if effect.operation == "retire_pr":
            authorize()
            sent.append(effect)
            raise OSError("Original retirement interrupted before its physical write")
        return execute(effect, authorize)

    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    with monkeypatch.context() as patch:
        patch.setattr(scenario.port, "execute", interrupt_retirement)
        interrupted = operations.until(lambda step: bool(sent))
    assert interrupted.reason == "original_write_readback_required"
    original, = sent
    # The original launch outcome is uncertain; no close has actually happened.
    scenario.port.force_read["retire_pr"] = "unknown" if uncertainty == "prior_after_continuation" else uncertainty
    assert operations.once().kind == "paused"
    if uncertainty != "prior_after_continuation":
        assert kernel.operation_state().disposition == "quiescent"
    assert scenario.port.pr_by_number(prior_pr.number).state == "OPEN"
    assert not [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
    assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
        42, "start", exit_status=0, ended_at=kernel.clock(), usage_released=True)) == "exited"
    continuation = kernel.admit_host("continue-cleanup", kind="abandon", continuation_of="original-abandon",
        command=("native-host",), environment_carrier=tmp_path / "continued-carrier", preflight=lambda: None)
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "new", "/new-host", "boot",
                                                           43, "new-start", usage_ready=True)) == "running"
    scenario.execution = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    scenario.port.force_read.clear()
    events = []
    reconcile = scenario.port.reconcile

    def readback(effect):
        events.append(("read", effect))
        return reconcile(effect)

    def recorded_execute(effect, authorize):
        events.append(("execute", effect))
        return execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "reconcile", readback)
    monkeypatch.setattr(scenario.port, "execute", recorded_execute)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    assert operations.once().kind == "progress"
    assert events[0][0] == "read" and events[0][1].effect_id == original.effect_id
    assert operations.once().kind == "progress"
    assert events[-2][0] == "execute" and events[-2][1].operation == "inspect_resources"
    assert not [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
    for _ in range(64):
        step = scenario.runner().run_once()
        if step.call is not None:
            scripted_return(kernel, scenario.runner(), step.call, scenario.agent)
        if step.kind == "complete":
            break
    else:
        raise AssertionError("The original finite abandonment could not continue after an exact prior readback")
    retired = [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
    assert len(retired) == 1 and retired[0]["intent"] == original.effect_id
    assert scenario.port.pr_by_number(prior_pr.number).state == "CLOSED"
    assert kernel.source_status()["abandonment"]["status"] == "complete"


def managed_abandon_scenario(tmp_path: Path) -> tuple[GraphScenario, HostLaunch]:
    # Host observations below are explicit controller-consumer fixture evidence;
    # this helper never starts a process or claims a physical exit.
    scenario = GraphScenario(tmp_path / "abandon", independent_source())
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    operations.until(lambda step: kernel.status(scenario.delivery.key)["phase"] == "development")
    assert Path(scenario.delivery.checkout).exists()
    kernel.control("stop", "retire-offline")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    return scenario, request


def test_abandon_original_cap_ends_before_a_managed_continuation(tmp_path: Path) -> None:
    scenario = GraphScenario(tmp_path / "original-cap", independent_source())
    kernel, original = scenario.kernel, scenario.execution
    inspections = scenario.port.inspect_resources(kernel.generation_resources())
    assert kernel.control("abandon", "original-abandon", resource_inspections=inspections) == "pending"
    kernel.confirm_executor_exit(original)
    continuation = kernel.admit_host("continue-cleanup", kind="abandon", continuation_of="original-abandon",
        command=("native-host",), environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    with pytest.raises(KernelError, match="executor_fenced"):
        OperationRunner(kernel, original, scenario.make_ports).observe(scenario.delivery.key)
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "new", "/new-host", "boot",
                                                           43, "new-start", usage_ready=True)) == "running"
    current = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    operations = CoreOperations(kernel, current, scenario.make_ports)
    step = operations.once()
    assert step.call is None and step.kind == "progress"
    with kernel.storage.transaction(write=False) as tx:
        inspected = tx.one("SELECT status FROM effects WHERE operation='inspect_resources'")
        assert inspected["status"] == "observed"


@pytest.mark.parametrize("recovery", ["clean", "discard"])
def test_abandon_continuation_reinspects_after_a_late_dirty_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    inspections = scenario.port.inspect_resources(kernel.generation_resources())
    assert all(item.disposition in {"clean", "absent"} for item in inspections)
    assert kernel.control_host(request, "abandon", "original-abandon", resource_inspections=inspections) == "pending"
    note = Path(scenario.delivery.checkout) / "late-work.txt"
    note.write_text("Work completed after the initial clean observation\n")
    inspections_seen = []
    execute = scenario.port.execute

    def record_inspection(effect, authorize):
        if effect.operation == "inspect_resources":
            inspections_seen.append(effect)
        return execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", record_inspection)
    assert scenario.runner().run_once().kind == "progress"
    inspected, = inspections_seen
    assert scenario.runner().run_once().kind == "quiescent"
    with kernel.storage.transaction(write=False) as tx:
        receipt = tx.one("SELECT receipt_id,subject FROM effects WHERE operation='inspect_resources' ORDER BY rowid DESC LIMIT 1")
        observed = json.loads(tx.get(receipt["receipt_id"], receipt["subject"]))
        assert any(item["disposition"] == "dirty" for item in observed["raw"]["resources"])
    assert kernel.operation_state().disposition == "quiescent"
    assert not [write for write in scenario.port.writes() if write["operation"] in kernel.REMOTE]
    if recovery == "clean":
        note.unlink()
    else:
        assert note.exists()
    assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
        42, "start", exit_status=0, ended_at=kernel.clock(), usage_released=True)) == "exited"
    arguments = {} if recovery == "clean" else {
        "discard_worktree": True, "resource_inspections": scenario.port.inspect_resources(kernel.generation_resources())}
    continuation = kernel.admit_host("continue-cleanup", kind="abandon", continuation_of="original-abandon",
        command=("native-host",), environment_carrier=tmp_path / "continued-carrier", preflight=lambda: None, **arguments)
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "new", "/new-host", "boot",
                                                           43, "new-start", usage_ready=True)) == "running"
    scenario.execution = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    assert scenario.runner().run_once().kind == "progress"
    fresh = inspections_seen[-1]
    assert fresh.operation == "inspect_resources" and len(inspections_seen) == 2
    assert fresh.effect_id != inspected.effect_id
    for _ in range(32):
        step = scenario.runner().run_once()
        if step.call is not None:
            scripted_return(kernel, scenario.runner(), step.call, scenario.agent)
        if step.kind == "complete":
            break
    else:
        raise AssertionError("Explicit original-action continuation did not finish cleanup")
    assert kernel.source_status()["abandonment"]["status"] == "complete"
    assert not [write for write in scenario.port.writes() if write["operation"] in kernel.REMOTE]


def test_managed_abandon_refuses_dirty_before_action_then_recovers_own_ticket(tmp_path: Path) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    assert scenario.run().kind == "complete"
    completed = kernel.source_status()["completions"]
    assert len(completed) == 1 and completed[0]["subject"] == "ticket:3"
    target_before = scenario.port.remote_commit("refs/heads/run")
    scenario.select("ticket:4")
    for _ in range(100):
        scenario.step()
        if kernel.status("ticket:4")["phase"] == "checks":
            break
    else:
        raise AssertionError("The remaining Ticket never published its original PR")
    prior_pr = scenario.port.pull_request("ticket:4")
    assert prior_pr is not None and prior_pr.state == "OPEN"
    checkout = Path(scenario.delivery.checkout)
    note = checkout / "operator-note.txt"
    note.write_text("Uncommitted original work\n")
    resources = kernel.generation_resources()
    # Real Git reads provide this synthetic port's mechanical facts. Native
    # adapter tests independently verify canonical paths and common Git roots.
    inspections = tuple(ResourceInspection(resource, "absent") if not Path(resource.checkout).exists() else
                        ResourceInspection(resource,
                            "dirty" if scenario.port.git.run("status", "--porcelain", "--untracked-files=all", cwd=Path(resource.checkout)).stdout else "clean",
                            scenario.port.git.run("rev-parse", "HEAD", cwd=Path(resource.checkout)).stdout.strip())
                        for resource in resources)
    assert any(item.disposition == "dirty" for item in inspections)
    writes = scenario.port.writes()
    history = kernel.history("ticket:3")
    with pytest.raises(KernelError, match="dirty_worktree"):
        kernel.control_host(request, "abandon", "abandon-original", resource_inspections=inspections)
    assert kernel.history("ticket:3") == history
    assert scenario.port.writes() == writes and note.exists()
    assert kernel.control_host(request, "abandon", "abandon-original", resource_inspections=inspections,
                               discard_worktree=True) == "pending"
    assert kernel.control_host(request, "abandon", "abandon-original", resource_inspections=inspections,
                               discard_worktree=True) == "pending"
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.control_host(request, "resume", "never-resume-business")
    # The same exact Host has only the original Action's finite cleanup cap.
    scenario.port.lose_response.add("retire_pr")
    now = [1000.25]
    kernel.clock = lambda: now[0]
    for _ in range(100):
        step = scenario.runner().run_once()
        assert step.call is None
        if step.kind == "waiting" and step.retry_at is not None:
            now[0] = step.retry_at
        if step.kind == "complete":
            break
    else:
        raise AssertionError("The original abandonment never settled its finite obligations")
    assert scenario.port.pr_by_number(prior_pr.number).state == "CLOSED"
    assert scenario.port.remote_commit("refs/heads/run") == target_before
    assert kernel.source_status()["completions"] == completed
    assert scenario.port.issue_state(3) == "OPEN"
    assert not checkout.exists()
    assert len([write for write in scenario.port.writes() if write["operation"] == "retire_pr"]) == 1
    assert len([write for write in scenario.port.writes() if write["operation"] == "reopen_ticket"]) == 1
    writes = scenario.port.writes()
    recovery = [write for write in writes if write["operation"] == "recovery_comment"]
    assert len(recovery) == 1
    assert [write["operation"] for write in writes].index("reopen_ticket") < [write["operation"] for write in writes].index("recovery_comment")
    with kernel.storage.transaction(write=False) as tx:
        recovered = tx.one("SELECT * FROM effects WHERE id=?", (recovery[0]["intent"],))
        payload = json.loads(tx.get(recovered["payload_id"], recovered["subject"]))
        reopened = tx.one("SELECT * FROM effects WHERE id=?", (payload["reopen_effect"],))
        assert reopened["operation"] == "reopen_ticket" and reopened["status"] == "confirmed"
        assert payload["completion"] == completed[0]
        assert payload["reopen_receipt"] == json.loads(tx.get(reopened["receipt_id"], reopened["subject"]))
        assert "<!--" not in payload["comment_body"]
        assert all(value in payload["comment_body"] for value in (scenario.execution.run_id, "Ticket #3",
                   f"PR #{completed[0]['pr_number']}", completed[0]["merge_sha"], "abandoned before entering the default branch"))
    with pytest.raises(KernelError, match="run_abandoned"):
        kernel.control_host(request, "resume", "never-resume-business")
