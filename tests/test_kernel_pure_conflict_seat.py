"""A real unmerged checkout keeps one seat until resolution or a real pause."""
import json
import os
from pathlib import Path
import sys

import pytest

from agent_run.model import Issue, ParentIssue, Policy, Requirements, Role, wire_json
from agent_run.errors import KernelError
from agent_run.queries.run import read
from agent_run.runtime.execution import execute
from support.core_operations import CoreOperations, scripted_return
from support.offline import GraphScenario


def _source():
    return Requirements(ParentIssue(1, "Parent", "Two independent Tickets", (2, 3)), (
        Issue(2, "A", "Change the shared file", "OPEN", frozenset({"ready-for-agent"})),
        Issue(3, "B", "An independent change", "OPEN", frozenset({"ready-for-agent"})),
    ))


def _conflict_with_waiter(tmp_path, *, lose_reply=False):
    scenario = GraphScenario(tmp_path / "graph", _source(), policy=Policy(concurrency=2))
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    clock = [kernel.clock()]
    if lose_reply:
        kernel.clock = lambda: clock[0]
        scenario.port.lose_response.add("integrate")

    def agent(call):
        if call.role == Role.DEVELOPMENT:
            scenario.edits = ({"README.md": "A and external content resolved\n" if call.purpose == "integration"
                              else "A candidate content\n"},) if call.subject == "ticket:2" else ({"b.txt": "B content\n"},)
        return scenario.agent(call)

    reviews = {}
    for _ in range(100):
        step = operations.once()
        if step.call is not None:
            if step.call.role == Role.REVIEW:
                reviews[step.call.subject] = step.call
            else:
                scripted_return(kernel, operations.runner, step.call, agent)
        if len(reviews) == 2:
            break
    else:
        raise AssertionError("Both normal initial Reviews were not reserved")
    scripted_return(kernel, operations.runner, reviews["ticket:2"], agent)
    target = scenario.port.external_append("refs/heads/run", {"README.md": "External content\n"}, "conflicting-target")
    for _ in range(30):
        step = operations.once()
        if lose_reply and step.retry_at is not None:
            clock[0] = step.retry_at
        state = kernel.status("ticket:2")
        if state["phase"] == "development" and state["purpose"] == "integration":
            break
    else:
        raise AssertionError("Real Git conflict did not return to Integration Development")
    checkout = Path(kernel.delivery("ticket:2").checkout)
    assert scenario.port.git.run("diff", "--name-only", "--diff-filter=U", cwd=checkout).stdout.strip() == "README.md"
    assert scenario.port.git.run("rev-parse", "MERGE_HEAD", cwd=checkout).stdout.strip() == target.sha
    with kernel.storage.transaction(write=False) as tx:
        failure = tx.one("SELECT last_failure FROM subjects WHERE id='ticket:2'")[0]
        receipt = json.loads(tx.get(failure, "ticket:2"))
        assert receipt["disposition"] == "failure" and receipt["raw"]["merge_head"] == target.sha
    scripted_return(kernel, operations.runner, reviews["ticket:3"], agent)
    repair = None
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            assert step.call.subject == "ticket:2" and step.call.purpose == "integration"
            repair = step.call
        if repair is not None and (step.reason == "integration_seat" or kernel.status("ticket:3")["holds_integration"]):
            break
    else:
        raise AssertionError("B did not reach the existing integration queue")
    assert repair is not None
    return scenario, operations, agent, repair, receipt


@pytest.mark.parametrize("lose_reply", [False, True])
def test_pure_conflict_keeps_the_original_seat_while_the_other_ticket_waits(tmp_path, lose_reply):
    scenario, operations, agent, repair, receipt = _conflict_with_waiter(tmp_path, lose_reply=lose_reply)
    kernel = scenario.kernel
    assert kernel.status("ticket:2")["holds_integration"]
    assert not kernel.status("ticket:3")["holds_integration"]
    assert kernel.status("ticket:2")["budgets"]["integration"]["developments_used"] == 0
    assert receipt["raw"]["merge_conflict"] is True
    deadline = repair.deadline_at
    assert operations.runner.state().active == (repair.lease_id,)
    assert repair.context["integration_evidence"]["raw"] == receipt["raw"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT deadline_at FROM invocations WHERE id=?", (repair.invocation_id,))[0] == deadline
    scripted_return(kernel, operations.runner, repair, agent)
    assert kernel.status("ticket:2")["budgets"]["integration"]["developments_used"] == 1
    for _ in range(50):
        if kernel.status("ticket:2")["phase"] == "complete":
            break
        assert not kernel.status("ticket:3")["holds_integration"]
        step = operations.once()
        if step.call is not None:
            scripted_return(kernel, operations.runner, step.call, agent)
    else:
        raise AssertionError("Resolved conflict did not finish its original Ticket")
    # The public projection supplies the real chain consumed by #324 coverage.
    # Normal allocation intentionally exposes no copied Development context.
    page = read(kernel.storage.path, "history", storage=kernel.storage).data["history"]
    events, effects = page["events"], {row["id"]: row for row in page["effects"]}
    allocation, = [row for row in events if row["kind"] == "agent_allocated" and row["identity"] == repair.invocation_id]
    assert allocation["evidence"] is None
    failed, = [row for row in events if row["kind"] == "effect_observed" and row["evidence"] == receipt]
    assert effects[failed["identity"]]["operation"] == "integrate"
    assert effects[failed["identity"]]["generation"] == repair.context["generation"]
    assert effects[failed["identity"]]["candidate_sha"] == repair.context["candidate_sha"]
    prepared, = [row for row in events if row["kind"] == "effect_prepared"
                and effects[row["identity"]]["operation"] == "candidate"
                and row["evidence"].get("summary_attempt") == repair.attempt_id]
    assert prepared["evidence"]["parents"] == [receipt["raw"]["head"], receipt["raw"]["merge_head"]]
    candidate, = [row for row in events if row["kind"] == "effect_consumed" and row["identity"] == prepared["identity"]]
    assert candidate["evidence"]["raw"]["parents"] == prepared["evidence"]["parents"]
    integrated, = [row for row in events if row["kind"] == "effect_consumed"
                  and effects[row["identity"]]["operation"] == "integrate"
                  and effects[row["identity"]]["candidate_sha"] == candidate["evidence"]["commit"]["sha"]]
    assert integrated["evidence"]["disposition"] == "target"
    assert failed["position"] < allocation["position"] < prepared["position"] < candidate["position"] < integrated["position"]
    for _ in range(8):
        operations.once()
        if kernel.status("ticket:3")["holds_integration"]:
            break
    assert kernel.status("ticket:3")["holds_integration"]


def _waiter_gets_seat(scenario, operations):
    for _ in range(12):
        operations.once()
        if scenario.kernel.status("ticket:3")["holds_integration"]:
            return
    raise AssertionError("The released seat did not reach the original queued Ticket")


@pytest.mark.parametrize("boundary", ["stop", "human", "unavailable", "abandon", "candidate_failure"])
def test_definite_pause_or_nonconflict_failure_releases_the_conflict_seat(tmp_path, boundary):
    scenario, operations, agent, repair, _receipt = _conflict_with_waiter(tmp_path)
    kernel = scenario.kernel
    assert kernel.status("ticket:2")["holds_integration"]
    if boundary == "stop":
        assert kernel.control("stop", "stop-conflict", subject=repair.subject) == "applied"
        kernel.agent_unavailable(repair, "controlled_unstarted_cancel", {}, before_dispatch=True)
    elif boundary == "unavailable":
        kernel.agent_unavailable(repair, "controlled_unstarted_failure", {}, before_dispatch=True)
    elif boundary == "abandon":
        writes = scenario.port.writes()
        assert kernel.control("abandon", "abandon-conflict", discard_worktree=True,
            resource_inspections=scenario.port.inspect_resources(kernel.generation_resources())) == "pending"
        kernel.agent_unavailable(repair, "controlled_unstarted_cancel", {}, before_dispatch=True)
        assert not kernel.status("ticket:2")["holds_integration"]
        assert not kernel.status("ticket:3")["holds_integration"]
        with pytest.raises(KernelError, match="executor_fenced"):
            kernel.guard_agent(repair)
        assert scenario.port.writes() == writes
        return
    else:
        if boundary == "human":
            kernel.receive(repair, wire_json({"result_kind": "human_blocker", "summary": None,
                                             "human_blockers": ["Operator resolution is needed"]}))
        else:
            def invalid_content(call):
                raw = agent(call)
                (Path(call.context["checkout"]) / "README.md").write_text("Unacceptable trailing spaces   \n")
                return raw
            scripted_return(kernel, operations.runner, repair, invalid_content)
        for _ in range(12):
            operations.once()
            if not kernel.status("ticket:2")["holds_integration"]:
                break
        assert kernel.status("ticket:2")["budgets"]["integration"]["developments_used"] == 1
        if boundary == "human":
            assert "human_blocker" in kernel.status("ticket:2")["gates"]
        else:
            with kernel.storage.transaction(write=False) as tx:
                failure = tx.one("SELECT last_failure FROM subjects WHERE id=?", (repair.subject,))[0]
                receipt = json.loads(tx.get(failure, repair.subject))
                assert "--check" in receipt["raw"]["command"]
                assert receipt["raw"].get("merge_conflict") is not True
    assert not kernel.status("ticket:2")["holds_integration"]
    _waiter_gets_seat(scenario, operations)


def test_two_actual_failed_processes_pause_the_same_attempt_and_release_the_seat(tmp_path):
    scenario, operations, _agent, call, _receipt = _conflict_with_waiter(tmp_path)
    kernel = scenario.kernel
    assert kernel.status(call.subject)["holds_integration"]
    original = call
    for index in range(2):
        period_deadline = call.deadline_at
        result = execute(kernel, call, arguments=(sys.executable, "-I", "-c", "raise SystemExit(23)"),
            cwd=Path(call.context["checkout"]), prompt="", environment=os.environ, timeout=5,
            authorize_source=lambda: operations.runner.authorize_agent(call))
        assert result is not None and result.returncode == 23
        assert kernel.worker_settled(call)
        with pytest.raises(ChildProcessError):
            os.waitpid(int(result.identity["pid"]), os.WNOHANG)
        assert kernel.status(call.subject)["budgets"]["integration"]["developments_used"] == 1
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT deadline_at FROM invocations WHERE id=?", (call.invocation_id,))[0] == period_deadline
        if index == 0:
            step = operations.until(lambda step: step.call is not None)
            call = step.call
            assert call is not None and call.attempt_id == original.attempt_id and call.thread_id == original.thread_id
    for _ in range(12):
        operations.once()
        if "execution_failed" in kernel.status(original.subject)["gates"]:
            break
    assert "execution_failed" in kernel.status(original.subject)["gates"]
    assert not kernel.status(original.subject)["holds_integration"]
    _waiter_gets_seat(scenario, operations)


def test_a_targeted_stop_keeps_an_original_unknown_merge_and_its_seat(tmp_path):
    scenario = GraphScenario(tmp_path / "graph", _source())
    kernel = scenario.kernel
    scenario.port.lose_response.add("merge")
    for _ in range(100):
        scenario.step()
        pending = kernel.status("ticket:2")["pending_write"]
        if pending is not None and pending["operation"] == "merge" and pending["status"] == "sent":
            break
    else:
        raise AssertionError("The normal merge producer did not lose its original reply")
    before = scenario.port.writes()
    assert kernel.status("ticket:2")["holds_integration"]
    assert kernel.control("stop", "stop-unknown-merge", subject="ticket:2") == "applied"
    assert "operator_stopped" in kernel.status("ticket:2")["gates"]
    assert kernel.status("ticket:2")["holds_integration"]
    assert not kernel.status("ticket:3")["holds_integration"]
    assert kernel.status("ticket:2")["pending_write"] == pending
    assert scenario.port.writes() == before
