"""Real Core operations retain the original bounded supervision contract."""

from support.repository_fixture import create_kernel
from support.core_operations import prepare_scripted_agent
from agent_run.core.operation_runner import OperationRunner
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.model import DeliveryKind
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import ReadWait
from support.offline import Scenario
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.queries.run import read
from support.core_operations import CoreOperations, scripted_return
from support.core_operations import delivery_run, source_operations, unavailable


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
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
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
    window = read(kernel.storage.path, 'status', storage=kernel.storage).data["supervision_windows"][0]
    assert window["started_at"] == 100.25 and window["deadline"] == 700.25
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    reopened.clock = lambda: 700.25
    runner = OperationRunner(reopened, execution, lambda binding: ports)
    expired = prepare_scripted_agent(kernel, runner.run_once())
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
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data["supervision_windows"] == []
    kernel.clock = lambda: 800
    monkeypatch.setattr(ports, "source", unavailable)
    assert operations.once().retry_at == 805
    fresh = read(kernel.storage.path, 'status', storage=kernel.storage).data["supervision_windows"][0]
    assert fresh["started_at"] == 800 and fresh["deadline"] == 1400


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


def test_checks_read_error_reuses_the_original_45_minute_window(ticket_before_pr, monkeypatch) -> None:
    scenario = ticket_before_pr
    scenario.checks[:] = ["pending"]
    scenario.kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    monkeypatch.setattr(operations.runner.ports, "source", unavailable)
    scenario.kernel.clock = lambda: 701
    assert operations.once().kind == "waiting"
    window = read(scenario.kernel.storage.path, 'status', storage=scenario.kernel.storage).data["supervision_windows"][0]
    assert window["started_at"] == 100 and window["deadline"] == 2800
    reopened = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    reopened.clock = lambda: 2800
    runner = OperationRunner(reopened, scenario.execution, lambda binding: operations.runner.ports)
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
    window = read(kernel.storage.path, 'status', storage=kernel.storage).data["supervision_windows"][0]
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
