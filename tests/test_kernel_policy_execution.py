"""Frozen policy controls real domain allocation and continuation authority."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec, World
from agent_run_kernel.offline import Scenario


@pytest.mark.parametrize(("kind", "expected"), [(DeliveryKind.TICKET, 1), (DeliveryKind.PARENT_ONLY, 2), (DeliveryKind.FINAL_RUN, 2)])
def test_ci_allowance_uses_the_actual_ticket_or_parent_owner(tmp_path: Path, kind: DeliveryKind, expected: int) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("delivery", kind, 1, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(ticket_ci_developments=1, parent_ci_developments=2)
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    assert kernel.status(delivery.key)["budgets"]["ci"]["development_limit"] == expected


def test_frozen_call_deadline_cannot_be_extended_or_used_after_expiry(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(development_deadline_seconds=7.0, review_deadline_seconds=11.0, publication_deadline_seconds=13.0)
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    call = kernel.advance(delivery.key, executor, World("parent", "graph", "requirements", base)).call
    assert call is not None and call.deadline_seconds == 7.0 and call.deadline_at == 107.25
    with pytest.raises(KernelError, match="invocation_binding"):
        kernel.dispatch_agent(replace(call, deadline_at=108.25))
    assert kernel.dispatch_agent(call)
    now[0] = 107.25
    with pytest.raises(KernelError, match="invocation_deadline_exceeded"):
        kernel.guard_agent(call)
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize("limit", [0, 1])
def test_resync_charges_only_external_completed_snapshots_and_resume_only_renews_it(tmp_path: Path, limit: int) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY, policy=Policy(resync_limit=limit))
    assert scenario.run().kind == "approval"
    initial = scenario.kernel.status(scenario.delivery.key)
    for ordinal in range(1, limit + 2):
        scenario.port.external_remote_append(scenario.delivery.target_ref, {f"external-{ordinal}.txt": "Retained external work\n"}, f"resync-{ordinal}")
        boundary = scenario.run()
        if ordinal <= limit:
            assert boundary.kind == "approval"
            current = scenario.kernel.status(scenario.delivery.key)
            assert current["budgets"]["requirements"] == initial["budgets"]["requirements"]
            assert current["budgets"]["ci"] == initial["budgets"]["ci"]
        else:
            assert boundary.kind == "paused" and "integration_resync_exhausted" in scenario.kernel.status(scenario.delivery.key)["gates"]
    before = scenario.kernel.status(scenario.delivery.key)
    assert initial["resync"]["used"] == 0 and initial["resync"]["open"] is False
    assert before["resync"]["used"] == limit
    assert before["resync"]["window"] == limit + 1
    assert scenario.development_count == 1
    if limit:
        scenario.kernel.control("resume", "renew-resync-only", subject=scenario.delivery.key)
        renewed = scenario.kernel.status(scenario.delivery.key)
        assert renewed["resync"]["generation"] == 2 and renewed["resync"]["used"] == 0
        assert renewed["budgets"] == before["budgets"]
        assert scenario.run().kind == "approval"
        assert scenario.kernel.status(scenario.delivery.key)["resync"]["used"] == 1
        assert scenario.port.pull_request(scenario.delivery.key).number == initial["pr_number"]


def test_external_changes_during_open_integration_share_the_free_snapshot(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN, policy=Policy(resync_limit=0))
    for _ in range(50):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "integrate":
            break
        scenario.handle(step)
    else:
        raise AssertionError("Integration was not reached")
    effect = step.effect
    assert effect is not None
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    scenario.port.execute(effect, lambda: scenario.kernel.guard_effect(effect, scenario.execution, scenario.world()))
    source = scenario.port.external_remote_append(scenario.delivery.source_ref, {"foreign-source.txt": "Source work\n"}, "open-source")
    scenario.port.external_remote_append(scenario.delivery.target_ref, {"foreign-target.txt": "Target work\n"}, "open-target")
    scenario.kernel.reconcile(effect, scenario.port.reconcile(effect), scenario.execution, scenario.world())
    opened = scenario.kernel.status(scenario.delivery.key)["resync"]
    assert (opened["used"], opened["window"], opened["open"]) == (0, 1, True)
    assert scenario.run().kind == "approval"
    completed = scenario.kernel.status(scenario.delivery.key)["resync"]
    assert (completed["used"], completed["window"], completed["open"]) == (0, 1, False)
    published = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert published is not None and scenario.port.git.is_ancestor(source.sha, published.sha)
    assert scenario.port.git.run("show", published.sha + ":foreign-source.txt").stdout == "Source work\n"
    assert scenario.port.git.run("show", published.sha + ":foreign-target.txt").stdout == "Target work\n"
    assert scenario.development_count == 0


def test_format_repair_keeps_deadline_and_retains_each_physical_output(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    world = World("parent", "graph", "requirements", base)
    first = kernel.advance(delivery.key, executor, world).call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    kernel.receive(first, b"invalid structured output")
    kernel.agent_exited(first, process, 0)
    now[0] = 102.5
    repair_step = kernel.advance(delivery.key, executor, world)
    assert repair_step.kind == "agent"
    repair = repair_step.call
    assert repair is not None and repair.mode == "output-repair"
    assert repair.invocation_id == first.invocation_id and repair.thread_id == first.thread_id
    assert repair.lease_id != first.lease_id and repair.deadline_at == 107.25
    assert repair.context["readonly"] is True and repair.reported_thread == "backend-thread"
    assert kernel.dispatch_agent(repair)
    kernel.agent_exited(first, process, 0)  # A delayed duplicate exit cannot revoke the repair.
    kernel.guard_agent(repair)
    next_process = {**process, "pid": 24, "pgid": 24, "start_token": "repair-start"}
    kernel.agent_started(repair, next_process)
    kernel.receive(repair, b'{"result_kind":"development","summary":"Valid result","human_blockers":null}')
    kernel.agent_exited(repair, next_process, 0)
    assert kernel.advance(delivery.key, executor, world).kind == "progress"
    assert kernel.status(delivery.key)["phase"] == "candidate"
    returned = [event for event in kernel.history(delivery.key) if event["kind"] == "raw_returned"]
    assert len(returned) == 2 and returned[0]["material_id"] != returned[1]["material_id"]
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1


def test_ordinary_recovery_keeps_attempt_thread_and_uses_one_new_deadline(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    world = World("parent", "graph", "requirements", base)
    first = kernel.advance(delivery.key, executor, world).call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    now[0] = 102.5
    kernel.agent_exited(first, process, 1)
    step = kernel.advance(delivery.key, executor, world)
    assert step.kind == "agent"
    second = step.call
    assert second is not None and second.invocation_id != first.invocation_id
    assert second.attempt_id == first.attempt_id and second.thread_id == first.thread_id
    assert second.mode == "resume" and second.deadline_seconds == 7.0 and second.deadline_at == 109.5
    assert kernel.dispatch_agent(second)
    next_process = {**process, "pid": 24, "pgid": 24, "start_token": "second-start"}
    kernel.agent_started(second, next_process)
    kernel.agent_exited(second, next_process, 1)
    assert kernel.advance(delivery.key, executor, world).kind == "paused"
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1


def test_allocated_call_deadline_releases_only_its_undispatched_lease(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    world = World("parent", "graph", "requirements", base)
    call = kernel.advance(delivery.key, executor, world).call
    assert call is not None
    now[0] = 107.25
    assert kernel.advance(delivery.key, executor, world).kind == "paused"
    assert kernel.scheduling()["active"] == []
    assert kernel.dispatch_agent(call) is False
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1
