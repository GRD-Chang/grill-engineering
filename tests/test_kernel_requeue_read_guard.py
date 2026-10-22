"""Pending requeue reads retain their Action, deadline and write boundaries."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Issue, Kernel, KernelError, ParentIssue, Requirements
from agent_run_kernel.offline import GraphScenario
from agent_run_kernel.operation_runner import ReadUnavailable
from support.core_operations import CoreOperations, scripted_return


def pending_requeue(tmp_path: Path) -> tuple[GraphScenario, CoreOperations]:
    source = Requirements(ParentIssue(1, "Parent", "Deliver the original requirement.", (2, 3)), tuple(
        Issue(number, "Ticket", "Original requirement", "OPEN", frozenset({"ready-for-agent"}))
        for number in (2, 3)))
    scenario = GraphScenario(tmp_path / "scenario", source)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    for _ in range(80):
        step = operations.once()
        if step.call is not None:
            scripted_return(kernel, operations.runner, step.call, scenario.agent)
        if kernel.status("ticket:2")["phase"] == "checks":
            break
    else:
        raise AssertionError("The original Ticket never published its real PR")
    assert scenario.port.pull_request("ticket:2").state == "OPEN"
    scenario.requirements = replace(source, issues=tuple(
        replace(issue, body="Clarified original requirement") if issue.number == 2 else issue
        for issue in source.issues))
    world = kernel.observe_control("ticket:2", scenario.make_ports)
    assert kernel.source_status()["blocked"] == "requeue_required"
    original = kernel.delivery("ticket:2")
    replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                          checkout=str(Path(original.checkout).with_name("ticket-2-g2")))
    assert kernel.control("requeue", "requeue-original", subject="ticket:2", world=world,
                          replacement=replacement) == "pending"
    assert kernel.status("ticket:2")["generation"] == 1
    return scenario, operations


def read_outage(_parent: int) -> Requirements:
    raise ReadUnavailable("github_unavailable", "Synthetic physical source read outage")


def assert_original_boundary(scenario: GraphScenario, writes) -> None:
    assert scenario.kernel.status("ticket:2")["generation"] == 1
    assert scenario.port.writes() == writes
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT kind,subject,status FROM actions WHERE id='requeue-original'")) == (
            "requeue", "ticket:2", "pending")
        assert not tx.rows("SELECT id FROM effects WHERE operation='retire_pr'")


def test_pending_requeue_can_read_and_keep_one_bounded_read_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, operations = pending_requeue(tmp_path)
    writes = scenario.port.writes()
    # Observation accepts the exact pending Action's facts without writing.
    world = operations.runner.observe("ticket:2")
    assert world.pr == scenario.port.pull_request("ticket:2")
    monkeypatch.setattr(operations.runner.ports, "source", read_outage)
    scenario.kernel.clock = lambda: 100.25
    waiting = operations.once()
    assert (waiting.kind, waiting.reason, waiting.retry_at) == ("waiting", "github_convergence", 105.25)
    assert scenario.kernel.status("ticket:2")["gates"] == ["requeue_required"]
    assert_original_boundary(scenario, writes)


def test_requeue_read_outage_keeps_its_original_deadline_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, operations = pending_requeue(tmp_path)
    kernel = scenario.kernel
    writes = scenario.port.writes()
    monkeypatch.setattr(operations.runner.ports, "source", read_outage)
    kernel.clock = lambda: 100.25
    assert operations.once().retry_at == 105.25
    kernel.clock = lambda: 200.25
    assert operations.once().kind == "waiting"
    with kernel.storage.transaction(write=False) as tx:
        wait = tx.one("SELECT started_at,deadline FROM waits WHERE subject='ticket:2'")
        assert tuple(wait) == (100.25, 700.25)
    scenario.kernel = Kernel(kernel.storage.path)
    scenario.kernel.clock = lambda: 700.25
    restarted = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    monkeypatch.setattr(restarted.runner.ports, "source", read_outage)
    expired = restarted.once()
    assert (expired.kind, expired.reason) == ("paused", "supervision_timeout")
    assert "supervision_timeout" in scenario.kernel.status("ticket:2")["gates"]
    assert_original_boundary(scenario, writes)


@pytest.mark.parametrize("blocked", ["sibling", "cleanup", "other_gate", "other_block"])
def test_requeue_read_permission_never_opens_other_work(tmp_path: Path, blocked: str) -> None:
    """Internal guard-consumer matrix, with public runner/control preconditions.

    This inspects the exact sibling/cleanup token admission contract. Public
    operation reads, restarts and in-flight control fencing are tested separately.
    """
    scenario, _ = pending_requeue(tmp_path)
    kernel, execution = scenario.kernel, scenario.execution
    writes = scenario.port.writes()
    if blocked == "other_gate":
        kernel.control("stop", "stop-original-subject", subject="ticket:2")
    elif blocked == "other_block":
        # A real graph change supplies a non-requeue Run block; no SQL-created
        # Final state or other delivery's generation is borrowed for this unit.
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, sub_issue_numbers=(2, 3, 4)),
            issues=(*scenario.requirements.issues,
                    Issue(4, "New scope", "Another delivery", "OPEN", frozenset({"ready-for-agent"}))))
        kernel.observe_control("ticket:2", scenario.make_ports)
        assert kernel.source_status()["blocked"] == "unsupported_scope_change"
    subject = "ticket:3" if blocked == "sibling" else "ticket:2"
    token = kernel._begin_automatic_step(execution, subject, cleanup=blocked == "cleanup")
    with pytest.raises(KernelError, match="automatic_step_paused"):
        kernel._guard_automatic_step(token, execution)
    assert_original_boundary(scenario, writes)


@pytest.mark.parametrize("change", ["stop", "replacement"])
def test_requeue_read_is_bound_to_the_same_action_after_external_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    scenario, operations = pending_requeue(tmp_path)
    kernel = scenario.kernel
    writes = scenario.port.writes()
    delivery = operations.runner.ports.delivery
    observed = []

    def changed(request):
        facts = delivery(request)
        observed.append(request)
        if change == "stop":
            kernel.control("stop", "stop-during-read")
        else:
            scenario.requirements = replace(scenario.requirements, issues=tuple(
                replace(issue, body="Another clarification during the read") if issue.number == 2 else issue
                for issue in scenario.requirements.issues))
            world = kernel.observe_control("ticket:2", scenario.make_ports)
            # Core itself supersedes the stale Action before a new public
            # requeue is accepted. Neither operation may retire the original PR.
            ended = scenario.runner().run_once(agent_capacity=0)
            assert ended.reason == "requeue_scope_changed"
            original = kernel.delivery("ticket:2")
            replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                checkout=str(Path(original.checkout).with_name("ticket-2-g2")))
            assert kernel.control("requeue", "requeue-replacement", subject="ticket:2", world=world,
                                  replacement=replacement) == "pending"
        return facts

    monkeypatch.setattr(operations.runner.ports, "delivery", changed)
    fenced = operations.once()
    assert len(observed) == 1
    assert fenced.reason == ("executor_fenced" if change == "stop" else "automatic_step_superseded")
    assert kernel.status("ticket:2")["generation"] == 1
    assert scenario.port.writes() == writes
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='retire_pr'")
        if change == "replacement":
            assert tx.one("SELECT status FROM actions WHERE id='requeue-original'")[0] == "superseded"
            assert tx.one("SELECT status FROM actions WHERE id='requeue-replacement'")[0] == "pending"
