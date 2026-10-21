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
    original_completions = scenario.kernel.source_status()["completions"]
    scenario.requirements = replace(source, parent=replace(source.parent, body="Clarified overall Parent outcome."),
                                    issues=tuple(replace(issue, title="Post-completion title edit", body="Edited after delivery")
                                                 if issue.number == 3 else issue for issue in source.issues))
    scenario.refresh()
    assert scenario.kernel.source_status()["completions"] == original_completions
    assert scenario.kernel.source_status()["blocked"] is None
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    final = scenario.activate_final()
    scenario.select(final)
    assert scenario.kernel.delivery(final).base == before
    assert scenario.run().kind == "approval"
    final_calls = [call for call in scenario.role_calls if call["subject"] == final]
    assert [call["role"] for call in final_calls] == ["review", "publication"]
    context = final_calls[0]["context"]
    assert context["requirements"][0]["body"] == "Clarified overall Parent outcome."
    assert context["requirements"][1]["title"] == "Deliver one ticket"
    assert context["requirements"][1]["body"] == source.issues[0].body
    assert all(entry["review"]["artifact"]["checks"]["spec"]["status"] == "pass" for entry in context["ticket_completions"])
    assert all(entry["development"]["artifact"]["summary"] for entry in context["ticket_completions"])
    assert len(scenario.kernel.source_status()["completions"]) == 2
    assert all(entry["tree"] for entry in scenario.kernel.source_status()["completions"])
    assert scenario.port.git.commit("refs/heads/main") != scenario.port.git.commit("refs/heads/run")
    scenario.kernel.control("approve", "approve-whole-parent", subject=final, world=scenario.world())
    assert scenario.run().kind == "complete"


def independent_source() -> Requirements:
    source = Requirements.from_wire(native_input()["initial_requirements"])
    return replace(source, issues=tuple(replace(issue, blocked_by=()) for issue in source.issues))


def demand_world(kernel: Kernel, subject: str) -> World:
    state = kernel.status(subject)
    return World(state["source_parent_revision"], kernel.source_status()["observed_graph_revision"], state["source_revision"], Commit("base", "base-tree"))


def test_requirement_requeue_is_explicit_idempotent_and_retains_sibling_return(tmp_path: Path) -> None:
    kernel, source = source_run(tmp_path / "run.sqlite3", independent_source())
    token = kernel.claim_executor("executor")
    first = kernel.advance("ticket:3", token, demand_world(kernel, "ticket:3")).call
    second = kernel.advance("ticket:4", token, demand_world(kernel, "ticket:4")).call
    assert first is not None and second is not None
    raw = b'{"result_kind":"development","summary":"retained exact return","human_blockers":null}'
    kernel.receive(second, raw)
    kernel.receive(first, raw)
    before = kernel.status("ticket:3")
    changed = replace(source, issues=tuple(replace(issue, body=issue.body + "\nNew requirement") if issue.number == 4 else issue for issue in source.issues))
    kernel.refresh(changed)
    assert kernel.source_status()["blocked"] == "requeue_required"
    assert kernel.advance("ticket:3", token, demand_world(kernel, "ticket:3")).kind == "paused"
    old = kernel.delivery("ticket:4")
    replacement = replace(old, source_ref="refs/heads/ticket-4-g2", checkout=str(tmp_path / "work-g2"))
    world = demand_world(kernel, "ticket:4")
    assert kernel.requeue("ticket:4", "exact-requeue", replacement, world) == "applied"
    assert kernel.requeue("ticket:4", "exact-requeue", replacement, world) == "applied"
    assert kernel.status("ticket:4")["generation"] == 2
    assert kernel.status("ticket:4")["retired_generations"][0]["source_ref"] == old.source_ref
    assert kernel.status("ticket:3") == before
    assert kernel.source_status()["blocked"] is None
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.requeue("ticket:4", "exact-requeue", replace(replacement, source_ref="refs/heads/foreign"), world)
    kernel.receive(second, raw)  # Old exact raw is history only after replacement.
    assert kernel.status("ticket:4")["pending_attempt"] is None
    assert kernel.advance("ticket:3", token, demand_world(kernel, "ticket:3"), allow_agent=False).kind == "progress"
    assert kernel.status("ticket:3")["budgets"]["requirements"]["developments_used"] == 1


def test_graph_refresh_cannot_reopen_stop_and_late_siblings_settle_without_authority(tmp_path: Path) -> None:
    kernel, source = source_run(tmp_path / "run.sqlite3", independent_source())
    token = kernel.claim_executor("original")
    calls = [kernel.advance(subject, token, demand_world(kernel, subject)).call for subject in ("ticket:3", "ticket:4")]
    kernel.control("stop", "operator-stop")
    removed = replace(source, parent=replace(source.parent, sub_issue_numbers=(4,)), issues=(source.issues[1],))
    kernel.refresh(removed)
    assert kernel.source_status()["blocked"] == "unsupported_scope_change"
    assert kernel.source_status()["stopped"] is True
    assert kernel.source_status()["frontier"] == []
    for call in calls:
        assert call is not None
        kernel.receive(call, b'{"result_kind":"development","summary":"late retained","human_blockers":null}')
        assert kernel.status(call.subject)["pending_return"] is True
        with pytest.raises(KernelError, match="executor_fenced"):
            kernel.advance(call.subject, token, demand_world(kernel, call.subject))
    kernel.refresh(source)
    assert kernel.source_status()["blocked"] is None
    assert kernel.source_status()["stopped"] is True
    kernel.confirm_executor_exit(token)
    kernel.control("resume", "explicit-resume")
    replacement = kernel.claim_executor("replacement")
    for subject in ("ticket:3", "ticket:4"):
        assert kernel.advance(subject, replacement, demand_world(kernel, subject), allow_agent=False).kind == "progress"
        assert kernel.status(subject)["budgets"]["requirements"]["developments_used"] == 1
