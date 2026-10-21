"""Old native Issue inputs through the replacement domain's public seam."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, Requirements, RunSpec, World
from agent_run_kernel.offline import GraphScenario


def native_input() -> dict:
    return json.loads((Path(__file__).parent / "data/kernel/graph-refresh-v1.json").read_text())


def source_run(path: Path, requirements: Requirements | None = None) -> tuple[Kernel, Requirements]:
    source = requirements or Requirements.from_wire(native_input()["initial_requirements"])
    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "reader-untrusted-revision", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in source.parent.sub_issue_numbers)
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "reader-untrusted-revision", base, "refs/heads/run", "refs/heads/main")
    spec = RunSpec("native-run", "example/project", 1, "untrusted-parent", "untrusted-graph", (*deliveries, final), Policy(concurrency=2))
    return Kernel.create(path, spec, requirements=source), source


def test_closed_local_dependency_without_completion_never_unblocks_or_creates_final(tmp_path: Path) -> None:
    kernel, source = source_run(tmp_path / "run.sqlite3")
    initial = kernel.source_status()
    assert initial["frontier"] == ["ticket:3"]
    assert initial["remaining"] == {"ticket:4": "blocked_by_incomplete_tickets"}
    assert initial["final_subject"] is None
    closed = replace(source, issues=tuple(replace(issue, state="CLOSED") if issue.number == 3 else
                                         replace(issue, blocked_by=tuple(replace(blocker, state="CLOSED") for blocker in issue.blocked_by))
                                         for issue in source.issues))
    kernel.refresh(closed)
    state = Kernel(kernel.storage.path).source_status()
    assert state["frontier"] == []
    assert state["remaining"] == {"ticket:3": "ticket_closed", "ticket:4": "blocked_by_incomplete_tickets"}
    assert state["final_subject"] is None
    token = kernel.claim_executor("test-executor")
    status = kernel.status("ticket:4")
    world = World(status["parent_revision"], status["graph_revision"], status["revision"], Commit("base", "base-tree"))
    assert kernel.advance("ticket:4", token, world).reason == "blocked_by_incomplete_tickets"


def test_real_ticket_close_receipts_create_completion_before_dependency_and_final(tmp_path: Path) -> None:
    source = Requirements.from_wire(native_input()["initial_requirements"])
    scenario = GraphScenario(tmp_path / "graph", source)
    with pytest.raises(KernelError, match="ticket_completion_required"):
        scenario.activate_final()
    assert scenario.run().kind == "complete"
    completed = scenario.kernel.source_status()["completions"]
    assert [entry["subject"] for entry in completed] == ["ticket:3"]
    assert completed[0]["source_sha"] == scenario.port.pull_request("ticket:3").head.sha
    assert scenario.kernel.source_status()["frontier"] == ["ticket:4"]
    scenario.select("ticket:4")
    # Close has actually happened; a fresh source read sees CLOSED before its
    # original receipt is consumed. It cannot substitute for Completion.
    for _ in range(100):
        step = scenario.step()
        if step.effect is not None and step.effect.operation == "close":
            break
    else:
        raise AssertionError("second Ticket never reached its close intent")
    assert len(scenario.kernel.source_status()["completions"]) == 1
    scenario.refresh()
    with pytest.raises(KernelError, match="ticket_completion_required"):
        scenario.activate_final()
    assert scenario.run().kind == "complete"
    before = scenario.port.git.commit("refs/heads/run")
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    final = scenario.activate_final()
    scenario.select(final)
    assert scenario.kernel.delivery(final).base == before
    assert scenario.run().kind == "approval"
    final_calls = [call for call in scenario.role_calls if call["subject"] == final]
    assert [call["role"] for call in final_calls] == ["review", "publication"]
    assert len(scenario.kernel.source_status()["completions"]) == 2
    assert all(entry["tree"] for entry in scenario.kernel.source_status()["completions"])
    assert scenario.port.git.commit("refs/heads/main") != scenario.port.git.commit("refs/heads/run")
    scenario.kernel.control("approve", "approve-whole-parent", subject=final, world=scenario.world())
    assert scenario.run().kind == "complete"
