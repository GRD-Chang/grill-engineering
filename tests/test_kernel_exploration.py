"""Behavioral probes for the isolated replacement kernel, never legacy fixtures."""

from pathlib import Path
from dataclasses import asdict, replace
import json
import os
import subprocess
import sys

import pytest

from agent_run.model import DeliveryKind, Policy, Role
from agent_run.core.kernel import Kernel
from agent_run.contracts.write_authority import FinalAdmission, WriteAuthority
from agent_run.errors import KernelError
from support.offline import Scenario, review_result
from support.review_facts import requirement_review
from agent_run.model import wire_json
from support.core_operations import CoreOperations, native_agent_fixture
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


def test_parent_cannot_skip_review_when_its_budget_is_exhausted(tmp_path: Path) -> None:
    # Ticket fallback evidence and role order are asserted by the Publication
    # prompt test. Parent budget exhaustion must stop before any remote write.
    with requirement_review(tmp_path, kind=DeliveryKind.PARENT_ONLY, policy=Policy(parent_rounds=1)) as (kernel, operations):
        development = operations.until(lambda step: step.call is not None).call
        operations.runner.authorize_agent(development)
        kernel.receive(development, wire_json({"result_kind": "development", "summary": "Changed", "human_blockers": None}))
        review = operations.until(lambda step: step.call is not None).call
        operations.runner.authorize_agent(review)
        kernel.receive(review, wire_json(review_result("fail")))
        operations.until(lambda step: step.kind == "paused")
        assert "review_budget_exhausted" in kernel.status(review.subject)["gates"]


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_ci_uses_separate_cumulative_development_and_parent_review(kind: DeliveryKind, request) -> None:
    scenario = request.getfixturevalue("domain_ticket_before_pr" if kind == DeliveryKind.TICKET else "domain_parent_at_approval")
    if kind == DeliveryKind.PARENT_ONLY:
        # A granted merge must still hand a newly failed check back to CI.
        assert scenario.run().kind == "approval"
        kernel, subject = scenario.kernel, scenario.delivery.key
        assert kernel.control("approve", "original-grant", subject=subject, world=scenario.world()) == "pending"
        scenario.checks[:] = ["fail", "none"]
        assert scenario.step().kind == "progress"
        assert kernel.status(subject)["phase"] == "checks"
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT status FROM actions WHERE id='original-grant'")[0] == "updating"
            assert tx.one("SELECT final_action FROM runs")[0] is None
            assert tx.one("SELECT grant_id FROM subjects WHERE id=?", (subject,))[0] is None
            assert not tx.rows("SELECT id FROM effects WHERE operation='merge'")
    else:
        scenario.checks[:] = ["fail", "none"]
    assert scenario.run(approve=True).kind == "complete"
    if kind == DeliveryKind.PARENT_ONLY:
        pr = scenario.port.pr_by_number(scenario.kernel.status(scenario.delivery.key)["pr_number"])
        assert pr.body.startswith("Parent Issue: #1\nDelivery Type: Parent-only\n\n")
        assert len(scenario.port.git.parents(scenario.port.remote_commit(scenario.delivery.target_ref).sha)) == 2
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




def test_old_pr_intent_is_settled_before_reintegration_after_target_append(monkeypatch, domain_ticket_before_pr) -> None:
    scenario = domain_ticket_before_pr
    held = advance_to_effect(scenario, monkeypatch, "pr_write", sent=True)
    effect = held.effect
    scenario.port.execute(effect, held.authorize)
    scenario.port.external_append(scenario.delivery.target_ref, {"external.txt": "Other actor's retained work\n"}, "external")
    scenario.step()
    assert scenario.step().kind == "progress"
    status = scenario.kernel.status(scenario.delivery.key)
    assert status["phase"] == "integration"
    assert status["pending_write"] is None
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
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


def test_real_git_conflict_returns_to_parent_development_then_repair_pr(tmp_path: Path, domain_scenario, monkeypatch) -> None:
    scenario = domain_scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN,
        edits=({"ticket.txt": "Resolved complete content\n"},))
    original_agent, threads = scenario.agent, {}

    def agent(call):
        if call.role == Role.DEVELOPMENT:
            threads["development"] = call.thread_id
        elif call.role == Role.PUBLICATION:
            assert call.thread_id != threads["development"]
            assert call.mode == "fresh" and call.reported_thread is None
            threads["publication"] = call.thread_id
        return original_agent(call)

    monkeypatch.setattr(scenario, "agent", agent)
    historical_writes = scenario.port.writes()
    scenario.port.external_append(scenario.delivery.target_ref, {"ticket.txt": "External content\n"}, "conflict")
    assert scenario.run(approve=True).kind == "complete"
    integration = [call for call in scenario.role_calls if call["role"] == "development" and call["purpose"] == "integration"]
    assert len(integration) == 1 and set(threads) == {"development", "publication"}
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.writes()) == 2
    assert scenario.port.writes()[:len(historical_writes)] == historical_writes
    assert sum(write["operation"] == "pr_write" for write in historical_writes) == 1
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    assert scenario.port.git.run("show", f"{target.sha}:ticket.txt").stdout == "Resolved complete content\n"


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


def test_final_approval_check_timeout_has_a_finite_action_and_same_grant_resume(parent_at_approval) -> None:
    scenario = parent_at_approval
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
