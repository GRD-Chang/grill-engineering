"""Old native Issue inputs through the replacement domain's public seam."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, Requirements, RunSpec, World
from agent_run_kernel.offline import GraphScenario
from agent_run_kernel.model import Effect, EffectResult, Execution, HostObservation


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
    kernel.refresh(lambda: closed)
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
    kernel.refresh(lambda: changed)
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


@pytest.mark.parametrize("retirement_race", ["none", "foreign_author", "target_append_after_receipt", "target_append_after_lost_ack", "requirements_after_receipt", "requirements_then_foreign_close"])
def test_managed_requeue_reconciles_the_original_pr_before_replacing_its_generation(tmp_path: Path, retirement_race: str) -> None:
    scenario = GraphScenario(tmp_path / "managed-requeue", independent_source(), policy=Policy(concurrency=2))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    for _ in range(100):
        scenario.step()
        if kernel.status(subject)["phase"] == "checks":
            break
    else:
        raise AssertionError("Original Ticket never published its PR")
    original_pr = scenario.port.pull_request(subject)
    assert original_pr is not None and original_pr.state == "OPEN"
    original = kernel.delivery(subject)
    preserved = Path(original.checkout) / "operator-note.txt"
    preserved.write_text("Uncommitted work from the retired generation\n")
    scenario.select("ticket:4")
    for _ in range(20):
        step = kernel.advance("ticket:4", scenario.execution, scenario.world())
        if step.call is not None:
            sibling = step.call
            break
        scenario.handle(step)
    else:
        raise AssertionError("Independent sibling never acquired its Invocation")
    assert kernel.dispatch_agent(sibling)
    sibling_before = kernel.status("ticket:4")
    scenario.select(subject)
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body + "\nClarified requirement") if issue.number == original.issue else issue
        for issue in scenario.requirements.issues))
    world = scenario.world()
    replacement = replace(original, source_ref=original.source_ref + "-g2", checkout=str(Path(original.checkout).with_name("ticket-3-g2")),
                          base=world.target)
    if retirement_race == "foreign_author":
        scenario.port._write_pr(subject, replace(original_pr, actor="foreign-pr-author"))
        with pytest.raises(KernelError, match="retirement_pr_changed"):
            kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=scenario.world(),
                                replacement=replacement)
        assert kernel.status(subject)["generation"] == 1
        assert not [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
        return
    assert kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=world,
                               replacement=replacement) == "pending"
    assert kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=world,
                               replacement=replacement) == "pending"
    assert kernel.status(subject)["generation"] == 1
    retirement = kernel.advance(subject, scenario.execution, scenario.world())
    assert retirement.effect is not None and retirement.effect.operation == "retire_pr"
    assert retirement.effect.pr_number == original_pr.number and retirement.effect.before_sha == original_pr.head.sha
    if retirement_race not in {"target_append_after_receipt", "requirements_after_receipt", "requirements_then_foreign_close"}:
        scenario.port.lose_response.add("retire_pr")
    scenario.handle(retirement)
    assert kernel.status(subject)["generation"] == 1
    assert not kernel.status(subject)["retired_generations"]
    assert scenario.port.pull_request(subject).state == "CLOSED"
    if retirement_race in {"requirements_after_receipt", "requirements_then_foreign_close"}:
        scenario.requirements = replace(scenario.requirements, issues=tuple(
            replace(issue, body=issue.body + "\nAnother requirement after the accepted Action") if issue.number == original.issue else issue
            for issue in scenario.requirements.issues))
        assert scenario.step().kind == "paused"
        assert kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=world,
                                   replacement=replacement) == "superseded"
        assert kernel.status(subject)["generation"] == 1
        assert kernel.status(subject)["pending_write"] is None
        assert not kernel.status(subject)["retired_generations"]
        assert len([write for write in scenario.port.writes() if write["operation"] == "retire_pr"]) == 1
        if retirement_race == "requirements_then_foreign_close":
            scenario.port.external_pr_transition(original_pr.number, "OPEN", "another-maintainer")
            scenario.port.external_pr_transition(original_pr.number, "CLOSED", "another-maintainer")
        latest = scenario.world()
        renewed = replace(replacement, base=latest.target)
        assert kernel.control_host(request, "requeue", "accept-new-requirements", subject=subject, world=latest,
                                   replacement=renewed) == "pending"
        checked = kernel.advance(subject, scenario.execution, scenario.world())
        assert checked.kind == "reconcile" and checked.effect is not None
        assert checked.effect.effect_id == retirement.effect.effect_id and checked.effect.operation == "retire_pr"
        scenario.handle(checked)
        assert kernel.status(subject)["generation"] == 1
        if retirement_race == "requirements_then_foreign_close":
            assert kernel.control_host(request, "requeue", "accept-new-requirements", subject=subject, world=latest,
                                       replacement=renewed) == "superseded"
            assert kernel.status(subject)["pending_write"] is None
            assert len([write for write in scenario.port.writes() if write["operation"] == "retire_pr"]) == 1
            return
        assert scenario.step().kind == "progress"
        assert kernel.status(subject)["generation"] == 2
        assert scenario.run().kind == "complete"
        assert scenario.port.pr_by_number(original_pr.number).state == "CLOSED"
        assert len([write for write in scenario.port.writes() if write["operation"] == "retire_pr"]) == 1
        return
    if retirement_race in {"target_append_after_receipt", "target_append_after_lost_ack"}:
        scenario.port.external_remote_append(original.target_ref, {"independent-target.txt": "Independent target advancement\n"}, "after-retirement")
    if retirement_race not in {"target_append_after_receipt", "requirements_after_receipt", "requirements_then_foreign_close"}:
        assert scenario.step().kind == "reconcile"
        assert kernel.status(subject)["generation"] == 1
    assert scenario.step().kind == "progress"
    assert kernel.status(subject)["generation"] == 2
    assert kernel.status("ticket:4") == sibling_before
    kernel.guard_agent(sibling)
    assert preserved.read_text() == "Uncommitted work from the retired generation\n"
    assert scenario.port.ref_commit(replacement.source_ref) is None
    assert scenario.run().kind == "complete"
    replacement_pr = scenario.port.pull_request(subject)
    assert replacement_pr is not None and replacement_pr.number != original_pr.number
    assert scenario.port.pr_by_number(original_pr.number).state == "CLOSED"
    assert len([write for write in scenario.port.writes() if write["operation"] == "retire_pr"]) == 1


def test_graph_refresh_cannot_reopen_stop_and_late_siblings_settle_without_authority(tmp_path: Path) -> None:
    kernel, source = source_run(tmp_path / "run.sqlite3", independent_source())
    token = kernel.claim_executor("original")
    calls = [kernel.advance(subject, token, demand_world(kernel, subject)).call for subject in ("ticket:3", "ticket:4")]
    kernel.control("stop", "operator-stop")
    removed = replace(source, parent=replace(source.parent, sub_issue_numbers=(4,)), issues=(source.issues[1],))
    kernel.refresh(lambda: removed)
    assert kernel.source_status()["blocked"] == "unsupported_scope_change"
    assert kernel.source_status()["stopped"] is True
    assert kernel.source_status()["frontier"] == []
    for call in calls:
        assert call is not None
        kernel.receive(call, b'{"result_kind":"development","summary":"late retained","human_blockers":null}')
        assert kernel.status(call.subject)["pending_return"] is True
        with pytest.raises(KernelError, match="executor_fenced"):
            kernel.advance(call.subject, token, demand_world(kernel, call.subject))
    kernel.refresh(lambda: source)
    assert kernel.source_status()["blocked"] is None
    assert kernel.source_status()["stopped"] is True
    kernel.confirm_executor_exit(token)
    kernel.control("resume", "explicit-resume")
    replacement = kernel.claim_executor("replacement")
    for subject in ("ticket:3", "ticket:4"):
        assert kernel.advance(subject, replacement, demand_world(kernel, subject), allow_agent=False).kind == "progress"
        assert kernel.status(subject)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize("dirty_repair", [False, True])
def test_final_parent_drift_retires_old_work_and_restarts_full_review_without_refund(tmp_path: Path, dirty_repair: bool) -> None:
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(tmp_path / "graph", source, reviews=("pass", "fail", "pass") if dirty_repair else ("pass",))
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    if dirty_repair:
        for _ in range(100):
            step = scenario.step()
            if step.call is not None and step.call.role.value == "development":
                break
        else:
            raise AssertionError("Final Findings never reached the real Repair checkout")
    else:
        assert scenario.run(cut="publication_pending").kind == "publication_pending"
    before = scenario.kernel.status(scenario.delivery.key)
    run_head = scenario.port.git.commit("refs/heads/run")
    old_checkout = Path(scenario.delivery.checkout)
    old_contents = {path.name: path.read_bytes() for path in old_checkout.iterdir() if path.is_file()}
    scenario.requirements = replace(source, parent=replace(source.parent, body="Updated Parent requirements after whole-Run work."))
    scenario.refresh()
    refreshed = scenario.kernel.status(scenario.delivery.key)
    assert refreshed["phase"] == "refresh_final"
    assert refreshed["budgets"] == before["budgets"]
    assert scenario.run(cut="publication_pending").kind == "publication_pending"
    after = scenario.kernel.status(scenario.delivery.key)
    assert after["generation"] == before["generation"] + 1
    assert scenario.kernel.delivery(scenario.delivery.key).source_ref == "refs/heads/run"
    assert Path(scenario.kernel.delivery(scenario.delivery.key).checkout) != old_checkout
    assert old_contents == {path.name: path.read_bytes() for path in old_checkout.iterdir() if path.is_file()}
    assert scenario.port.git.commit("refs/heads/run") == run_head
    assert after["budgets"]["integration"]["developments_used"] == int(dirty_repair)
    assert after["budgets"]["integration"]["reviews_used"] == before["budgets"]["integration"]["reviews_used"] + 1
    final_calls = [call for call in scenario.role_calls if call["subject"] == scenario.delivery.key]
    assert [call["role"] for call in final_calls] == (["review", "development", "review"] if dirty_repair else ["review", "review"])
    assert final_calls[-1]["context"]["requirements"][0]["body"] == scenario.requirements.parent.body
    assert "previous_review" not in final_calls[-1]["context"]


@pytest.mark.parametrize("outcome", ["target", "prior", "third", "foreign_receipt"])
def test_original_final_approval_reconciles_before_parent_requirement_refresh(tmp_path: Path, outcome: str) -> None:
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(tmp_path / "graph", source)
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    assert scenario.run().kind == "approval"
    final = scenario.delivery.key
    scenario.kernel.control("approve", "original-final-approval", subject=final, world=scenario.world())
    for _ in range(20):
        step = scenario.kernel.advance(final, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "merge":
            effect = step.effect
            break
        scenario.handle(step)
    else:
        raise AssertionError("Final approval did not prepare its exact merge")
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    if outcome in {"target", "foreign_receipt"}:
        scenario.port.execute(effect, lambda: scenario.kernel.guard_effect(effect, scenario.execution, scenario.world()))
    elif outcome == "third":
        third = scenario.port.external_remote_append(effect.source_ref, {"third.txt": "Another actor's work\n"}, "approved-third")
    scenario.requirements = replace(source, parent=replace(source.parent, body="Changed after the original approval was dispatched."))
    scenario.refresh()
    observed = scenario.port.reconcile(effect)
    if outcome == "foreign_receipt":
        assert observed.pr is not None
        observed = replace(observed, pr=replace(observed.pr, actor="foreign-publisher"))
    scenario.kernel.reconcile(effect, observed, scenario.execution, scenario.world())
    if outcome == "foreign_receipt":
        with pytest.raises(KernelError, match="foreign_merge_receipt"):
            scenario.kernel.advance(final, scenario.execution, scenario.world())
        assert scenario.kernel.status(final)["generation"] == 1
    elif outcome == "target":
        assert scenario.run().kind == "complete"
        assert scenario.kernel.status(final)["generation"] == 1
        assert [call["role"] for call in scenario.role_calls if call["subject"] == final] == ["review", "publication"]
        assert len([write for write in scenario.port.writes() if write["subject"] == final and write["operation"] == "merge"]) == 1
    elif outcome == "prior":
        assert scenario.run().kind == "approval"
        assert scenario.kernel.status(final)["generation"] == 2
        assert scenario.kernel.status(final)["budgets"]["integration"]["reviews_used"] == 2
    else:
        assert scenario.kernel.advance(final, scenario.execution, scenario.world()).kind == "reconcile"
        assert scenario.kernel.status(final)["pending_write"]["id"] == effect.effect_id
        assert scenario.port.remote_commit(effect.source_ref) == third
    if outcome != "target":
        assert not [write for write in scenario.port.writes() if write["subject"] == final and write["operation"] == "close"]


def final_merge_in_flight(path: Path) -> tuple[GraphScenario, Effect]:
    """Real Ticket Completion, full Review and human approval precede the fault."""
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(path, source)
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    assert scenario.run().kind == "approval"
    final = scenario.delivery.key
    scenario.kernel.control("approve", "original-final-approval", subject=final, world=scenario.world())
    for _ in range(20):
        step = scenario.kernel.advance(final, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "merge":
            effect = step.effect
            assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
            scenario.port.execute(effect, lambda: scenario.kernel.guard_effect(effect, scenario.execution, scenario.world()))
            return scenario, effect
        scenario.handle(step)
    raise AssertionError("Final approval did not send its exact merge")


def test_original_final_merge_settles_after_remote_target_appends(tmp_path: Path) -> None:
    # Old test_run_publication_reconciliation preserves the original grant
    # after our merge and a later external default-branch advance.
    scenario, effect = final_merge_in_flight(tmp_path / "graph")
    pr = scenario.port.pull_request(effect.subject)
    assert pr is not None and pr.merged is not None
    merged = pr.merged
    before = scenario.kernel.status(effect.subject)
    advanced = scenario.port.external_remote_append(effect.target_ref, {"later.txt": "Later independent work\n"}, "after-merge")
    assert scenario.port.git.commit(effect.target_ref) == merged
    assert scenario.port.remote_commit(effect.target_ref) == advanced
    observed = scenario.port.reconcile(effect)
    assert observed.disposition == "target"
    assert observed.commit == merged
    scenario.kernel.reconcile(effect, observed, scenario.execution, scenario.world())
    assert scenario.run().kind == "complete"
    assert scenario.port.remote_commit(effect.target_ref) == advanced
    assert scenario.kernel.status(effect.subject)["budgets"] == before["budgets"]
    assert len([write for write in scenario.port.writes() if write["subject"] == effect.subject and write["operation"] == "merge"]) == 1


@pytest.mark.parametrize("first_read", ["unknown", "third"])
def test_original_final_merge_keeps_closeout_after_uncertain_read_and_parent_drift(tmp_path: Path, first_read: str) -> None:
    scenario, effect = final_merge_in_flight(tmp_path / "graph")
    before = scenario.kernel.status(effect.subject)
    scenario.port.force_read["merge"] = first_read
    observed = scenario.port.reconcile(effect)
    assert scenario.kernel.reconcile(effect, observed, scenario.execution, scenario.world()).kind == "paused"
    assert not [write for write in scenario.port.writes() if write["subject"] == effect.subject and write["operation"] == "close"]
    scenario.port.force_read.clear()
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    scenario.requirements = replace(scenario.requirements, parent=replace(scenario.requirements.parent, body="Changed after uncertain original merge readback."))
    scenario.refresh()
    step = scenario.kernel.advance(effect.subject, scenario.execution, scenario.world())
    assert step.kind == "reconcile" and step.effect is not None and step.effect.effect_id == effect.effect_id
    scenario.handle(step)  # The exact original target is now actually observable.
    scenario.kernel.control("resume", "resume-original-closeout", subject=effect.subject)
    assert scenario.run().kind == "complete"
    after = scenario.kernel.status(effect.subject)
    assert after["generation"] == before["generation"]
    assert after["budgets"] == before["budgets"]
    assert [call["role"] for call in scenario.role_calls if call["subject"] == effect.subject] == ["review", "publication"]
    writes = [write for write in scenario.port.writes() if write["subject"] == effect.subject]
    assert len([write for write in writes if write["operation"] == "merge"]) == 1
    assert len([write for write in writes if write["operation"] == "close"]) == 1


def test_final_requirement_refresh_never_reuses_old_generation_human_response(tmp_path: Path) -> None:
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(tmp_path / "graph", source, reviews=("pass", "blocked", "pass"))
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    assert scenario.run().reason == "human_blocker"
    final = scenario.delivery.key
    scenario.kernel.control("resume", "answer-old-generation", subject=final, message="Decision for original Parent requirements only.")
    scenario.requirements = replace(source, parent=replace(source.parent, body="New Parent requirements retire the old response."))
    scenario.refresh()
    for _ in range(50):
        step = scenario.kernel.advance(final, scenario.execution, scenario.world())
        if step.call is not None:
            call = step.call
            break
        scenario.handle(step)
    else:
        raise AssertionError("Fresh Final generation did not reach Review")
    assert call.context["generation"] == 2
    assert not call.context.get("human_responses")
    assert scenario.kernel.dispatch_agent(call)
    scenario.kernel.agent_unavailable(call, "worker_credentials_unavailable", "Synthetic prelaunch credential absence")
    scenario.kernel.control("resume", "continue-current-generation", subject=final)
    continuation = scenario.kernel.advance(final, scenario.execution, scenario.world()).call
    assert continuation is not None and continuation.attempt_id == call.attempt_id
    assert continuation.context["generation"] == 2
    assert not continuation.context.get("human_responses")
