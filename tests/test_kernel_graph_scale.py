"""Legal source graphs exceed one physical SQLite read page."""

from support.repository_fixture import create_kernel
from pathlib import Path

import pytest

from agent_run.model import Blocker, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Policy, Requirements, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.core.operation_contract import DeliveryFacts
from agent_run.queries.run import read
from support.offline import fixture_inputs
from support.core_operations import CoreOperations, ReadOnlyPorts


def test_130_ticket_source_graph_dependencies_fifo_and_stop_cross_read_pages(tmp_path: Path, monkeypatch) -> None:
    base = Commit("explicit-base", "explicit-tree")
    numbers = tuple(range(2, 132))
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "Implement this requirement.", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in numbers)
    source = Requirements(ParentIssue(1, "Parent", "Explicit original requirement", numbers), tuple(
        Issue(number, f"Ticket {number}", "Implement this requirement.", "OPEN", frozenset({"ready-for-agent"}),
              tuple(Blocker(predecessor, "OPEN") for predecessor in numbers[:-1]) if number == numbers[-1] else ())
        for number in numbers))
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "unaccepted", base, "refs/heads/run", "refs/heads/main")
    spec = RunSpec("wide-run", "example/project", 1, "unaccepted", "unaccepted", (*deliveries, final),
                   Policy(concurrency=1), language="en", repository_root=str(tmp_path.resolve()), inputs=fixture_inputs())
    kernel = create_kernel(tmp_path / "run.sqlite3", spec, requirements=source)
    ports = ReadOnlyPorts(source, deliveries[0].source_ref, deliveries[0].target_ref, DeliveryFacts(base, None))
    facts = {(delivery.source_ref, delivery.target_ref): DeliveryFacts(base, None) for delivery in deliveries}
    monkeypatch.setattr(ports, "delivery", lambda request: facts[(request.source_ref, request.target_ref)])
    execution = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    operations.runner.observe("ticket:2")
    status = kernel.source_status()
    assert len(status["frontier"]) == 129
    assert len(kernel.delivery(f"ticket:{numbers[-1]}").dependencies) == 129
    allocated = operations.once().call
    assert allocated is not None and allocated.subject == "ticket:2"
    for _ in range(128):
        assert operations.once(agent_capacity=0).call is None
    assert len(read(kernel.storage.path, 'status', storage=kernel.storage).data["scheduling"]["work_fifo"]) == 128
    assert kernel.status(f"ticket:{numbers[-1]}")["pending_attempt"] is None
    kernel.control("stop", "stop-whole-wide-run")
    assert all(kernel.status(f"ticket:{number}")["stopped"] for number in numbers)
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.guard_agent(allocated)
