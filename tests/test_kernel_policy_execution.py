"""Frozen policy controls real domain allocation and continuation authority."""

from support.repository_fixture import create_kernel

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from support.offline import Scenario
from support.core_operations import CoreOperations, native_agent_fixture, final_scenario


@pytest.mark.parametrize(("kind", "expected"), [(DeliveryKind.TICKET, 1), (DeliveryKind.PARENT_ONLY, 2), (DeliveryKind.FINAL_RUN, 2)])
def test_ci_allowance_uses_the_actual_ticket_or_parent_owner(tmp_path: Path, kind: DeliveryKind, expected: int) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("delivery", kind, 1, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(ticket_ci_developments=1, parent_ci_developments=2)
    if kind == DeliveryKind.FINAL_RUN:
        # Final is admitted only after a real Ticket Completion. A standalone
        # Final template is not a live subject whose budget can be queried.
        scenario = final_scenario(tmp_path / 'final', policy=policy)
        kernel, delivery = scenario.kernel, scenario.delivery
    else:
        kernel = create_kernel(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    assert kernel.status(delivery.key)["budgets"]["ci"]["development_limit"] == expected


def test_frozen_call_deadline_cannot_be_extended_or_used_after_expiry(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(development_deadline_seconds=7.0, review_deadline_seconds=11.0, publication_deadline_seconds=13.0)
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    call = operations.once().call
    assert call is not None and call.deadline_seconds == 7.0 and call.deadline_at == 107.25
    with pytest.raises(KernelError, match="invocation_binding"):
        kernel.dispatch_agent(replace(call, deadline_at=108.25))
    assert kernel.dispatch_agent(call)
    now[0] = 107.25
    with pytest.raises(KernelError, match="invocation_deadline_exceeded"):
        kernel.guard_agent(call)
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 0


def test_created_language_is_consumed_by_the_actual_agent_call(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, 1, "parent", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           language="en", repository_root=str(tmp_path)))
    call = CoreOperations(kernel, kernel.claim_executor("executor"), lambda binding: ports).once().call
    assert call is not None
    assert call.context.get("language") == "en"
    assert call.context.get("repository_root") == str(tmp_path)


@pytest.mark.parametrize(("kind", "review"), [(DeliveryKind.TICKET, False), (DeliveryKind.PARENT_ONLY, True)])
def test_ci_review_requirement_is_not_inferred_from_an_unlimited_review_budget(tmp_path: Path, kind: DeliveryKind, review: bool) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("delivery", kind, 1, "parent", base, "refs/heads/source", "refs/heads/target")
    kernel = create_kernel(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,)))
    ci = kernel.status(delivery.key)["budgets"]["ci"]
    assert ci["review_limit"] is None
    assert ci.get("review_required") is review


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
        assert scenario.port.pull_request_by_ref(scenario.kernel.delivery(scenario.delivery.key).source_ref, scenario.kernel.delivery(scenario.delivery.key).target_ref).number == initial["pr_number"]


def test_external_changes_during_open_integration_share_the_free_snapshot(tmp_path: Path) -> None:
    scenario = final_scenario(tmp_path / "scenario", policy=Policy(resync_limit=0))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    execute = scenario.port.execute
    appended = []

    def append_after_integration(effect, authorize=None):
        result = execute(effect, authorize)
        if effect.operation == "integrate" and not appended:
            appended.append(scenario.port.external_remote_append(scenario.delivery.source_ref,
                {"foreign-source.txt": "Source work\n"}, "open-source"))
            scenario.port.external_remote_append(scenario.delivery.target_ref,
                {"foreign-target.txt": "Target work\n"}, "open-target")
        return result

    scenario.port.execute = append_after_integration
    for _ in range(50):
        step = operations.once()
        assert step.call is None, step
        if appended:
            break
    else:
        raise AssertionError("Integration was not reached")
    source = appended[0]
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


@pytest.mark.parametrize("invalid_output", [b"invalid structured output", b"\x00" * (1024 * 1024)], ids=["small", "escaped-limit"])
def test_format_repair_keeps_deadline_and_retains_each_physical_output(tmp_path: Path, invalid_output: bytes) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    kernel.agent_source_ready(first)
    kernel.receive(first, invalid_output)
    kernel.agent_exited(first, process, 0)
    now[0] = 102.5
    repair_step = operations.once()
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
    assert operations.once().kind == "progress"
    assert kernel.status(delivery.key)["phase"] == "candidate"
    returned = [event for event in kernel.history(delivery.key) if event["kind"] == "raw_returned"]
    assert len(returned) == 2 and returned[0]["material_id"] != returned[1]["material_id"]
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1


def test_ordinary_recovery_keeps_attempt_thread_and_uses_one_new_deadline(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    kernel.agent_source_ready(first)
    now[0] = 102.5
    kernel.agent_exited(first, process, 1)
    step = operations.once()
    assert step.kind == "agent"
    second = step.call
    assert second is not None and second.invocation_id != first.invocation_id
    assert second.attempt_id == first.attempt_id and second.thread_id == first.thread_id
    assert second.mode == "resume" and second.deadline_seconds == 7.0 and second.deadline_at == 109.5
    assert kernel.dispatch_agent(second)
    next_process = {**process, "pid": 24, "pgid": 24, "start_token": "second-start"}
    kernel.agent_started(second, next_process)
    kernel.agent_source_ready(second)
    kernel.agent_exited(second, next_process, 1)
    assert operations.once().kind == "paused"
    now[0] = 105.75
    kernel.control("resume", "manual-thread-replacement", subject=delivery.key, new_thread=True)
    third = operations.once().call
    assert third is not None and third.attempt_id == first.attempt_id and third.thread_id != first.thread_id
    assert third.deadline_seconds == first.deadline_seconds == 7.0
    assert third.deadline_at == now[0] + 7.0 == 112.75
    assert kernel.dispatch_agent(third)
    kernel.bind_thread(third, "replacement-backend-thread")
    third_process = {**process, "pid": 25, "pgid": 25, "start_token": "third-start"}
    kernel.agent_started(third, third_process)
    kernel.agent_source_ready(third)
    kernel.agent_exited(third, third_process, 1)
    assert operations.once().kind == "paused"
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1


def test_manual_resume_after_format_deadline_only_continues_the_readonly_output_step(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "original-backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    kernel.agent_source_ready(first)
    kernel.receive(first, b"invalid structured output")
    kernel.agent_exited(first, process, 0)
    now[0] = 108.0
    assert operations.once().kind == "paused"
    assert kernel.status(delivery.key)["gates"] == ["execution_failed"]
    kernel.control("resume", "manual-format-continuation", subject=delivery.key)
    now[0] = 200.25
    resumed = operations.once()
    assert resumed.kind == "agent"
    repair = resumed.call
    assert repair is not None and repair.invocation_id != first.invocation_id
    assert repair.attempt_id == first.attempt_id and repair.thread_id == first.thread_id
    assert repair.output_ordinal == 1 and repair.mode == "output-repair"
    assert repair.context["readonly"] is True and repair.context["contract_error"]
    assert repair.reported_thread == "original-backend-thread"
    assert repair.deadline_seconds == 7.0 and repair.deadline_at == 207.25
    assert kernel.dispatch_agent(repair)
    next_process = {**process, "pid": 24, "pgid": 24, "start_token": "repair-start"}
    kernel.agent_started(repair, next_process)
    kernel.receive(repair, b'{"result_kind":"development","summary":"Recovered original result","human_blockers":null}')
    kernel.agent_exited(repair, next_process, 0)
    assert operations.once().kind == "progress"
    assert kernel.status(delivery.key)["phase"] == "candidate"
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1
    with kernel.storage.transaction(write=False) as tx:
        calls = tx.rows("SELECT recovery_kind,action_id FROM invocations ORDER BY rowid")
        assert [tuple(row) for row in calls] == [(None, None), ("explicit", "manual-format-continuation")]


def test_allocated_call_deadline_releases_only_its_undispatched_lease(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    call = operations.once().call
    assert call is not None
    now[0] = 107.25
    assert operations.once().kind == "paused"
    assert operations.runner.state().active == ()
    assert kernel.dispatch_agent(call) is False
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 0


def test_capacity_recovery_waits_thirty_seconds_and_checks_current_authority(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    kernel, ports = native_agent_fixture(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,),
                           Policy(development_deadline_seconds=7.0)))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, "backend-thread")
    process = {"pid": 23, "start_token": "first-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    now[0] = 101.5
    kernel.agent_exited(call, process, 1, failure="machine:server_overloaded", failure_kind="capacity")
    now[0] = 131.499
    assert operations.once().kind == "waiting"
    assert operations.runner.state().active == ()
    now[0] = 131.5
    recovered = operations.once().call
    assert recovered is not None and recovered.invocation_id != call.invocation_id
    assert recovered.thread_id == call.thread_id and recovered.deadline_at == 138.5
    assert kernel.dispatch_agent(recovered)
    second_process = {**process, "pid": 24, "pgid": 24, "start_token": "second-start"}
    kernel.agent_started(recovered, second_process)
    kernel.agent_source_ready(recovered)
    kernel.agent_exited(recovered, second_process, 1, failure="machine:server_overloaded", failure_kind="capacity")
    now[0] = 161.5
    ports.requirements = replace(ports.requirements, issues=tuple(
        replace(issue, body="Changed requirement") for issue in ports.requirements.issues))
    assert operations.once().kind == "paused"
    assert operations.runner.state().active == ()
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1
