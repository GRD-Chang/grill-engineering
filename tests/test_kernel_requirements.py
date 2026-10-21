"""Old native Issue inputs through the replacement domain's public seam."""

from dataclasses import replace
import json
from pathlib import Path

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, Requirements, RunSpec, World


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
