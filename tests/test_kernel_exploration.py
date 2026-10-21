"""Behavioral probes for the isolated replacement kernel, never legacy fixtures."""

from pathlib import Path
from dataclasses import replace
import json
import subprocess
import sys
import sqlite3

import pytest

from agent_run_kernel import Checks, Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec, World
from agent_run_kernel.offline import Scenario, review_result
from agent_run_kernel.model import Effect, EffectResult, wire_json


def ticket_spec() -> RunSpec:
    base = Commit("base", "base-tree")
    return RunSpec(
        "offline-run", "example/repository", 1, "parent-v1", "graph-v1",
        (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                  "refs/heads/ticket-2", "refs/heads/run"),),
        Policy(ticket_rounds=1),
    )


def test_return_is_durable_before_consumption_and_resume_does_not_charge_twice(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel = Kernel.create(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    first = kernel.advance("ticket:2", token, world)
    assert first.kind == "agent"
    assert first.call is not None
    assert first.call.role.value == "development"
    kernel.receive(first.call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')

    reopened = Kernel(path)
    observed = reopened.status("ticket:2")
    assert observed["pending_return"] is True
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    assert observed["phase"] == "development"
    consumed = reopened.advance("ticket:2", token, world, allow_agent=False)
    assert consumed.kind == "progress"
    assert reopened.status("ticket:2")["phase"] == "candidate"
    assert reopened.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize(("kind", "expected_roles", "development_purpose"), [
    (DeliveryKind.TICKET, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.PARENT_ONLY, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.FINAL_RUN, ["review", "development", "review", "publication"], "integration"),
])
def test_three_deliveries_complete_with_required_review_and_exact_git_history(
    tmp_path: Path, kind: DeliveryKind, expected_roles: list[str], development_purpose: str,
) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, reviews=("fail", "pass"))
    original_target = scenario.port.git.commit(scenario.delivery.target_ref)
    boundary = scenario.run()
    assert boundary.kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    if kind != DeliveryKind.TICKET:
        assert scenario.port.git.commit(scenario.delivery.target_ref) == original_target
        scenario.kernel.control("approve", "exact-approval", subject=scenario.delivery.key, world=scenario.world())
        boundary = scenario.run()
    assert boundary.kind == "complete"
    assert [call["role"] for call in scenario.role_calls] == expected_roles
    assert {call["purpose"] for call in scenario.role_calls if call["role"] == "development"} == {development_purpose}
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "completion_comment" for write in scenario.port.writes()) == 1
    target = scenario.port.git.commit(scenario.delivery.target_ref)
    assert len(scenario.port.git.parents(target.sha)) == (1 if kind == DeliveryKind.TICKET else 2)
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    assert scenario.port.git.run("ls-tree", "-r", "--name-only", target.sha).stdout.find("validation.tmp") == -1
    assert not list((scenario.port.root / "work").glob("review-*"))


def test_ticket_final_development_is_explicit_fallback_but_parent_cannot_skip_review(tmp_path: Path) -> None:
    ticket = Scenario(tmp_path / "ticket", DeliveryKind.TICKET, reviews=("fail",), policy=Policy(ticket_rounds=1))
    assert ticket.run().kind == "complete"
    assert [call["role"] for call in ticket.role_calls] == ["development", "review", "development", "publication"]
    assert ticket.kernel.status(ticket.delivery.key)["gate_mode"] == "deterministic_fallback"
    parent = Scenario(tmp_path / "parent", DeliveryKind.PARENT_ONLY, reviews=("fail",), policy=Policy(parent_rounds=1))
    assert parent.run().kind == "paused"
    assert "review_budget_exhausted" in parent.kernel.status(parent.delivery.key)["gates"]
    assert not any(write["operation"] == "pr_write" for write in parent.port.writes())


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_ci_uses_separate_cumulative_development_and_parent_review(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, checks=("fail", "none"))
    assert scenario.run(approve=True).kind == "complete"
    ci = [call for call in scenario.role_calls if call["purpose"] == "ci"]
    assert [call["role"] for call in ci if call["role"] != "publication"] == (
        ["development"] if kind == DeliveryKind.TICKET else ["development", "review"])
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 2
    if kind == DeliveryKind.FINAL_RUN:
        assert {write["subject"] for write in scenario.port.writes()} == {scenario.delivery.key}


def test_stop_late_raw_is_settlement_only_and_independent_process_can_read_it(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel = Kernel.create(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    step = kernel.advance("ticket:2", token, world)
    assert step.call is not None
    kernel.control("stop", "stop-original")
    kernel.receive(step.call, b'{"result_kind":"development","summary":"retained","human_blockers":null}')
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.advance("ticket:2", token, world)
    command = "from pathlib import Path; from agent_run_kernel import Kernel; import json,sys; print(json.dumps(Kernel(Path(sys.argv[1])).status('ticket:2')))"
    process = subprocess.run([sys.executable, "-c", command, str(path)], text=True, capture_output=True, check=True, timeout=15)
    observed = json.loads(process.stdout)
    assert observed["stopped"] is True
    assert observed["pending_return"] is True
    assert observed["phase"] == "development"
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    kernel.confirm_executor_exit(token)
    kernel.control("resume", "resume-original")
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.advance("ticket:2", token, world)
    replacement = kernel.claim_executor("executor-two")
    consume_command = "from pathlib import Path; from agent_run_kernel import Kernel,Execution,World,Commit; import json,sys; k=Kernel(Path(sys.argv[1])); step=k.advance('ticket:2',Execution('offline-run',sys.argv[2],int(sys.argv[3])),World('parent-v1','graph-v1','ticket-v1',Commit('base','base-tree')),allow_agent=False); print(json.dumps({'step':step.kind,'status':k.status('ticket:2')}))"
    resumed = subprocess.run([sys.executable, "-c", consume_command, str(path), replacement.owner, str(replacement.epoch)], text=True, capture_output=True, check=True, timeout=15)
    assert json.loads(resumed.stdout)["step"] == "progress"
    assert json.loads(resumed.stdout)["status"]["phase"] == "candidate"
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


def test_unknown_successful_pr_write_is_read_back_without_a_second_write(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    scenario.port.lose_response.add("pr_write")
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert scenario.publication_count == 1


def advance_to_effect(scenario: Scenario, operation: str, *, developments: int = 0) -> Effect:
    for _ in range(150):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == operation and scenario.development_count >= developments:
            return step.effect
        scenario.handle(step)
        assert step.kind not in {"paused", "complete", "approval", "waiting"}
    raise AssertionError("requested effect never became ready")


@pytest.mark.parametrize("disposition", ["prior", "target", "third", "unknown"])
def test_unknown_write_branches_preserve_intent_and_resume_only_original_outcome(tmp_path: Path, disposition: str) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    effect = advance_to_effect(scenario, "pr_write")
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    if disposition != "prior":
        scenario.port.execute(effect)
    actual = scenario.port.reconcile(effect) if disposition in {"prior", "target"} else EffectResult(disposition)
    scenario.kernel.reconcile(effect, actual, scenario.execution, scenario.world())
    if disposition in {"third", "unknown"}:
        assert scenario.kernel.status(scenario.delivery.key)["pending_write"]["status"] == "sent"
        assert scenario.kernel.status(scenario.delivery.key)["holds_integration"]
        assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
        scenario.kernel.reconcile(effect, scenario.port.reconcile(effect), scenario.execution, scenario.world())
        scenario.kernel.control("resume", "resume-exact-write", subject=scenario.delivery.key)
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1


@pytest.mark.parametrize("already_written", [False, True])
def test_old_pr_intent_is_settled_before_reintegration_after_target_append(tmp_path: Path, already_written: bool) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    effect = advance_to_effect(scenario, "pr_write")
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    if already_written:
        scenario.port.execute(effect)
    scenario.port.external_append(scenario.delivery.target_ref, {"external.txt": "Other actor's retained work\n"}, "external")
    scenario.kernel.reconcile(effect, scenario.port.reconcile(effect), scenario.execution, scenario.world())
    if already_written:
        assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "progress"
    status = scenario.kernel.status(scenario.delivery.key)
    assert status["phase"] == "integration"
    assert status["pending_write"] is None
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == int(already_written)
    assert scenario.run().kind == "complete"
    assert scenario.port.git.run("show", f"{scenario.delivery.target_ref}:external.txt").stdout == "Other actor's retained work\n"


def test_human_continuation_replaces_thread_but_keeps_attempt_budget_and_original_context(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel = Kernel.create(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    first = kernel.advance("ticket:2", token, world).call
    assert first is not None
    kernel.bind_thread(first, "reported-first")
    kernel.receive(first, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Provide the external decision."]}))
    assert kernel.advance("ticket:2", token, world).kind == "paused"
    kernel.control("resume", "human-resume", subject="ticket:2", message="Decision: retain original formatting.\r\n", new_thread=True)
    second = kernel.advance("ticket:2", token, world).call
    assert second is not None
    assert second.attempt_id == first.attempt_id
    assert second.thread_id != first.thread_id
    assert second.mode == "fresh"
    assert second.context["human_responses"] == ["Decision: retain original formatting.\r\n"]
    assert "human_responses" not in first.context
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1
    first_raw = wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Provide the external decision."]})
    kernel.receive(first, first_raw)
    assert kernel.scheduling()["active"] == [{"id": second.lease_id, "subject": "ticket:2", "invocation": second.invocation_id}]


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_real_git_conflict_returns_to_integration_development_without_internal_pr(tmp_path: Path, kind: DeliveryKind) -> None:
    file_name = "ticket.txt" if kind == DeliveryKind.FINAL_RUN else "shared.txt"
    edits = ({file_name: "Resolved complete content\n"},) if kind == DeliveryKind.FINAL_RUN else (
        {file_name: "Candidate content\n"}, {file_name: "Resolved complete content\n"})
    scenario = Scenario(tmp_path / "scenario", kind, edits=edits)
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
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert scenario.port.git.run("show", f"{scenario.delivery.target_ref}:{file_name}").stdout == "Resolved complete content\n"


def test_successor_git_write_rechecks_stop_after_previous_git_operation(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    effect = advance_to_effect(scenario, "candidate")
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    guards = 0

    def authorize() -> None:
        nonlocal guards
        guards += 1
        if guards == 2:
            scenario.kernel.control("stop", "stop-after-git-add")
        scenario.kernel.guard_effect(effect, scenario.execution, scenario.world())

    before = scenario.port.git.commit(scenario.delivery.base.sha)
    with pytest.raises(KernelError, match="executor_fenced|operation_cancelled"):
        scenario.port.execute(effect, authorize)
    assert guards == 2
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=Path(scenario.delivery.checkout)).stdout.strip() == before.sha
    assert not any(write["operation"] == "candidate" for write in scenario.port.writes())


def test_fifo_and_stale_return_release_cannot_steal_new_lease(tmp_path: Path) -> None:
    original = ticket_spec()
    deliveries = tuple(replace(original.deliveries[0], key=f"ticket:{number}", issue=number) for number in (2, 3, 4))
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", replace(original, deliveries=deliveries))
    token = kernel.claim_executor("executor")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    first = kernel.advance("ticket:2", token, world).call
    assert first is not None
    assert kernel.advance("ticket:3", token, world).reason == "agent_capacity"
    assert kernel.advance("ticket:4", token, world).reason == "agent_capacity"
    assert kernel.scheduling()["work_fifo"] == ["ticket:3", "ticket:4"]
    raw = wire_json({"result_kind": "development", "summary": "Retained", "human_blockers": None})
    kernel.receive(first, raw)
    assert kernel.advance("ticket:2", token, world, allow_agent=False).kind == "progress"
    second = kernel.advance("ticket:3", token, world).call
    assert second is not None
    kernel.receive(first, raw)
    assert kernel.scheduling()["active"] == [{"id": second.lease_id, "subject": "ticket:3", "invocation": second.invocation_id}]
    assert kernel.advance("ticket:4", token, world).reason == "agent_capacity"
    kernel.receive(second, raw)
    third = kernel.advance("ticket:4", token, world).call
    assert third is not None
    assert len(kernel.scheduling()["active"]) == 1


def test_full_parent_resume_renews_only_exhausted_development_allowance(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN, reviews=("fail", "pass"),
                        policy=Policy(run_repairs=1), edits=({"shared.txt": "Candidate\n"}, {"shared.txt": "Resolved\n"}))
    for _ in range(60):
        if scenario.development_count == 1 and scenario.kernel.status(scenario.delivery.key)["phase"] == "integration":
            break
        scenario.step()
    else:
        raise AssertionError("first repair Candidate was not created")
    scenario.port.external_append(scenario.delivery.target_ref, {"shared.txt": "External\n"}, "selective-renew")
    assert scenario.run().kind == "paused"
    before = scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"]
    assert (before["developments_used"], before["reviews_used"]) == (1, 1)
    scenario.kernel.control("resume", "selective-renew", subject=scenario.delivery.key)
    after = scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"]
    assert (after["developments_used"], after["reviews_used"], after["window"]) == (0, 1, 2)
    assert scenario.run(approve=True).kind == "complete"


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_ci_renewal_keeps_requirement_budget_and_cumulative_reviewer_count(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, checks=("fail", "fail", "none"), policy=Policy(ticket_ci_developments=1, parent_ci_developments=1, ticket_ci_review=True))
    assert scenario.run().kind == "paused"
    before = scenario.kernel.status(scenario.delivery.key)["budgets"]
    assert before["ci"]["developments_used"] == 1
    assert before["ci"]["reviews_used"] == 1
    scenario.kernel.control("resume", "ci-renew-once", subject=scenario.delivery.key)
    assert scenario.kernel.control("resume", "ci-renew-once", subject=scenario.delivery.key) == "applied"
    renewed = scenario.kernel.status(scenario.delivery.key)["budgets"]
    assert (renewed["ci"]["window"], renewed["ci"]["developments_used"], renewed["ci"]["reviews_used"]) == (2, 0, 1)
    assert renewed["requirements"] == before["requirements"]
    assert scenario.run(approve=True).kind == "complete"
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]["reviews_used"] == 2


def test_final_approval_check_timeout_has_a_finite_action_and_same_grant_resume(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
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


def test_failed_approval_rolls_back_action_material_grant_and_business_facts(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    world = scenario.world()
    assert world.pr is not None
    changed = replace(world, checks=Checks(world.pr.number, world.pr.head.sha, "pass", (("new-required-check", "pass"),)))

    def independent_snapshot() -> dict[str, list[tuple[object, ...]]]:
        with sqlite3.connect(f"file:{scenario.kernel.storage.path}?mode=ro", uri=True) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("actions", "grants", "material", "history", "subjects", "budgets")}

    before = independent_snapshot()
    writes = scenario.port.writes()
    with pytest.raises(KernelError, match="approval_checks_changed"):
        scenario.kernel.control("approve", "atomic-approval", subject=scenario.delivery.key, world=changed)
    assert independent_snapshot() == before
    assert scenario.port.writes() == writes
    assert scenario.kernel.control("approve", "atomic-approval", subject=scenario.delivery.key, world=world) == "pending"


def test_parent_ci_target_append_changes_current_work_to_integration_and_retains_ci_accounting(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN,
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
    for _ in range(80):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.call is not None and step.call.role.value == "development" and step.call.purpose.value == "integration":
            call = step.call
            break
        scenario.handle(step)
        assert step.kind not in {"paused", "complete", "approval", "waiting"}
    else:
        raise AssertionError("new integration Findings did not return to Development")
    assert call.context["purpose"] == "integration"
    assert call.context["repair_source"] == "integration"
    assert "ci_failure" not in call.context
    budgets = scenario.kernel.status(scenario.delivery.key)["budgets"]
    assert budgets["ci"] == original_ci
    assert budgets["integration"]["developments_used"] == 1
    assert [event for event in scenario.kernel.history(scenario.delivery.key) if event["kind"] == "ci_failed"] == original_ci_events

