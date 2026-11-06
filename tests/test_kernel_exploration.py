"""Behavioral probes for the isolated replacement kernel, never legacy fixtures."""

from pathlib import Path
from dataclasses import asdict, replace
import json
import os
import subprocess
import sys

import pytest

from agent_run.model import DeliveryKind, Policy
from agent_run.core.kernel import Kernel
from agent_run.contracts.write_authority import FinalAdmission, WriteAuthority
from agent_run.errors import KernelError
from support.offline import Scenario, review_result
from agent_run.model import wire_json
from support.core_operations import CoreOperations, native_agent_fixture, scripted_return
from support.agent_continuation import ticket_spec
from support.effect_boundary import advance_to_effect
from support.run_scoped_kernel import domain_scenario


def test_return_is_durable_before_consumption_and_resume_does_not_charge_twice(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel, ports = native_agent_fixture(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    operations = CoreOperations(kernel, token, lambda binding: ports)
    first = operations.until(lambda step: step.call is not None)
    assert first.kind == "agent"
    assert first.call is not None
    assert first.call.role.value == "development"
    operations.runner.authorize_agent(first.call)
    kernel.receive(first.call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')

    reopened = Kernel(path, run_id=kernel.storage.run_id)
    observed = reopened.status("ticket:2")
    assert observed["pending_return"] is True
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    assert observed["phase"] == "development"
    consumed = CoreOperations(reopened, token, lambda binding: ports).once(agent_capacity=0)
    assert consumed.kind == "progress"
    assert reopened.status("ticket:2")["phase"] == "candidate"
    assert reopened.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize(("kind", "expected_roles", "development_purpose"), [
    (DeliveryKind.TICKET, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.PARENT_ONLY, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.FINAL_RUN, ["review", "development", "review", "publication"], "integration"),
])
def test_three_deliveries_complete_with_required_review_and_exact_git_history(
    tmp_path: Path, kind: DeliveryKind, expected_roles: list[str], development_purpose: str, domain_scenario,
) -> None:
    scenario = domain_scenario(tmp_path / "scenario", kind, reviews=("fail", "pass"))
    prefix_history = scenario.kernel.history("ticket:2") if kind == DeliveryKind.FINAL_RUN else None
    if kind != DeliveryKind.TICKET:
        assert scenario.run(cut="publication_pending").kind == "publication_pending"
        assert [call["role"] for call in scenario.role_calls] == expected_roles[:-1]
        assert not any(write["operation"] == "pr_write" for write in scenario.writes())
    original_target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert original_target is not None
    boundary = scenario.run()
    assert boundary.kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    if kind != DeliveryKind.TICKET:
        assert scenario.port.remote_commit(scenario.delivery.target_ref) == original_target
        scenario.kernel.control("approve", "exact-approval", subject=scenario.delivery.key, world=scenario.world())
        boundary = scenario.run()
    assert boundary.kind == "complete"
    assert [call["role"] for call in scenario.role_calls] == expected_roles
    assert {call["purpose"] for call in scenario.role_calls if call["role"] == "development"} == {development_purpose}
    assert sum(write["operation"] == "pr_write" for write in scenario.writes()) == 1
    assert sum(write["operation"] == "merge" for write in scenario.writes()) == 1
    assert sum(write["operation"] == "completion_comment" for write in scenario.writes()) == 1
    if kind == DeliveryKind.FINAL_RUN:
        assert scenario.historical_trace is not None
        assert scenario.historical_trace["raw_backend_calls"] == {"development": 1, "review": 1, "publication": 1}
        assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 2
        assert scenario.kernel.history("ticket:2") == prefix_history
        completion, = scenario.kernel.source_status()["completions"]
        assert completion["subject"] == "ticket:2" and completion["review_attempt"] is not None
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    physical = scenario.port.physical.commit(target.sha)
    assert physical["sha"] == scenario.port.physical.refs()[scenario.delivery.target_ref] == target.sha
    assert [parent["sha"] for parent in physical["parents"]] == list(scenario.port.git.parents(target.sha))
    assert len(scenario.port.git.parents(target.sha)) == (1 if kind == DeliveryKind.TICKET else 2)
    headers = {
        DeliveryKind.TICKET: "Parent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\n",
        DeliveryKind.PARENT_ONLY: "Parent Issue: #1\nDelivery Type: Parent-only\n\n",
        DeliveryKind.FINAL_RUN: "Parent Issue: #1\nDelivery Type: Final Run\n\n## Completed Tickets\n\n- #2: Deliver one ticket\n\n",
    }
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None and pr.state == "MERGED" and pr.body.startswith(headers[kind])
    assert scenario.kernel.status(scenario.delivery.key)["resource_cleanup"]["state"] == "complete"
    assert not Path(scenario.delivery.checkout).exists()
    assert scenario.port.remote_refs().keys() == ({"refs/heads/main", "refs/heads/run"} if kind == DeliveryKind.TICKET else {"refs/heads/main"})
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    assert scenario.port.git.run("ls-tree", "-r", "--name-only", target.sha).stdout.find("validation.tmp") == -1
    assert not list((scenario.port.root / "work").glob("review-*"))


def test_ticket_final_development_is_explicit_fallback_but_parent_cannot_skip_review(tmp_path: Path, domain_scenario) -> None:
    ticket = domain_scenario(tmp_path / "ticket", DeliveryKind.TICKET, reviews=("fail",), policy=Policy(ticket_rounds=1))
    assert ticket.run().kind == "complete"
    assert [call["role"] for call in ticket.role_calls] == ["development", "review", "development", "publication"]
    assert ticket.kernel.status(ticket.delivery.key)["gate_mode"] == "deterministic_fallback"
    parent = domain_scenario(tmp_path / "parent", DeliveryKind.PARENT_ONLY, reviews=("fail",), policy=Policy(parent_rounds=1))
    assert parent.run().kind == "paused"
    assert "review_budget_exhausted" in parent.kernel.status(parent.delivery.key)["gates"]
    assert not any(write["operation"] == "pr_write" for write in parent.port.writes())


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_ci_uses_separate_cumulative_development_and_parent_review(tmp_path: Path, kind: DeliveryKind, domain_scenario) -> None:
    scenario = domain_scenario(tmp_path / "scenario", kind, checks=("fail", "none"))
    assert scenario.run(approve=True).kind == "complete"
    ci = [call for call in scenario.role_calls if call["purpose"] == "ci"]
    assert [call["role"] for call in ci if call["role"] != "publication"] == (
        ["development"] if kind == DeliveryKind.TICKET else ["development", "review"])
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.writes()) == 2


def test_stop_late_raw_is_settlement_only_and_independent_process_can_read_it(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel, ports = native_agent_fixture(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    operations = CoreOperations(kernel, token, lambda binding: ports)
    step = operations.until(lambda step: step.call is not None)
    assert step.call is not None
    operations.runner.authorize_agent(step.call)
    kernel.control("stop", "stop-original")
    kernel.receive(step.call, b'{"result_kind":"development","summary":"retained","human_blockers":null}')
    with pytest.raises(KernelError, match="executor_fenced"):
        operations.runner.authorize_agent(step.call)
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((
        str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent))))
    command = "from pathlib import Path; from agent_run.core.kernel import Kernel; import json,sys; print(json.dumps(Kernel(Path(sys.argv[1]),run_id=sys.argv[2]).status('ticket:2')))"
    process = subprocess.run([sys.executable, "-c", command, str(path), kernel.storage.run_id], env=environment,
                             text=True, capture_output=True, check=True, timeout=15)
    observed = json.loads(process.stdout)
    assert observed["stopped"] is True
    assert observed["pending_return"] is True
    assert observed["phase"] == "development"
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    kernel.confirm_executor_exit(token)
    kernel.control("resume", "resume-original")
    with pytest.raises(KernelError, match="executor_fenced"):
        operations.runner.authorize_agent(step.call)
    replacement = kernel.claim_executor("executor-two")
    consume_command = """from pathlib import Path
from agent_run.core.kernel import Kernel
from agent_run.model import Execution, Commit, Requirements, ParentIssue, Issue
from agent_run.core.operation_contract import DeliveryFacts
from support.core_operations import CoreOperations, ReadOnlyPorts
import json, sys
k = Kernel(Path(sys.argv[1]),run_id=sys.argv[5])
wire = json.loads(sys.argv[4])
parent = wire['parent']; parent['sub_issue_numbers'] = tuple(parent['sub_issue_numbers'])
source = Requirements(ParentIssue(**parent), tuple(Issue(**dict(item, labels=frozenset(item['labels']), blocked_by=())) for item in wire['issues']))
ports = ReadOnlyPorts(source, 'refs/heads/ticket-2', 'refs/heads/run', DeliveryFacts(Commit('base','base-tree'), None))
step = CoreOperations(k, Execution('offline-run',sys.argv[2],int(sys.argv[3])), lambda binding: ports).once(agent_capacity=0)
print(json.dumps({'step':step.kind,'status':k.status('ticket:2')}))
"""
    resumed = subprocess.run([sys.executable, "-c", consume_command, str(path), replacement.owner, str(replacement.epoch),
                              json.dumps(asdict(ports.requirements), default=list), kernel.storage.run_id],
                             env=environment, text=True, capture_output=True, check=True, timeout=15)
    assert json.loads(resumed.stdout)["step"] == "progress"
    assert json.loads(resumed.stdout)["status"]["phase"] == "candidate"
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


def test_unknown_successful_pr_write_is_read_back_without_a_second_write(tmp_path: Path, domain_scenario) -> None:
    scenario = domain_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("pr_write")
    waiting = scenario.run()
    assert waiting.kind == "waiting" and waiting.reason == "github_convergence"
    assert waiting.retry_at == 105.25
    original = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert original["operation"] == "pr_write" and original["status"] == "sent"
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    now[0] = waiting.retry_at
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert scenario.publication_count == 1


@pytest.mark.parametrize("already_written", [False, True])
def test_old_pr_intent_is_settled_before_reintegration_after_target_append(tmp_path: Path, monkeypatch, already_written: bool, domain_scenario) -> None:
    scenario = domain_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, "pr_write", sent=True)
    effect = held.effect
    if already_written:
        scenario.port.execute(effect, held.authorize)
    scenario.port.external_append(scenario.delivery.target_ref, {"external.txt": "Other actor's retained work\n"}, "external")
    scenario.step()
    if already_written:
        assert scenario.step().kind == "progress"
    status = scenario.kernel.status(scenario.delivery.key)
    assert status["phase"] == "integration"
    assert status["pending_write"] is None
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == int(already_written)
    assert scenario.run().kind == "complete"
    assert scenario.port.git.run("show", f"{scenario.delivery.target_ref}:external.txt").stdout == "Other actor's retained work\n"


def test_human_continuation_replaces_thread_but_keeps_attempt_budget_and_original_context(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel, ports = native_agent_fixture(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    operations = CoreOperations(kernel, token, lambda binding: ports)
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None
    operations.runner.authorize_agent(first)
    kernel.bind_thread(first, "reported-first")
    kernel.receive(first, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Provide the external decision."]}))
    assert operations.once().kind == "paused"
    kernel.control("resume", "human-resume", subject="ticket:2", message="Decision: retain original formatting.\r\n", new_thread=True)
    second = operations.until(lambda step: step.call is not None).call
    assert second is not None
    assert second.attempt_id == first.attempt_id
    assert second.thread_id != first.thread_id
    assert second.mode == "fresh"
    assert second.context["human_responses"] == ["Decision: retain original formatting.\r\n"]
    assert "human_responses" not in first.context
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1
    first_raw = wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Provide the external decision."]})
    kernel.receive(first, first_raw)
    assert operations.runner.state().active == (second.lease_id,)
    assert second.subject == "ticket:2"
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.rows("SELECT id,subject,invocation FROM leases")] == [
            {"id": second.lease_id, "subject": second.subject, "invocation": second.invocation_id}]


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_real_git_conflict_returns_to_integration_development_without_internal_pr(tmp_path: Path, kind: DeliveryKind, domain_scenario) -> None:
    file_name = "ticket.txt" if kind == DeliveryKind.FINAL_RUN else "shared.txt"
    edits = ({file_name: "Resolved complete content\n"},) if kind == DeliveryKind.FINAL_RUN else (
        {file_name: "Candidate content\n"}, {file_name: "Resolved complete content\n"})
    scenario = domain_scenario(tmp_path / "scenario", kind, edits=edits)
    historical_writes = scenario.port.writes()
    if kind != DeliveryKind.FINAL_RUN:
        for _ in range(30):
            if scenario.kernel.status(scenario.delivery.key)["phase"] == "integration":
                break
            scenario.step()
        else:
            raise AssertionError("initial review did not finish")
    scenario.port.external_append(scenario.delivery.target_ref, {file_name: "External content\n"}, "conflict")
    assert scenario.run(approve=True).kind == "complete"
    integration = [call for call in scenario.role_calls if call["role"] == "development" and call["purpose"] == "integration"]
    assert len(integration) == 1
    if kind == DeliveryKind.TICKET:
        assert scenario.review_count == 1
        assert [call["role"] for call in scenario.role_calls if call["purpose"] == "integration"] == ["development", "publication"]
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.writes()) == 1
    if kind == DeliveryKind.FINAL_RUN:
        assert scenario.port.writes()[:len(historical_writes)] == historical_writes
        assert sum(write["operation"] == "pr_write" for write in historical_writes) == 1
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    assert scenario.port.git.run("show", f"{target.sha}:{file_name}").stdout == "Resolved complete content\n"


def test_successor_git_write_rechecks_stop_after_previous_git_operation(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, "candidate", sent=True)
    effect = held.effect
    guards = 0
    admissions = []

    def prepare() -> FinalAdmission:
        nonlocal guards
        guards += 1
        if guards == 2:
            scenario.kernel.control("stop", "stop-after-git-add")
        final_admission = held.authorize.prepare()
        def admitted() -> None:
            final_admission()
            admissions.append(effect.effect_id)
        return admitted

    before = scenario.port.git.commit(scenario.delivery.base.sha)
    with pytest.raises(KernelError, match="executor_fenced|operation_cancelled"):
        scenario.port.execute(effect, WriteAuthority(prepare))
    assert guards == 2
    assert admissions == [effect.effect_id]
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=Path(scenario.delivery.checkout)).stdout.strip() == before.sha
    assert not any(write["operation"] == "candidate" for write in scenario.port.writes())


def test_fifo_and_stale_return_release_cannot_steal_new_lease(tmp_path: Path) -> None:
    original = ticket_spec()
    deliveries = tuple(replace(original.deliveries[0], key=f"ticket:{number}", issue=number) for number in (2, 3, 4))
    kernel, ports = native_agent_fixture(tmp_path / "kernel.sqlite3", replace(original, deliveries=deliveries,
                           policy=replace(original.policy, concurrency=1)))
    token = kernel.claim_executor("executor")
    operations = CoreOperations(kernel, token, lambda binding: ports)
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None and first.subject == "ticket:2"
    operations.runner.authorize_agent(first)
    before = {subject: kernel.status(subject)["budgets"] for subject in ("ticket:3", "ticket:4")}
    for subject in ("ticket:3", "ticket:4"):
        waiting = operations.until(lambda step: step.kind == "needs_agent", agent_capacity=0)
        assert waiting.subject == subject and waiting.call is None
    with kernel.storage.transaction(write=False) as tx:
        assert [row[0] for row in tx.rows("SELECT subject FROM work_queue ORDER BY ready")] == ["ticket:3", "ticket:4"]
    assert {subject: kernel.status(subject)["budgets"] for subject in before} == before
    raw = wire_json({"result_kind": "development", "summary": "Retained", "human_blockers": None})
    kernel.receive(first, raw)
    assert operations.once(agent_capacity=0).kind == "progress"
    second = operations.until(lambda step: step.call is not None).call
    assert second is not None and second.subject == "ticket:3"
    kernel.receive(first, raw)
    assert operations.runner.state().active == (second.lease_id,)
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.rows("SELECT id,subject,invocation FROM leases")] == [
            {"id": second.lease_id, "subject": second.subject, "invocation": second.invocation_id}]
    assert operations.runner.state().concurrency == 1
    assert kernel.status("ticket:4")["pending_attempt"] is None
    assert kernel.status("ticket:4")["budgets"] == before["ticket:4"]
    with kernel.storage.transaction(write=False) as tx:
        assert [row[0] for row in tx.rows("SELECT subject FROM work_queue ORDER BY ready")] == ["ticket:4"]
    operations.runner.authorize_agent(second)
    kernel.receive(second, raw)
    third = operations.until(lambda step: step.call is not None).call
    assert third is not None and third.subject == "ticket:4"
    assert operations.runner.state().active == (third.lease_id,)


def test_final_approval_check_timeout_has_a_finite_action_and_same_grant_resume(tmp_path: Path, domain_scenario) -> None:
    scenario = domain_scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    scenario.kernel.control("approve", "approve-exact", subject=scenario.delivery.key, world=scenario.world())
    with pytest.raises(KernelError, match="final_approval_noncancellable"):
        scenario.kernel.control("stop", "cancel-final")
    scenario.checks[:] = ["pending"]
    scenario.kernel.clock = lambda: 100.0
    assert scenario.step().kind == "waiting"
    scenario.kernel.clock = lambda: 2801.0
    assert scenario.step().kind == "paused"
    scenario.checks[:] = ["none"]
    scenario.kernel.control("resume", "resume-final", subject=scenario.delivery.key)
    with pytest.raises(KernelError, match="final_approval_noncancellable"):
        scenario.kernel.control("stop", "cancel-resumed-final")
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 1


def test_parent_ci_target_append_changes_current_work_to_integration_and_retains_ci_accounting(tmp_path: Path, domain_scenario) -> None:
    scenario = domain_scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN,
                        reviews=("pass", "pass", "fail", "pass"), checks=("fail", "none"))
    for _ in range(100):
        status = scenario.kernel.status(scenario.delivery.key)
        if scenario.development_count == 1 and scenario.review_count == 2 and status["phase"] == "ready_publication":
            break
        scenario.step()
    else:
        raise AssertionError("CI repair review did not reach publication boundary")
    original_ci = scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]
    assert (original_ci["developments_used"], original_ci["reviews_used"]) == (1, 1)
    original_ci_events = [event for event in scenario.kernel.history(scenario.delivery.key) if event["kind"] == "ci_failed"]
    assert len(original_ci_events) == 1
    scenario.port.external_append(scenario.delivery.target_ref, {"external.txt": "Default branch advanced\n"}, "ci-then-append")
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    failed_review = None
    for _ in range(80):
        step = operations.once()
        if step.call is not None and step.call.role.value == "development" and step.call.purpose.value == "integration":
            call = step.call
            break
        if step.call is not None:
            if step.call.role.value == "review" and step.call.purpose.value == "integration":
                failed_review = step.call
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
        assert step.kind not in {"paused", "complete", "approval", "waiting"}
    else:
        raise AssertionError("new integration Findings did not return to Development")
    assert call.context["purpose"] == "integration"
    # Purpose selects the integration budget; the latest actual Review Findings
    # select the repair input, not the older Git integration responsibility.
    assert call.context["repair_source"] == "acceptance"
    assert failed_review is not None
    with scenario.kernel.storage.transaction(write=False) as tx:
        previous = tx.one("SELECT result_id,subject,generation,outcome FROM attempts WHERE id=?", (failed_review.attempt_id,))
        findings = json.loads(tx.get(previous["result_id"], call.subject))
        job = tx.one("SELECT last_failure FROM subjects WHERE id=?", (call.subject,))
        assert previous["outcome"] == "fail" and job["last_failure"] == previous["result_id"]
        assert (previous["subject"], previous["generation"]) == (call.subject, call.context["generation"])
    assert call.context["acceptance_artifact"] == findings == review_result("fail")
    assert "ci_failure" not in call.context
    budgets = scenario.kernel.status(scenario.delivery.key)["budgets"]
    assert budgets["ci"] == original_ci
    assert budgets["integration"]["developments_used"] == 0
    assert [event for event in scenario.kernel.history(scenario.delivery.key) if event["kind"] == "ci_failed"] == original_ci_events


    scripted_return(scenario.kernel, operations.runner, call, scenario.agent)
    budgets = scenario.kernel.status(scenario.delivery.key)["budgets"]
    assert budgets["integration"]["developments_used"] == 1
    assert budgets["ci"] == original_ci
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            assert step.call.role.value == "review" and step.call.purpose.value == "integration"
            rereview = step.call
            break
        assert step.kind not in {"paused", "complete", "approval", "waiting"}
    else:
        raise AssertionError("Integration Findings repair did not return to its Reviewer")
    facts = rereview.context["review_context"]["facts"]
    assert rereview.subject == failed_review.subject == call.subject
    assert rereview.context["generation"] == failed_review.context["generation"] == call.context["generation"]
    assert facts["previous_acceptance_artifact"] == findings
    assert facts["previous_review_identity"] == failed_review.context["review_context"]["facts"]["current_review_identity"]
    assert scenario.kernel.status(call.subject)["budgets"]["ci"] == original_ci
    assert [event for event in scenario.kernel.history(call.subject) if event["kind"] == "ci_failed"] == original_ci_events
