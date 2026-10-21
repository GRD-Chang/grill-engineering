"""Whole automatic operations retain the original bounded supervision contract."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec, World
from agent_run_kernel.model import Issue, ParentIssue, Requirements, Step
from agent_run_kernel.offline import Scenario


def delivery_run(path: Path) -> tuple[Kernel, Requirements]:
    source = Requirements(ParentIssue(1, "Parent", "Requirement", (2, 3)), (
        Issue(2, "First", "First work", "OPEN", frozenset({"ready-for-agent"})),
        Issue(3, "Second", "Second work", "OPEN", frozenset({"ready-for-agent"})),
    ))
    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "unread", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "unread", base, "refs/heads/run", "refs/heads/main")
    return Kernel.create(path, RunSpec("run", "example/repository", 1, "unread", "unread", (*deliveries, final),
                                      Policy(concurrency=2)), requirements=source), source


def current_world(kernel: Kernel, subject: str) -> World:
    source = kernel.source_status()
    return World(source["parent_revision"], source["graph_revision"], kernel.delivery(subject).revision,
                 Commit("base", "base-tree"))


def test_whole_operation_progress_requires_its_own_business_change(tmp_path: Path) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    # Another Worker and a fresh source receipt cannot credit this operation.
    assert kernel.advance("ticket:3", execution, current_world(kernel, "ticket:3")).kind == "agent"
    kernel.refresh(lambda: replace(source, issues=tuple(reversed(source.issues))))
    result = kernel.finish_automatic_step(operation, execution, Step("progress", "ticket:2"))
    assert (result.kind, result.reason) == ("paused", "controller_no_progress")
    reopened = Kernel(kernel.storage.path)
    assert reopened.status("ticket:2")["gates"] == ["execution_failed"]
    assert reopened.status("ticket:3")["gates"] == []


def test_idempotent_read_and_real_agent_result_are_not_no_progress(tmp_path: Path) -> None:
    kernel, _ = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    world = current_world(kernel, "ticket:2")
    assert kernel.advance("ticket:2", execution).kind == "observe"
    assert kernel.advance("ticket:2", execution).kind == "observe"
    allocation = kernel.advance("ticket:2", execution, world)
    assert allocation.call is not None
    assert kernel.finish_automatic_step(operation, execution, allocation).kind == "agent"
    kernel.receive(allocation.call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    consumed = kernel.advance("ticket:2", execution, world, allow_agent=False)
    assert kernel.finish_automatic_step(operation, execution, consumed).kind == "progress"
    assert kernel.status("ticket:2")["phase"] == "candidate"
    assert kernel.status("ticket:2")["gates"] == []


def test_read_deadline_survives_successful_reads_diagnostics_siblings_and_restart(tmp_path: Path) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100.25
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    first = kernel.wait_for_read(operation, execution, "github_read_failed", "first outage")
    assert first.retry_at == 105.25
    assert kernel.guard_automatic_step(operation, execution).retry_at == 105.25
    kernel.clock = lambda: 110.25
    # A successful read does not complete the surrounding business operation.
    kernel.refresh(lambda: replace(source, issues=tuple(reversed(source.issues))))
    assert kernel.guard_automatic_step(operation, execution, after_read=True) is None
    assert kernel.advance("ticket:3", execution, current_world(kernel, "ticket:3")).kind == "agent"
    reopened = Kernel(kernel.storage.path)
    reopened.clock = lambda: 700.25
    resumed = reopened.begin_automatic_step(execution, "ticket:2")
    expired = reopened.wait_for_read(resumed, execution, "another_read_error", "different diagnostic")
    assert (expired.kind, expired.reason) == ("paused", "supervision_timeout")
    assert reopened.status("ticket:2")["gates"] == ["supervision_timeout"]
    assert reopened.status("ticket:3")["gates"] == []


def test_completed_business_operation_retires_read_wait(tmp_path: Path) -> None:
    kernel, _ = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    world = current_world(kernel, "ticket:2")
    allocation = kernel.advance("ticket:2", execution, world)
    assert allocation.call is not None
    kernel.receive(allocation.call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')
    kernel.clock = lambda: 100
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    kernel.wait_for_read(operation, execution, "github_read_failed", "outage")
    consumed = kernel.advance("ticket:2", execution, world, allow_agent=False)
    assert kernel.finish_automatic_step(operation, execution, consumed).kind == "progress"
    kernel.clock = lambda: 800
    fresh = kernel.begin_automatic_step(execution, "ticket:2")
    assert kernel.wait_for_read(fresh, execution, "github_read_failed", "new operation").retry_at == 805


def test_before_final_timeout_is_run_scoped_and_requires_original_executor_exit(tmp_path: Path) -> None:
    kernel, _ = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100
    operation = kernel.begin_automatic_step(execution)
    assert kernel.wait_for_read(operation, execution, "final_source_missing", "Run ref unavailable").retry_at == 105
    assert kernel.source_status()["final_subject"] is None
    kernel.clock = lambda: 700
    assert kernel.guard_automatic_step(operation, execution).reason == "supervision_timeout"
    assert kernel.source_status()["blocked"] == "supervision_timeout"
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.control("resume", "too-early")
    # This isolated driver has no managed Host. Its existing synthetic exit
    # interface consumes a retired Executor; production uses observe_host.
    kernel.control("stop", "retire-synthetic-executor")
    kernel.confirm_executor_exit(execution)
    kernel.control("resume", "explicit-resume")
    assert kernel.source_status()["blocked"] is None
    replacement = kernel.claim_executor("replacement")
    fresh = kernel.begin_automatic_step(replacement)
    assert kernel.wait_for_read(fresh, replacement, "github_read_failed", "same ref").retry_at == 705


def test_waits_do_not_authorize_a_stopped_or_replaced_scope(tmp_path: Path) -> None:
    kernel, _ = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operation = kernel.begin_automatic_step(execution, "ticket:2")
    allocation = kernel.advance("ticket:2", execution, current_world(kernel, "ticket:2"))
    assert allocation.kind == "agent"
    assert kernel.wait_for_read(operation, execution, "github_read_failed", "late error").reason == "observation_superseded"
    fresh = kernel.begin_automatic_step(execution, "ticket:2")
    kernel.control("stop", "stop-only-first", subject="ticket:2")
    with pytest.raises(KernelError, match="automatic_step_paused"):
        kernel.guard_automatic_step(fresh, execution, after_read=True)
    assert kernel.wait_for_read(fresh, execution, "github_read_failed", "late error").kind == "paused"
    assert kernel.status("ticket:2")["gates"] == ["operator_stopped"]


def test_checks_read_error_reuses_the_original_45_minute_window(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("pending",))
    scenario.kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    operation = scenario.kernel.begin_automatic_step(scenario.execution, scenario.delivery.key)
    scenario.kernel.clock = lambda: 701
    assert scenario.kernel.wait_for_read(operation, scenario.execution, "github_read_failed", "checks unavailable").kind == "waiting"
    reopened = Kernel(scenario.kernel.storage.path)
    reopened.clock = lambda: 2800
    operation = reopened.begin_automatic_step(scenario.execution, scenario.delivery.key)
    assert reopened.wait_for_read(operation, scenario.execution, "github_read_failed", "still unavailable").reason == "supervision_timeout"


def test_timed_out_sent_write_quiesces_until_resume_then_only_reads_back(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    kernel, subject = scenario.kernel, scenario.delivery.key
    for _ in range(80):
        step = kernel.advance(subject, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "push":
            effect = step.effect
            break
        scenario.handle(step)
    else:
        raise AssertionError("Original push was never prepared")
    # The original physical write succeeded; its readback is unavailable.
    scenario.port.execute(effect, lambda: kernel.dispatch(effect, scenario.execution, scenario.world()))
    assert kernel.status(subject)["pending_write"]["status"] == "sent"
    kernel.clock = lambda: 100
    token = kernel.begin_automatic_step(scenario.execution, subject)
    kernel.wait_for_read(token, scenario.execution, "github_read_failed", "original readback unavailable")
    kernel.clock = lambda: 700
    assert kernel.guard_automatic_step(token, scenario.execution, readback=effect).reason == "supervision_timeout"
    assert kernel.scheduling()["disposition"] == "quiescent"
    history = kernel.history(subject)
    assert kernel.guard_automatic_step(token, scenario.execution, readback=effect).reason == "supervision_timeout"
    assert kernel.history(subject) == history
    assert kernel.status(subject)["pending_write"]["status"] == "sent"
    kernel.control("resume", "resume-original-write", subject=subject)
    fresh = kernel.begin_automatic_step(scenario.execution, subject)
    assert kernel.guard_automatic_step(fresh, scenario.execution, readback=effect) is None
    result = scenario.port.reconcile(effect)
    assert kernel.reconcile(effect, result, scenario.execution).kind == "progress"
    assert sum(write["operation"] == "push" for write in scenario.port.writes()) == 1
