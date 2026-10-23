"""Real Core operations retain the original bounded supervision contract."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec
from agent_run_kernel.model import Issue, ParentIssue, ReadWait, Requirements
from support.offline import GraphScenario, Scenario, fixture_inputs
from agent_run_kernel.operation_runner import DeliveryFacts, ReadUnavailable
from agent_run_kernel.query import read
from support.core_operations import CoreOperations, ReadOnlyPorts, scripted_return


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
        Policy(concurrency=2), language="en", repository_root=str(path.parent.resolve()), inputs=fixture_inputs()), requirements=source), source


def source_operations(kernel, source, execution):
    """Explicit read-only external facts; selection is shared production Core."""
    deliveries = [kernel.delivery(f"ticket:{issue.number}") for issue in source.issues]
    facts = {(delivery.source_ref, delivery.target_ref): DeliveryFacts(delivery.base, None) for delivery in deliveries}
    first = deliveries[0]
    ports = ReadOnlyPorts(source, first.source_ref, first.target_ref, facts[(first.source_ref, first.target_ref)])
    ports.delivery = lambda request: facts[(request.source_ref, request.target_ref)]
    return CoreOperations(kernel, execution, lambda binding: ports)


def unavailable(parent):
    raise ReadUnavailable("github_read_failed", "Original source outage")


def test_whole_operation_progress_requires_its_own_business_change(tmp_path: Path, monkeypatch) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operations = source_operations(kernel, source, execution)
    kernel.clock = lambda: 100.25
    ports = operations.runner.ports
    original = ports.source
    monkeypatch.setattr(ports, "source", unavailable)
    assert operations.once().retry_at == 105.25
    monkeypatch.setattr(ports, "source", original)
    sibling = operations.once().call
    assert sibling is not None and sibling.subject == "ticket:3"
    scripted_return(kernel, operations.runner, sibling,
        lambda call: b'{"result_kind":"development","summary":"real sibling result","human_blockers":null}')
    assert operations.once().kind == "progress"
    assert kernel.status("ticket:3")["phase"] == "candidate"
    # Real sibling work and another source observation cannot credit the stalled
    # ticket. No caller can manufacture a Progress result through the runner.
    ports.requirements = replace(source, issues=tuple(reversed(source.issues)))
    operations.runner.observe("ticket:3")
    kernel.clock = lambda: 700.25
    expired = operations.once()
    assert (expired.kind, expired.reason, expired.subject) == ("paused", "supervision_timeout", "ticket:2")
    reopened = Kernel(kernel.storage.path)
    assert reopened.status("ticket:2")["gates"] == ["supervision_timeout"]
    assert reopened.status("ticket:3")["gates"] == []


def test_idempotent_read_and_real_agent_result_are_not_no_progress(tmp_path: Path) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    operations = source_operations(kernel, source, kernel.claim_executor("owner"))
    operations.runner.observe("ticket:2")
    operations.runner.observe("ticket:2")
    assert kernel.status("ticket:2")["pending_attempt"] is None
    allocation = operations.once()
    assert allocation.call is not None and allocation.subject == "ticket:2"
    scripted_return(kernel, operations.runner, allocation.call,
        lambda call: b'{"result_kind":"development","summary":"changed","human_blockers":null}')
    assert operations.once(agent_capacity=0).kind == "progress"
    assert kernel.status("ticket:2")["phase"] == "candidate"
    assert kernel.status("ticket:2")["gates"] == []


def test_read_deadline_survives_successful_reads_diagnostics_siblings_and_restart(tmp_path: Path, monkeypatch) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operations = source_operations(kernel, source, execution)
    ports = operations.runner.ports
    original = ports.source
    kernel.clock = lambda: 100.25
    monkeypatch.setattr(ports, "source", unavailable)
    assert operations.once().retry_at == 105.25
    monkeypatch.setattr(ports, "source", original)
    assert operations.once().call.subject == "ticket:3"
    kernel.clock = lambda: 110.25
    ports.requirements = replace(source, issues=tuple(reversed(source.issues)))
    # Source succeeds, delivery fails: the surrounding operation still has not
    # completed. Its first fractional deadline must survive this later read.
    def failed_delivery(request):
        raise ReadUnavailable("remote_refs_unavailable", "Different later diagnostic")
    monkeypatch.setattr(ports, "delivery", failed_delivery)
    assert operations.once().kind == "waiting"
    window = read(kernel.storage.path, "status").data["supervision_windows"][0]
    assert window["started_at"] == 100.25 and window["deadline"] == 700.25
    reopened = Kernel(kernel.storage.path)
    reopened.clock = lambda: 700.25
    runner = reopened.operation_runner(execution, lambda binding: ports)
    expired = runner.run_once()
    assert (expired.kind, expired.reason) == ("paused", "supervision_timeout")
    assert reopened.status("ticket:2")["gates"] == ["supervision_timeout"]
    assert reopened.status("ticket:3")["gates"] == []


def test_completed_business_operation_retires_read_wait(tmp_path: Path, monkeypatch) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    operations = source_operations(kernel, source, kernel.claim_executor("owner"))
    ports = operations.runner.ports
    original = ports.source
    kernel.clock = lambda: 100
    monkeypatch.setattr(ports, "source", unavailable)
    assert operations.once().retry_at == 105
    kernel.clock = lambda: 105
    monkeypatch.setattr(ports, "source", original)
    allocation = operations.once()
    assert allocation.call is not None and allocation.subject == "ticket:2"
    scripted_return(kernel, operations.runner, allocation.call,
        lambda call: b'{"result_kind":"development","summary":"changed","human_blockers":null}')
    assert operations.once(agent_capacity=0).kind == "progress"
    assert read(kernel.storage.path, "status").data["supervision_windows"] == []
    kernel.clock = lambda: 800
    monkeypatch.setattr(ports, "source", unavailable)
    assert operations.once().retry_at == 805
    fresh = read(kernel.storage.path, "status").data["supervision_windows"][0]
    assert fresh["started_at"] == 800 and fresh["deadline"] == 1400


def test_before_final_timeout_is_run_scoped_and_requires_original_executor_exit(tmp_path: Path, monkeypatch) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)), (
        Issue(2, "Ticket", "Work", "OPEN", frozenset({"ready-for-agent"})),))
    scenario = GraphScenario(tmp_path / "graph", source)
    assert scenario.run().kind == "complete"
    kernel, execution = scenario.kernel, scenario.execution
    assert kernel.source_status()["final_subject"] is None
    operations = CoreOperations(kernel, execution, scenario.make_ports)
    monkeypatch.setattr(operations.runner.ports, "source", unavailable)
    kernel.clock = lambda: 100
    assert operations.once().retry_at == 105
    assert kernel.source_status()["final_subject"] is None
    kernel.clock = lambda: 700
    assert operations.once().reason == "supervision_timeout"
    assert kernel.source_status()["blocked"] == "supervision_timeout"
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.control("resume", "too-early")
    kernel.control("stop", "retire-synthetic-executor")
    kernel.confirm_executor_exit(execution)
    kernel.control("resume", "explicit-resume")
    assert kernel.source_status()["blocked"] is None
    replacement = kernel.claim_executor("replacement")
    fresh = kernel.operation_runner(replacement, lambda binding: operations.runner.ports)
    assert fresh.run_once().retry_at == 705


def test_waits_do_not_authorize_a_stopped_or_replaced_scope(tmp_path: Path, monkeypatch) -> None:
    kernel, source = delivery_run(tmp_path / "kernel.sqlite3")
    execution = kernel.claim_executor("owner")
    operations = source_operations(kernel, source, execution)
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.clock = lambda: 100.25
    monkeypatch.setattr(operations.runner.ports, "source", unavailable)
    with pytest.raises(ReadWait) as waiting:
        operations.runner.authorize_agent(call)
    assert waiting.value.retry_at == 105.25
    kernel.control("stop", "stop-only-first", subject="ticket:2")
    with pytest.raises(KernelError):
        operations.runner.authorize_agent(call)
    assert kernel.status("ticket:2")["gates"] == ["operator_stopped"]
    assert operations.runner.state().active == (call.lease_id,)
    assert not operations.runner.worker_settled(call)


def test_checks_read_error_reuses_the_original_45_minute_window(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("pending",))
    scenario.kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    monkeypatch.setattr(operations.runner.ports, "source", unavailable)
    scenario.kernel.clock = lambda: 701
    assert operations.once().kind == "waiting"
    window = read(scenario.kernel.storage.path, "status").data["supervision_windows"][0]
    assert window["started_at"] == 100 and window["deadline"] == 2800
    reopened = Kernel(scenario.kernel.storage.path)
    reopened.clock = lambda: 2800
    runner = reopened.operation_runner(scenario.execution, lambda binding: operations.runner.ports)
    assert runner.run_once().reason == "supervision_timeout"


def test_timed_out_sent_write_quiesces_until_resume_then_only_reads_back(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    scenario.port.lose_response.add("push")
    scenario.kernel.clock = lambda: 100
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    for _ in range(80):
        step = operations.once()
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
        pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
        if pending is not None and pending["operation"] == "push" and pending["status"] == "sent":
            break
    else:
        raise AssertionError("Original push was never sent")
    kernel, subject = scenario.kernel, scenario.delivery.key
    pending = kernel.status(subject)["pending_write"]
    assert pending["operation"] == "push" and pending["status"] == "sent"
    reconcile = operations.runner.ports.reconcile
    def failed_readback(effect):
        raise ReadUnavailable("github_read_failed", "original readback unavailable")
    monkeypatch.setattr(operations.runner.ports, "reconcile", failed_readback)
    kernel.clock = lambda: 105
    assert operations.once().retry_at == 115
    window = read(kernel.storage.path, "status").data["supervision_windows"][0]
    assert window["started_at"] == 100 and window["deadline"] == 700
    kernel.clock = lambda: 700
    assert operations.once().reason == "supervision_timeout"
    assert operations.runner.state().disposition == "quiescent"
    history = kernel.history(subject)
    assert operations.once().kind == "quiescent"
    assert kernel.history(subject) == history
    kernel.control("resume", "explicit-readback-resume", subject=subject)
    monkeypatch.setattr(operations.runner.ports, "reconcile", reconcile)
    assert operations.once().kind == "progress"
    assert kernel.status(subject)["pending_write"]["id"] == pending["id"]
    assert sum(write["operation"] == "push" for write in scenario.port.writes()) == 1
