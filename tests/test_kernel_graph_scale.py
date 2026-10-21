"""Legal source graphs exceed one physical SQLite read page."""

from pathlib import Path

import pytest

from agent_run_kernel import Blocker, Delivery, DeliveryKind, Issue, Kernel, KernelError, ParentIssue, Policy, Requirements, RunSpec
from agent_run_kernel.offline import OfflinePort


def test_130_ticket_source_graph_dependencies_fifo_and_stop_cross_read_pages(tmp_path: Path) -> None:
    port = OfflinePort.create(tmp_path / "repository")
    base = port.git.commit("refs/heads/main")
    port.git.run("update-ref", "refs/heads/run", base.sha)
    port._remote_cas("refs/heads/run", base.sha, base.sha)
    numbers = tuple(range(2, 132))
    requirements = Requirements(ParentIssue(1, "Wide graph", "All declared Tickets belong to this scope.", numbers), tuple(
        Issue(number, f"Ticket {number}", "Implement this requirement.", "OPEN", frozenset({"ready-for-agent"}),
              tuple(Blocker(predecessor, "OPEN") for predecessor in numbers[:-1]) if number == numbers[-1] else ())
        for number in numbers))
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "unaccepted", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in numbers)
    final = Delivery("final", DeliveryKind.FINAL_RUN, 1, "unaccepted", base, "refs/heads/run", "refs/heads/main")
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("wide-run", "example/project", 1, "unaccepted", "unaccepted",
                           (*deliveries, final), Policy(concurrency=1), language="en", repository_root=str(port.git.root)),
                           requirements=requirements)
    execution = kernel.claim_executor("executor")
    status = kernel.source_status()
    assert len(status["frontier"]) == 129
    assert len(kernel.delivery(f"ticket:{numbers[-1]}").dependencies) == 129
    accepted = RunSpec("wide-run", "example/project", 1, status["parent_revision"], status["graph_revision"], (*deliveries, final))
    allocated = []
    for number in numbers[:-1]:
        subject = f"ticket:{number}"
        delivery = kernel.delivery(subject)
        step = kernel.advance(subject, execution, port.world(accepted, delivery))
        if step.call is not None:
            allocated.append(step.call)
    assert len(allocated) == 1
    assert len(kernel.scheduling()["work_fifo"]) == 128
    kernel.control("stop", "stop-whole-wide-run")
    assert all(kernel.status(f"ticket:{number}")["stopped"] for number in numbers)
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.guard_agent(allocated[0])
