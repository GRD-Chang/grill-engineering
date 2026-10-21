"""Managed abandonment retains exact recovery obligations and authority."""

import json
from pathlib import Path

import pytest

from agent_run_kernel import KernelError
from agent_run_kernel.model import DeliveryKind, Execution, HostLaunch, HostObservation, ResourceInspection
from agent_run_kernel.offline import GraphScenario, Scenario
from test_kernel_requirements import final_merge_in_flight, independent_source


@pytest.mark.parametrize("uncertain", ["unknown", "third"])
def test_abandon_cannot_take_over_an_unsettled_final_merge(tmp_path: Path, uncertain: str) -> None:
    scenario, merge = final_merge_in_flight(tmp_path / "merged-unknown")
    scenario.port.force_read["merge"] = uncertain
    assert scenario.kernel.reconcile(merge, scenario.port.reconcile(merge), scenario.execution, scenario.world()).kind == "paused"
    resources = scenario.kernel.generation_resources()
    inspections = scenario.port.inspect_resources(resources)
    with pytest.raises(KernelError, match="final_merge_unsettled"):
        scenario.kernel.control("abandon", "cannot-abandon-default-merge", resource_inspections=inspections)
    assert scenario.kernel.source_status()["abandonment"] is None
    scenario.port.force_read.clear()
    assert scenario.step().kind == "reconcile"
    scenario.kernel.control("resume", "resume-original-closeout", subject=merge.subject)
    assert scenario.run().kind == "complete"
    assert scenario.port.issue_state(3) == "CLOSED"
    assert not [write for write in scenario.port.writes() if write["operation"] == "reopen_ticket"]


@pytest.mark.parametrize("acknowledgement", ["confirmed", "unknown", "third"])
def test_parent_only_default_merge_cannot_be_abandoned(tmp_path: Path, acknowledgement: str) -> None:
    scenario = Scenario(tmp_path / "parent-only", DeliveryKind.PARENT_ONLY)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert scenario.run().kind == "approval"
    before = scenario.port.remote_commit(scenario.delivery.target_ref)
    kernel.control("approve", "approve-default", subject=subject, world=scenario.world())
    for _ in range(20):
        step = kernel.advance(subject, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "merge":
            merge = step.effect
            assert kernel.dispatch(merge, scenario.execution, scenario.world())
            scenario.port.execute(merge, lambda: kernel.guard_effect(merge, scenario.execution, scenario.world()))
            break
        scenario.handle(step)
    else:
        raise AssertionError("Parent-only approval never sent its original default merge")
    assert scenario.port.remote_commit(scenario.delivery.target_ref) != before
    if acknowledgement == "confirmed":
        assert kernel.reconcile(merge, scenario.port.reconcile(merge), scenario.execution, scenario.world()).kind == "progress"
        assert kernel.advance(subject, scenario.execution, scenario.world()).kind == "progress"
        assert scenario.run().kind == "complete"
        expected = "merged_run_noncancellable"
    else:
        scenario.port.force_read["merge"] = acknowledgement
        assert kernel.reconcile(merge, scenario.port.reconcile(merge), scenario.execution, scenario.world()).kind == "paused"
        expected = "final_merge_unsettled"
    with pytest.raises(KernelError, match=expected):
        kernel.control("abandon", "reject-default-abandon", resource_inspections=scenario.port.inspect_resources(kernel.generation_resources()))
    assert kernel.source_status()["abandonment"] is None
    scenario.port.force_read.clear()
    if acknowledgement != "confirmed":
        assert scenario.step().kind == "reconcile"
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
    inspected = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert inspected.effect is not None and inspected.effect.operation == "inspect_resources"
    scenario.handle(inspected)
    assert scenario.step().reason == "dirty_worktree"
    assert kernel.scheduling()["disposition"] == "quiescent"
    assert not [write for write in scenario.port.writes() if write["operation"] in kernel.REMOTE]


@pytest.mark.parametrize("uncertainty", ["unknown", "third", "prior_after_continuation"])
def test_abandon_continuation_settles_the_original_sent_intent_before_fresh_inspection(tmp_path: Path, uncertainty: str) -> None:
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
    for _ in range(20):
        step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        scenario.select(step.subject)
        if step.effect is not None and step.effect.operation == "retire_pr":
            original = kernel.attach_retirement(step.effect, scenario.port.prepare_retirement(step.effect), scenario.execution, scenario.world())
            assert kernel.dispatch(original, scenario.execution, scenario.world(),
                                   resource_inspections=scenario.port.inspect_resources(kernel.effect_resources(original)))
            break
        scenario.handle(step)
    else:
        raise AssertionError("Abandon never prepared its original PR retirement")
    # The original launch outcome is uncertain; no close has actually happened.
    scenario.port.force_read["retire_pr"] = "unknown" if uncertainty == "prior_after_continuation" else uncertainty
    assert kernel.reconcile(original, scenario.port.reconcile(original), scenario.execution, scenario.world()).kind == "paused"
    if uncertainty != "prior_after_continuation":
        assert kernel.scheduling()["disposition"] == "quiescent"
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
    readback = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert readback.kind == "reconcile" and readback.effect.effect_id == original.effect_id
    scenario.handle(readback)
    fresh = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert fresh.effect is not None and fresh.effect.operation == "inspect_resources"
    assert not [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
    scenario.handle(fresh)
    for _ in range(64):
        step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        scenario.select(step.subject)
        scenario.handle(step)
        if step.kind == "complete":
            break
    else:
        raise AssertionError("The original finite abandonment could not continue after an exact prior readback")
    retired = [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
    assert len(retired) == 1 and retired[0]["intent"] == original.effect_id
    assert scenario.port.pr_by_number(prior_pr.number).state == "CLOSED"
    assert kernel.source_status()["abandonment"]["status"] == "complete"


def managed_abandon_scenario(tmp_path: Path) -> tuple[GraphScenario, HostLaunch]:
    scenario = GraphScenario(tmp_path / "abandon", independent_source())
    kernel = scenario.kernel
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
        kernel.advance(scenario.delivery.key, original, scenario.world())
    assert kernel.dispatch_host(continuation)
    assert kernel.observe_host(continuation, HostObservation("running", continuation.unit_name, "new", "/new-host", "boot",
                                                           43, "new-start", usage_ready=True)) == "running"
    current = Execution(continuation.run_id, continuation.owner, continuation.epoch)
    step = kernel.advance(scenario.delivery.key, current, scenario.world())
    assert step.call is None and step.kind == "effect" and step.effect.operation == "inspect_resources"


@pytest.mark.parametrize("recovery", ["clean", "discard"])
def test_abandon_continuation_reinspects_after_a_late_dirty_observation(tmp_path: Path, recovery: str) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    kernel = scenario.kernel
    inspections = scenario.port.inspect_resources(kernel.generation_resources())
    assert all(item.disposition in {"clean", "absent"} for item in inspections)
    assert kernel.control_host(request, "abandon", "original-abandon", resource_inspections=inspections) == "pending"
    note = Path(scenario.delivery.checkout) / "late-work.txt"
    note.write_text("Work completed after the initial clean observation\n")
    inspected = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert inspected.effect is not None and inspected.effect.operation == "inspect_resources"
    scenario.handle(inspected)
    assert scenario.step().reason == "dirty_worktree"
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
    fresh = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert fresh.effect is not None and fresh.effect.operation == "inspect_resources"
    assert fresh.effect.effect_id != inspected.effect.effect_id
    scenario.handle(fresh)
    for _ in range(32):
        step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        scenario.select(step.subject)
        scenario.handle(step)
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
    for _ in range(100):
        step = kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        assert step.call is None
        scenario.select(step.subject)
        scenario.handle(step)
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
