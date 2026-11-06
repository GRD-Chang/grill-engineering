"""Native Issue inputs through the shared Core operation runner."""

from support.core_operations import prepare_scripted_agent

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.model import Policy, Requirements
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from support.offline import GraphScenario
from agent_run.model import Effect, Execution, HostObservation
from support.core_operations import CoreOperations, scripted_return
from support.api_observation import api_writes, lose_api_reply, pr_author_read
from support.requirements_scenarios import independent_source, native_input, source_run


def test_closed_local_dependency_without_completion_never_unblocks_or_creates_final(tmp_path: Path) -> None:
    # Graph eligibility has no Git/PR precondition. The following test retains
    # the real close -> Completion -> dependency -> Final boundary.
    kernel, source = source_run(tmp_path / "source" / "kernel.sqlite3")
    initial = kernel.source_status()
    assert initial["frontier"] == ["ticket:3"]
    assert initial["remaining"] == {"ticket:4": "blocked_by_incomplete_tickets"}
    assert initial["final_subject"] is None
    closed = replace(source, issues=tuple(replace(issue, state="CLOSED") if issue.number == 3 else
        replace(issue, blocked_by=tuple(replace(blocker, state="CLOSED") for blocker in issue.blocked_by))
        for issue in source.issues))
    kernel.refresh(lambda: closed)
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    state = reopened.source_status()
    assert state["frontier"] == []
    assert state["remaining"] == {"ticket:3": "ticket_closed", "ticket:4": "blocked_by_incomplete_tickets"}
    assert state["final_subject"] is None
    # Observe normal scheduling without credential delivery or Git effects.
    token = reopened.claim_executor("graph-eligibility")
    from support.core_operations import ReadOnlyPorts
    from agent_run.core.operation_contract import DeliveryFacts
    delivery = reopened.delivery("ticket:4")
    ports = ReadOnlyPorts(closed, delivery.source_ref, delivery.target_ref, DeliveryFacts(delivery.base, None))
    step = CoreOperations(reopened, token, lambda _binding: ports, prepare_agents=False).once()
    assert step.call is None and step.preparation is None
    assert reopened.status("ticket:4")["pending_attempt"] is None


def test_real_ticket_close_receipts_create_completion_before_dependency_and_final(tmp_path: Path) -> None:
    source = Requirements.from_wire(native_input()["initial_requirements"])
    scenario = GraphScenario(tmp_path / "graph", source, record_checkpoints=False)
    assert scenario.kernel.source_status()["final_subject"] is None
    assert scenario.run(cut="publication_pending").kind == "publication_pending"
    assert scenario.kernel.status("ticket:3")["phase"] == "publish"
    assert scenario.run().kind == "complete"
    completed = scenario.kernel.source_status()["completions"]
    assert [entry["subject"] for entry in completed] == ["ticket:3"]
    assert completed[0]["source_sha"] == scenario.port.pr_by_number(completed[0]["pr_number"]).head.sha
    assert scenario.kernel.source_status()["frontier"] == ["ticket:4"]
    scenario.select("ticket:4")
    assert scenario.run(cut="publication_pending").kind == "publication_pending"
    assert scenario.kernel.status("ticket:4")["phase"] == "publish"
    # Close has actually happened; a fresh source read sees CLOSED before its
    # original receipt is consumed. It cannot substitute for Completion.
    for _ in range(100):
        step = scenario.step()
        pending = scenario.kernel.status("ticket:4")["pending_write"]
        if pending is not None and pending["operation"] == "close" and pending["status"] == "observed":
            break
    else:
        raise AssertionError("second Ticket never reached its close intent")
    assert len(scenario.kernel.source_status()["completions"]) == 1
    scenario.refresh()
    assert scenario.kernel.source_status()["final_subject"] is None
    assert scenario.run().kind == "complete"
    before = scenario.port.remote_commit("refs/heads/run")
    original_completions = scenario.kernel.source_status()["completions"]
    scenario.requirements = replace(source, parent=replace(source.parent, body="Clarified overall Parent outcome."),
                                    issues=tuple(replace(issue, title="Post-completion title edit", body="Edited after delivery")
                                                 if issue.number == 3 else issue for issue in source.issues))
    scenario.refresh()
    assert scenario.kernel.source_status()["completions"] == original_completions
    assert scenario.kernel.source_status()["blocked"] is None
    scenario.kernel = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    activation = operations.until(lambda step: step.reason == "final_activated")
    final = activation.subject
    for subject in ("ticket:3", "ticket:4"):
        assert scenario.kernel.status(subject)["resource_cleanup"]["state"] == "complete"
    scenario.select(final)
    assert scenario.kernel.delivery(final).base == before
    assert scenario.run().kind == "approval"
    assert scenario.runner().state().disposition == "quiescent"
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
    assert scenario.port.remote_commit("refs/heads/main") != scenario.port.remote_commit("refs/heads/run")
    # Maintainer revision consumes this real Final approval and updates its PR.
    from support.control_scenarios import admit_scenario
    admit_scenario(scenario, tmp_path)
    request = scenario.kernel.host_request("start")
    old_pr = scenario.port.pr_by_number(scenario.kernel.status(final)["pr_number"])
    feedback = "\n请补充默认分支兼容性。\r\n"
    for _ in range(2):
        assert scenario.kernel.control_host(request, "revise", "maintainer-revision", subject=final,
            world=scenario.world(), message=feedback) == "applied"
    with pytest.raises(KernelError, match="action_identity_changed"):
        scenario.kernel.control_host(request, "revise", "maintainer-revision", subject=final,
            world=scenario.world(), message="A different revision")
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    call = operations.until(lambda step: step.call is not None).call
    assert call.role == "development"
    assert call.context["repair_source"] == "human_revision" and call.context["human_feedback"] == feedback
    scripted_return(scenario.kernel, operations.runner, call, scenario.agent)
    assert scenario.run().kind == "approval"
    revised = scenario.port.pr_by_number(old_pr.number)
    assert revised.head != old_pr.head
    assert scenario.kernel.status(final)["pr_number"] == old_pr.number
    scenario.kernel.control("approve", "approve-whole-parent", subject=final, world=scenario.world())
    assert scenario.run().kind == "complete"
    assert scenario.runner().state().disposition == "complete"
    assert scenario.kernel.status(final)["phase"] == "complete"
    assert scenario.kernel.status(final)["resource_cleanup"]["state"] == "complete"


def test_requirement_requeue_is_explicit_idempotent_and_retains_sibling_return(tmp_path: Path) -> None:
    source = independent_source()
    scenario = GraphScenario(tmp_path / "requeue", source, policy=Policy(concurrency=2), record_checkpoints=False)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    first = operations.until(lambda step: step.call is not None).call
    second = operations.until(lambda step: step.call is not None).call
    assert first is not None and second is not None
    assert (first.subject, second.subject) == ("ticket:3", "ticket:4")
    raw = b'{"result_kind":"development","summary":"retained exact return","human_blockers":null}'
    scripted_return(kernel, operations.runner, second, lambda call: raw)
    scripted_return(kernel, operations.runner, first, lambda call: raw)
    before = kernel.status("ticket:3")
    scenario.requirements = replace(source, issues=tuple(replace(issue, body=issue.body + "\nNew requirement")
        if issue.number == 4 else issue for issue in source.issues))
    world = operations.runner.observe("ticket:4")
    assert kernel.source_status()["blocked"] == "requeue_required"
    assert operations.once().kind == "quiescent"
    assert kernel.status("ticket:3")["pending_return"] is True
    old = kernel.delivery("ticket:4")
    replacement = replace(old, source_ref="refs/heads/ticket-4-g2", checkout=str(Path(old.checkout).with_name("work-g2")), base=world.target)
    assert kernel.requeue("ticket:4", "exact-requeue", replacement, world) == "applied"
    assert kernel.requeue("ticket:4", "exact-requeue", replacement, world) == "applied"
    assert kernel.status("ticket:4")["generation"] == 2
    assert kernel.status("ticket:4")["retired_generations"][0]["source_ref"] == old.source_ref
    assert kernel.status("ticket:3") == before
    assert kernel.source_status()["blocked"] is None
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.requeue("ticket:4", "exact-requeue", replace(replacement, source_ref="refs/heads/foreign"), world)
    kernel.receive(second, raw)
    assert kernel.status("ticket:4")["pending_attempt"] is None
    consumed = operations.once(agent_capacity=0)
    assert consumed.kind == "progress" and consumed.subject == "ticket:3"
    assert kernel.status("ticket:3")["budgets"]["requirements"]["developments_used"] == 1


def test_managed_requeue_preserves_sibling_and_dirty_work_after_lost_ack_and_target_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = GraphScenario(tmp_path / "managed-requeue", independent_source(), policy=Policy(concurrency=2), record_checkpoints=False)
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
    now = [100.0]
    kernel.clock = lambda: now[0]
    scenario.checks = ["pending"]  # Real CI waiting lets Core select the independent sibling.
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    sibling = None
    for _ in range(160):
        step = operations.once()
        if step.call is not None:
            if step.call.subject == "ticket:4":
                assert sibling is None
                sibling = step.call
            else:
                scripted_return(kernel, operations.runner, step.call, scenario.agent)
        if kernel.status(subject)["phase"] == "checks" and sibling is not None:
            break
    else:
        raise AssertionError("Original Ticket never published its PR")
    assert sibling is not None
    scenario.checks = ["none"]
    now[0] = 105.0  # The original CI poll is due; observing never renews its window.
    original = kernel.delivery(subject)
    original_pr = scenario.port.pull_request_by_ref(original.source_ref, original.target_ref)
    assert original_pr is not None and original_pr.state == "OPEN"
    preserved = Path(original.checkout) / "operator-note.txt"
    preserved.write_text("Uncommitted work from the retired generation\n")
    assert kernel.dispatch_agent(sibling)
    sibling_before = kernel.status("ticket:4")
    scenario.select(subject)
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body + "\nClarified requirement") if issue.number == original.issue else issue
        for issue in scenario.requirements.issues))
    world = scenario.world()
    replacement = replace(original, source_ref=original.source_ref + "-g2", checkout=str(Path(original.checkout).with_name("ticket-3-g2")),
                          base=world.target)
    assert kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=world,
                               replacement=replacement) == "pending"
    assert kernel.control_host(request, "requeue", "replace-generation", subject=subject, world=world,
                               replacement=replacement) == "pending"
    assert kernel.status(subject)["generation"] == 1
    sent_retirements = []
    execute = scenario.port.execute
    def capture_retirement(effect, authorize):
        if effect.operation == "retire_pr":
            sent_retirements.append(effect)
        return execute(effect, authorize)
    scenario.port.execute = capture_retirement
    lose_api_reply(monkeypatch, scenario.port.database, "PATCH",
                   f"repos/{original_pr.repository}/pulls/{original_pr.number}")
    retirement_step = operations.once()
    if retirement_step.retry_at is not None:
        now[0] = retirement_step.retry_at
    assert len(sent_retirements) == 1
    retirement = sent_retirements[0]
    assert retirement.pr_number == original_pr.number and retirement.before_sha == original_pr.head.sha
    assert kernel.status(subject)["generation"] == 1
    assert not kernel.status(subject)["retired_generations"]
    assert scenario.port.pr_by_number(original_pr.number).state == "CLOSED"
    def retirement_intents():
        commands = [write for write in scenario.port.writes() if write["operation"] == "retire_pr"]
        assert len(commands) == 2
        comment = f"repos/{original_pr.repository}/issues/{original_pr.number}/comments"
        close = f"repos/{original_pr.repository}/pulls/{original_pr.number}"
        assert api_writes(scenario.port.objects, {comment, close}) == [("POST", comment), ("PATCH", close)]
        return {write["intent"] for write in commands}
    scenario.port.external_remote_append(original.target_ref,
        {"independent-target.txt": "Independent target advancement\n"}, "after-retirement")
    assert scenario.step().kind == "progress"
    assert kernel.status(subject)["generation"] == 1
    assert scenario.step().kind == "progress"
    assert kernel.status(subject)["generation"] == 2
    assert kernel.status("ticket:4") == sibling_before
    kernel.guard_agent(sibling)
    assert preserved.read_text() == "Uncommitted work from the retired generation\n"
    assert scenario.port.ref_commit(replacement.source_ref) is None
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None and call.subject == subject and call.context["generation"] == 2
    assert scenario.port.pr_by_number(original_pr.number).state == "CLOSED"
    assert len(retirement_intents()) == 1


def test_graph_refresh_cannot_reopen_stop_and_late_siblings_settle_without_authority(tmp_path: Path) -> None:
    source = independent_source()
    scenario = GraphScenario(tmp_path / "scope", source, policy=Policy(concurrency=2), record_checkpoints=False)
    kernel, token = scenario.kernel, scenario.execution
    operations = CoreOperations(kernel, token, scenario.make_ports)
    calls = [operations.until(lambda step: step.call is not None).call for _ in range(2)]
    assert all(call is not None for call in calls)
    assert [call.subject for call in calls] == ["ticket:3", "ticket:4"]
    raw = []
    for call in calls:
        operations.runner.authorize_agent(call)
        raw.append(scenario.agent(call))  # Real external work finishes before its late return.
    kernel.control("stop", "operator-stop")
    scenario.requirements = replace(source, parent=replace(source.parent, sub_issue_numbers=(4,)), issues=(source.issues[1],))
    # Source-only observation is the public Kernel contract also used by manual
    # lifecycle controls. It grants no automatic operation or Worker authority.
    kernel.refresh(lambda: operations.runner.ports.source(1))
    assert kernel.source_status()["blocked"] == "unsupported_scope_change"
    assert kernel.source_status()["stopped"] is True
    assert kernel.source_status()["frontier"] == []
    for call, result in zip(calls, raw):
        kernel.receive(call, result)
        assert kernel.status(call.subject)["pending_return"] is True
        fenced = operations.once()
        assert fenced.kind == "paused" and fenced.reason == "executor_fenced"
    scenario.requirements = source
    kernel.refresh(lambda: operations.runner.ports.source(1))
    assert kernel.source_status()["blocked"] is None
    assert kernel.source_status()["stopped"] is True
    kernel.confirm_executor_exit(token)
    kernel.control("resume", "explicit-resume")
    replacement = kernel.claim_executor("replacement")
    resumed = CoreOperations(kernel, replacement, scenario.make_ports)
    for subject in ("ticket:3", "ticket:4"):
        resumed.until(lambda step: kernel.status(subject)["phase"] == "candidate", agent_capacity=0)
        assert kernel.status(subject)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize("outcome", ["target", "prior", "third", "foreign_receipt"])
def test_original_final_approval_settlement_uses_its_grant_before_changed_requirements(outcome: str) -> None:
    from support.final_merge_record import approved_final_merge
    from agent_run.model import EffectResult

    with approved_final_merge() as (db, kernel, request, original, receipt):
        subject = request.effect.subject
        before = tuple(db.execute("SELECT * FROM budgets").fetchone())
        intent = tuple(db.execute("SELECT payload_id,attempts FROM effects").fetchone())
        # A later source observation is input to this consumer, not a fabricated
        # generation transition. The real lost-ACK journey below owns its producer.
        db.execute("INSERT INTO source_observations(run_id,sequence,parent_revision,graph_revision,body) "
                   "SELECT run_id,2,'new-parent',graph_revision,body FROM source_observations")
        db.execute("UPDATE runs SET observed_parent_revision='new-parent',source_sequence=2")
        world = replace(original, parent_revision="new-parent", revision="new-parent")
        if outcome == "foreign_receipt":
            receipt = replace(receipt, pr=replace(receipt.pr, actor="foreign-publisher"))
        elif outcome in {"prior", "third"}:
            receipt = EffectResult(outcome)
        with kernel.storage.transaction(write=False) as tx:
            token = kernel._operation_token(tx, subject)
        step = kernel._reconcile(request, receipt, world, token=token)
        if outcome in {"target", "foreign_receipt"}:
            assert step.kind == "progress"
            def consume():
                with kernel.storage.transaction() as tx:
                    run, job = kernel._owned(tx, subject, request.work.execution)
                    row = tx.one("SELECT * FROM effects")
                    return kernel._apply_effect(tx, run, job, row, world)
            if outcome == "foreign_receipt":
                with pytest.raises(KernelError, match="foreign_merge_receipt"):
                    consume()
                assert db.execute("SELECT phase,generation FROM subjects").fetchone()[:] == ("merge", 1)
            else:
                assert consume().kind == "progress"
                with kernel.storage.transaction() as tx:
                    run, job = kernel._owned(tx, subject, request.work.execution)
                    assert kernel._final_closing(tx, run, job)
                    assert not kernel._effect_stale(tx, run, job, tx.one("SELECT * FROM effects"))
                    assert kernel._current(tx, job, run, world)
                assert db.execute("SELECT phase,generation FROM subjects").fetchone()[:] == ("completion_comment", 1)
        elif outcome == "prior":
            assert step.kind == "progress"
            assert db.execute("SELECT status FROM effects").fetchone()[0] == "retired"
            assert db.execute("SELECT phase,refresh_revision,grant_id FROM subjects").fetchone()[:] == ("refresh_final", "new-parent", None)
            with kernel.storage.transaction() as tx:
                run, job = kernel._owned(tx, subject, request.work.execution)
                assert kernel._refresh_final(tx, run, job, world).reason == "fresh_whole_run_review"
            assert db.execute("SELECT phase,generation,review_attempt,grant_id FROM subjects").fetchone()[:] == ("workspace", 2, None, None)
        else:
            assert (step.kind, step.reason) == ("paused", "third")
            assert db.execute("SELECT status FROM effects").fetchone()[0] == "sent"
            assert db.execute("SELECT generation FROM subjects").fetchone()[0] == 1
        assert tuple(db.execute("SELECT payload_id,attempts FROM effects").fetchone()) == intent
        assert tuple(db.execute("SELECT * FROM budgets").fetchone()) == before
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def final_merge_in_flight(path: Path) -> tuple[GraphScenario, Effect]:
    """Real Ticket Completion, full Review and human approval precede the fault."""
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(path, source, record_checkpoints=False)
    assert scenario.run().kind == "complete"
    activation = scenario.runner().run_once()
    assert activation.reason == "final_activated"
    scenario.select(activation.subject)
    assert scenario.run().kind == "approval"
    final = scenario.delivery.key
    scenario.kernel.control("approve", "original-final-approval", subject=final, world=scenario.world())
    sent = []
    execute = scenario.port.execute
    class AcknowledgementCut(BaseException):
        """Host interruption after the actual merge, before local readback."""
    def capture_merge(effect, authorize):
        result = execute(effect, authorize)
        if effect.operation == "merge" and effect.subject == final:
            sent.append(effect)
            raise AcknowledgementCut()
        return result
    scenario.port.execute = capture_merge
    runner = scenario.runner()
    try:
        for _ in range(20):
            runner.run_once()
    except AcknowledgementCut:
        assert len(sent) == 1
        effect = sent[0]
        assert scenario.kernel.status(final)["pending_write"]["status"] == "sent"
        return scenario, effect
    finally:
        scenario.port.execute = execute
    raise AssertionError("Final approval did not send its exact merge")


def test_original_final_merge_keeps_closeout_after_uncertain_reads_target_append_and_parent_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario, effect = final_merge_in_flight(tmp_path / "graph")
    before = scenario.kernel.status(effect.subject)
    for actor in (None, "foreign-publisher"):
        restore_read = pr_author_read(monkeypatch, scenario.port.database, effect.pr_number, actor)
        paused = scenario.runner().run_once()
        assert paused.kind == "paused"
        assert scenario.kernel.status(effect.subject)["pending_write"]["id"] == effect.effect_id
        assert not [write for write in scenario.port.writes() if write["subject"] == effect.subject and write["operation"] == "close"]
        with pytest.raises(KernelError, match="final_merge_unsettled"):
            scenario.kernel.control("abandon", "cannot-abandon-default-merge",
                resource_inspections=scenario.port.inspect_resources(scenario.kernel.generation_resources()))
        assert scenario.kernel.source_status()["abandonment"] is None
        restore_read()
    pr = scenario.port.pr_by_number(effect.pr_number)
    assert pr is not None and pr.merged is not None
    local_before = scenario.port.git.commit(effect.target_ref)
    advanced = scenario.port.external_remote_append(effect.target_ref, {"later.txt": "Later independent work\n"}, "after-merge")
    assert scenario.port.git.commit(effect.target_ref) == local_before
    observed = scenario.port.reconcile(effect)
    assert observed.disposition == "target" and observed.commit == pr.merged
    scenario.kernel = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    scenario.requirements = replace(scenario.requirements, parent=replace(scenario.requirements.parent, body="Changed after uncertain original merge readback."))
    # Original sent intent readback precedes any source refresh, even across restart.
    assert scenario.runner().run_once().kind == "progress"
    assert any(event["kind"] == "effect_readback" and event["identity"] == effect.effect_id
               for event in scenario.kernel.history(effect.subject))
    scenario.kernel.control("resume", "resume-original-closeout", subject=effect.subject)
    assert scenario.run().kind == "complete"
    assert scenario.port.remote_commit(effect.target_ref) == advanced
    after = scenario.kernel.status(effect.subject)
    assert after["generation"] == before["generation"]
    assert after["budgets"] == before["budgets"]
    assert [call["role"] for call in scenario.role_calls if call["subject"] == effect.subject] == ["review", "publication"]
    writes = [write for write in scenario.port.writes() if write["subject"] == effect.subject]
    assert len([write for write in writes if write["operation"] == "merge"]) == 1
    assert len([write for write in writes if write["operation"] == "close"]) == 1
    assert not [write for write in scenario.port.writes() if write["operation"] == "reopen_ticket"]


def test_final_requirement_refresh_never_reuses_old_generation_human_response(tmp_path: Path) -> None:
    original = Requirements.from_wire(native_input()["initial_requirements"])
    source = replace(original, parent=replace(original.parent, sub_issue_numbers=(3,)), issues=(original.issues[0],))
    scenario = GraphScenario(tmp_path / "graph", source, reviews=("pass", "blocked", "pass"), record_checkpoints=False)
    assert scenario.run().kind == "complete"
    activation = prepare_scripted_agent(scenario.kernel, scenario.runner().run_once())
    assert activation.reason == "final_activated"
    scenario.select(activation.subject)
    assert scenario.run().reason == "human_blocker"
    final = scenario.delivery.key
    scenario.kernel.control("resume", "answer-old-generation", subject=final, message="Decision for original Parent requirements only.")
    scenario.requirements = replace(source, parent=replace(source.parent, body="New Parent requirements retire the old response."))
    fresh = scenario.world()
    before = scenario.kernel.status(final)
    calls_before, writes_before = len(scenario.role_calls), len(scenario.port.writes())
    blocked = prepare_scripted_agent(scenario.kernel, scenario.runner().run_once())
    assert blocked.kind in {"paused", "quiescent"}
    assert scenario.kernel.status(final) == before
    assert len(scenario.role_calls) == calls_before
    assert len(scenario.port.writes()) == writes_before
    assert scenario.kernel.control("requeue", "accept-new-parent-without-old-response", subject=final, world=fresh) == "applied"
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    step = operations.once()
    assert step.kind != "paused", step
    call = step.call or operations.until(lambda step: step.call is not None).call
    assert call is not None
    assert call.context["generation"] == 2
    assert not call.context.get("human_responses")
    assert scenario.kernel.dispatch_agent(call)
    scenario.kernel.agent_unavailable(call, "worker_credentials_unavailable", "Synthetic prelaunch credential absence")
    scenario.kernel.control("resume", "continue-current-generation", subject=final)
    continuation = operations.until(lambda step: step.call is not None).call
    assert continuation is not None and continuation.attempt_id == call.attempt_id
    assert continuation.context["generation"] == 2
    assert not continuation.context.get("human_responses")
