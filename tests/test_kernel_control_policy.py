"""Explicit checkpoint policy and stopped-Run feedback keep their real owners."""

from pathlib import Path

import pytest

from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec, World
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import EffectResult, Execution, HostLaunch, HostObservation, wire_json
from support.offline import Scenario
from support.core_operations import CoreOperations, ReadOnlyPorts, native_agent_fixture, final_scenario


def managed(kernel: Kernel, root: Path) -> tuple[HostLaunch, Execution]:
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=root / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    return request, Execution(request.run_id, request.owner, request.epoch)


def tickets(root: Path) -> tuple[Kernel, ReadOnlyPorts]:
    base = Commit("base", "tree")
    jobs = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "v1", base,
                          f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    kernel, ports = native_agent_fixture(root / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", jobs,
                           Policy(ticket_rounds=1, concurrency=2, development_deadline_seconds=7.0),
                           language="en", repository_root=str(root / "repository")))
    class TicketPorts(ReadOnlyPorts):
        def delivery(self, request):
            assert request.source_ref in {item.source_ref for item in jobs}
            assert request.target_ref == 'refs/heads/run'
            return self.facts

        def execute(self, effect, authorize):
            assert effect.operation == 'candidate'
            authorize()
            return EffectResult('failure', base, raw={'head': 'base', 'reason': 'candidate_integrity'})

    return kernel, TicketPorts(ports.requirements, ports.source_ref, ports.target_ref, ports.facts)


@pytest.mark.parametrize(("kind", "option", "purpose", "limits"), [
    (DeliveryKind.PARENT_ONLY, "parent_only_paired_rounds", "requirements", (2, 2)),
    (DeliveryKind.FINAL_RUN, "run_repair_rounds", "integration", (2, 3)),
])
def test_parent_checkpoint_freezes_new_limits_without_rewriting_old_invocations(
    tmp_path: Path, kind: DeliveryKind, option: str, purpose: str, limits: tuple[int, int],
) -> None:
    factory = final_scenario if kind == DeliveryKind.FINAL_RUN else lambda root, **kwargs: Scenario(root, kind, **kwargs)
    scenario = factory(tmp_path / "delivery", reviews=("fail",),
                       policy=Policy(parent_rounds=1, run_repairs=1, development_deadline_seconds=7.0))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request, scenario.execution = managed(kernel, tmp_path)
    assert scenario.run().kind == "paused"
    before = kernel.status(subject)
    assert "review_budget_exhausted" in before["gates"]
    assert before["pending_attempt"] is None
    with kernel.storage.transaction(write=False) as tx:
        old_invocations = [dict(row) for row in tx.all("SELECT * FROM invocations ORDER BY id")]
        old_budget = dict(tx.one("SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1", (subject, purpose)))
    overrides = {option: 2, "development_deadline": "13s", "review_deadline": "17s", "publication_deadline": "19s"}
    assert kernel.control_host(request, "resume", "new-window", subject=subject, policy_overrides=overrides) == "applied"
    assert kernel.control_host(request, "resume", "new-window", subject=subject, policy_overrides=overrides) == "applied"
    renewed = kernel.status(subject)["budgets"][purpose]
    assert (renewed["window"], renewed["development_limit"], renewed["review_limit"]) == (2, *limits)
    assert (renewed["developments_used"], renewed["reviews_used"]) == (0, 0)
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.all("SELECT * FROM invocations ORDER BY id")] == old_invocations
        assert dict(tx.one("SELECT * FROM budgets WHERE subject=? AND purpose=? AND window=1", (subject, purpose))) == {**old_budget, "current": 0}
    call = CoreOperations(kernel, scenario.execution, scenario.make_ports).until(lambda step: step.call is not None).call
    assert call is not None and call.role == "development" and call.deadline_seconds == 13.0
    assert kernel.active_host_action() == "start" and not kernel.dispatch_host(request)


def test_ticket_policy_window_does_not_change_a_sibling_call_or_its_allowances(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    previous = None
    sibling = None
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            if step.call.subject == "ticket:3":
                assert sibling is None
                sibling = step.call  # Keep the real FIFO lease; do not run or settle it.
            else:
                assert step.call.subject == "ticket:2"
                previous = step.call
                kernel.bind_thread(previous, previous.reported_thread or "ticket-two-thread")
                kernel.receive(previous, b'{"result_kind":"development","summary":"Investigated candidate integrity","human_blockers":null}')
        if "modification_budget_exhausted" in kernel.status("ticket:2")["gates"]:
            break
    else:
        raise AssertionError("The actual candidate failures did not exhaust Ticket 2")
    assert "modification_budget_exhausted" in kernel.status("ticket:2")["gates"]
    if sibling is None:
        sibling = operations.once().call
    assert sibling is not None and sibling.subject == "ticket:3" and sibling.deadline_seconds == 7.0
    sibling_budget = kernel.status("ticket:3")["budgets"]
    with kernel.storage.transaction(write=False) as tx:
        original_policy = tx.one("SELECT policy FROM runs")[0]
    assert kernel.control_host(request, "resume", "ticket-window", subject="ticket:2", policy_overrides={
        "ticket_review_rounds": 3, "development_deadline": "13s", "development_thread_policy": "new-per-attempt",
    }) == "applied"
    resumed = operations.once().call
    assert resumed is not None and resumed.deadline_seconds == 13.0
    assert previous is not None and resumed.attempt_id != previous.attempt_id and resumed.thread_id != previous.thread_id
    assert kernel.status("ticket:3")["budgets"] == sibling_budget
    assert kernel.dispatch_agent(sibling)
    kernel.guard_agent(sibling)
    budget = kernel.status("ticket:2")["budgets"]["requirements"]
    assert (budget["window"], budget["development_limit"], budget["review_limit"], budget["developments_used"]) == (2, 4, 3, 1)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT policy FROM runs")[0] == original_policy
        old = tx.one("SELECT * FROM budgets WHERE subject='ticket:2' AND purpose='requirements' AND window=1")
        assert (old["developments_used"], old["development_limit"], old["review_limit"], old["current"]) == (2, 2, 1, 0)


def test_ordinary_resume_cannot_change_policy_or_consume_its_action(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.agent_unavailable(call, "worker_preparation_failed", "No child process was created")
    before = kernel.status("ticket:2")
    with pytest.raises(KernelError, match="policy_override_boundary"):
        kernel.control_host(request, "resume", "ordinary", subject="ticket:2", policy_overrides={"development_deadline": "13s"})
    assert kernel.status("ticket:2") == before
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE id='ordinary'")
    assert kernel.control_host(request, "resume", "ordinary", subject="ticket:2", message="Fixed the worker setup") == "applied"
    continued = operations.once().call
    assert continued is not None and continued.attempt_id == call.attempt_id and continued.deadline_seconds == 7.0


def test_whole_run_resume_forwards_feedback_only_to_the_stopped_work(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    execution = kernel.claim_executor("original-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    blocked = operations.once().call
    assert blocked is not None
    kernel.receive(blocked, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["An independent product decision"]}))
    assert operations.once(agent_capacity=0).kind == "paused"
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "original-thread")
    process = {"pid": 23, "start_token": "original-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    kernel.control("stop", "whole-stop")
    kernel.agent_exited(first, process, 1, failure="Stopped by the operator")
    kernel.confirm_executor_exit(execution)
    message = "已检查停止原因；继续原工作。\r\n"
    request = kernel.admit_host("whole-resume", kind="resume", command=("native-host",), environment_carrier=tmp_path / "carrier",
                                preflight=lambda: None, message=message, new_thread=True)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "resumed", "/host", "boot", 42, "start",
                                                       usage_ready=True)) == "running"
    resumed_execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, resumed_execution, lambda binding: ports)
    assert kernel.status("ticket:2")["pending_attempt"] == blocked.attempt_id
    assert kernel.status("ticket:2")["gates"]
    resumed = operations.once().call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.thread_id != first.thread_id and resumed.context["human_responses"] == [message]
    assert resumed.context["semantic_continuation"] is True and resumed.deadline_seconds == 7.0
    assert kernel.status("ticket:3")["budgets"]["requirements"]["developments_used"] == 1
    assert not [item for item in kernel.history("ticket:2") if item["kind"] == "human_response"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT resume_action FROM attempts WHERE id=?", (blocked.attempt_id,))[0] is None


def test_local_human_resume_cannot_take_pending_whole_run_feedback(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    execution = kernel.claim_executor("original-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    blocked = operations.once().call
    assert blocked is not None
    kernel.receive(blocked, wire_json({"result_kind": "human_blocker", "summary": None,
                                      "human_blockers": ["An independent product decision"]}))
    assert operations.once(agent_capacity=0).kind == "paused"
    first = operations.once().call
    assert first is not None
    kernel.control("stop", "whole-stop")
    kernel.confirm_executor_exit(execution, (first.invocation_id,))
    request = kernel.admit_host("whole-resume", kind="resume", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                message="Continue the stopped work", new_thread=True)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "resumed", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    resumed_execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, resumed_execution, lambda binding: ports)
    assert kernel.control_host(request, "resume", "local-response", subject="ticket:2",
                               message="Independent product decision is now settled") == "applied"
    allocated = [operations.once().call, operations.once().call]
    assert all(call is not None for call in allocated)
    by_subject = {call.subject: call for call in allocated}
    assert set(by_subject) == {"ticket:2", "ticket:3"}
    local = by_subject["ticket:2"]
    assert local is not None and local.attempt_id == blocked.attempt_id
    assert local.thread_id == blocked.thread_id
    assert local.context["human_responses"] == ["Independent product decision is now settled"]
    stopped = by_subject["ticket:3"]
    assert stopped is not None and stopped.context["human_responses"] == ["Continue the stopped work"]
    assert stopped.attempt_id == first.attempt_id and stopped.thread_id != first.thread_id


def test_managed_revision_opens_a_new_repair_cycle_and_updates_the_original_pr(tmp_path: Path) -> None:
    scenario = final_scenario(tmp_path / "delivery", reviews=("fail", "pass"),
                        policy=Policy(run_repairs=2))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request, scenario.execution = managed(kernel, tmp_path)
    assert scenario.run().kind == "approval"
    before = kernel.status(subject)
    old_budget = before["budgets"]["integration"]
    assert old_budget["developments_used"] == 1
    old_pr = scenario.port.pull_request_by_ref(scenario.kernel.delivery(subject).source_ref, scenario.kernel.delivery(subject).target_ref)
    assert old_pr is not None
    feedback = "\n请补充默认分支兼容性。\r\n"
    assert kernel.control_host(request, "revise", "maintainer-revision", subject=subject,
                               world=scenario.world(), message=feedback) == "applied"
    assert kernel.control_host(request, "revise", "maintainer-revision", subject=subject,
                               world=scenario.world(), message=feedback) == "applied"
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.control_host(request, "revise", "maintainer-revision", subject=subject,
                            world=scenario.world(), message="A different revision")
    current = kernel.status(subject)
    assert current["phase"] == "development" and current["purpose"] == "integration"
    assert current["pr_number"] == old_pr.number and current["generation"] == before["generation"]
    budget = current["budgets"]["integration"]
    assert (budget["window"], budget["developments_used"], budget["reviews_used"]) == (old_budget["window"] + 1, 0, 0)
    call = CoreOperations(kernel, scenario.execution, scenario.make_ports).until(lambda step: step.call is not None).call
    assert call is not None and call.role == "development"
    assert call.context["repair_source"] == "human_revision" and call.context["human_feedback"] == feedback
    kernel.receive(call, scenario.agent(call))
    assert scenario.run().kind == "approval"
    revised_pr = scenario.port.pull_request_by_ref(scenario.kernel.delivery(subject).source_ref, scenario.kernel.delivery(subject).target_ref)
    assert revised_pr is not None and revised_pr.number == old_pr.number and revised_pr.head != old_pr.head
    assert kernel.status(subject)["budgets"]["integration"]["developments_used"] == 1
    with kernel.storage.transaction(write=False) as tx:
        old = tx.one("SELECT * FROM budgets WHERE subject=? AND purpose='integration' AND window=?", (subject, old_budget["window"]))
        assert (old["developments_used"], old["reviews_used"], old["current"]) == (1, old_budget["reviews_used"], 0)


def test_two_accepted_whole_run_responses_reach_the_original_work_in_order(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    first = operations.once().call
    assert first is not None
    for number in (1, 2):
        assert kernel.control_host(request, "stop", f"stop-{number}") == "applied"
        assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
                                                           42, "start", exit_status=0, ended_at=kernel.clock(),
                                                           usage_released=True)) == "exited"
        request = kernel.admit_host(f"resume-{number}", kind="resume", command=("native-host",),
                                    environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                    message=f"Accepted response {number}\r\n", new_thread=number == 1)
        assert kernel.dispatch_host(request)
        assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                           42, "start", usage_ready=True)) == "running"
    repeated = kernel.admit_host("resume-2", kind="resume", command=("native-host",),
                                 environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                 message="Accepted response 2\r\n")
    assert repeated == request and not kernel.dispatch_host(repeated)
    resumed = CoreOperations(kernel, Execution(request.run_id, request.owner, request.epoch), lambda binding: ports).once().call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.context["human_responses"] == ["Accepted response 1\r\n", "Accepted response 2\r\n"]
    assert resumed.thread_id != first.thread_id
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1
    responses = [item["identity"] for item in kernel.history("ticket:2") if item["kind"] == "human_response"]
    assert responses == ["resume-1", "resume-2"]
