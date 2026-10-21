"""Explicit actions use an existing Host and the original semantic work queue."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec, World
from agent_run_kernel.model import Execution, HostObservation
from agent_run_kernel.offline import Scenario


def test_busy_host_applies_resume_message_once_and_preserves_the_ticket_queue(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    jobs = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "v1", base,
                          f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", jobs,
                           Policy(concurrency=1), language="en", repository_root=str(tmp_path / "repository")))
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    running = HostObservation("running", request.unit_name, "original", "/host", "boot", 42, "start", usage_ready=True)
    assert kernel.observe_host(request, running) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    world = World("parent", "graph", "v1", base)
    failed = kernel.advance("ticket:2", execution, world).call
    assert failed is not None and kernel.dispatch_agent(failed)
    kernel.agent_unavailable(failed, "preparation_failed", "No process was created")
    sibling = kernel.advance("ticket:3", execution, world).call
    assert sibling is not None

    message = "已修复启动配置；继续本轮，保留先前调查。"
    assert kernel.control_host(request, "resume", "resume-ticket-2", subject="ticket:2", message=message, new_thread=True) == "applied"
    assert kernel.control_host(request, "resume", "resume-ticket-2", subject="ticket:2", message=message, new_thread=True) == "applied"
    assert kernel.active_host_action() == "start" and kernel.host_request("start") == request
    assert not kernel.dispatch_host(request)
    assert kernel.advance("ticket:2", execution, world).reason == "agent_capacity"
    assert kernel.scheduling()["work_fifo"] == ["ticket:2"]
    assert kernel.scheduling()["active"][0]["invocation"] == sibling.invocation_id
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.control_host(request, "resume", "resume-ticket-2", subject="ticket:2", message="changed", new_thread=True)
    with pytest.raises(KernelError, match="host_binding_changed"):
        kernel.control_host(replace(request, nonce="other"), "resume", "wrong-host", subject="ticket:2")

    # This controlled producer never dispatched a physical sibling process.
    kernel.receive(sibling, b'{"result_kind":"development","summary":"Sibling result","human_blockers":null}')
    continued = kernel.advance("ticket:2", execution, world).call
    assert continued is not None and continued.attempt_id == failed.attempt_id
    assert continued.thread_id != failed.thread_id and continued.invocation_id != failed.invocation_id
    assert continued.execution == execution and continued.context["human_responses"] == [message]
    assert continued.context["semantic_continuation"] is True
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1
    applied = [event for event in kernel.history("ticket:2") if event["kind"] == "control_applied" and event["identity"] == "resume-ticket-2"]
    assert len(applied) == 1


def test_busy_host_applies_approval_to_the_original_delivery(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "delivery", DeliveryKind.PARENT_ONLY)
    scenario.kernel.control("stop", "retire-fixture")
    scenario.kernel.confirm_executor_exit(scenario.execution)
    scenario.kernel.control("resume", "prepare-managed")
    request = scenario.kernel.admit_host("start", kind="run", command=("native-host",),
                                         environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert scenario.kernel.dispatch_host(request)
    running = HostObservation("running", request.unit_name, "original", "/host", "boot", 42, "start", usage_ready=True)
    assert scenario.kernel.observe_host(request, running) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    assert scenario.run().kind == "approval"
    assert scenario.kernel.control_host(request, "approve", "approve-parent", subject=scenario.delivery.key, world=scenario.world()) == "pending"
    assert scenario.kernel.active_host_action() == "start" and scenario.kernel.host_request("start") == request
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 1
