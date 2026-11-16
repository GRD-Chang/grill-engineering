"""Pending requeue reads retain their Action, deadline and write boundaries."""
from agent_run.core.operation_runner import observe_control
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agent_run.model import Issue, ParentIssue, PullRequest, Requirements, wire_json
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from support.offline import GraphScenario
from agent_run.core.operation_contract import ReadUnavailable
from support.core_operations import CoreOperations, ReadOnlyPorts, scripted_return
from support.requirements_scenarios import source_run
from agent_run.core.operation_contract import DeliveryFacts


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
    assert scenario.port.pull_request_by_ref(scenario.kernel.delivery("ticket:2").source_ref, scenario.kernel.delivery("ticket:2").target_ref).state == "OPEN"
    scenario.requirements = replace(source, issues=tuple(
        replace(issue, body="Clarified original requirement") if issue.number == 2 else issue
        for issue in source.issues))
    world = observe_control(kernel, "ticket:2", scenario.make_ports)
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


def test_requeue_read_outage_keeps_its_original_deadline_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, operations = pending_requeue(tmp_path)
    kernel = scenario.kernel
    writes = scenario.port.writes()
    assert operations.runner.observe("ticket:2").pr == scenario.port.pull_request_by_ref(
        kernel.delivery("ticket:2").source_ref, kernel.delivery("ticket:2").target_ref)
    monkeypatch.setattr(operations.runner.ports, "source", read_outage)
    kernel.clock = lambda: 100.25
    assert operations.once().retry_at == 105.25
    kernel.clock = lambda: 200.25
    assert operations.once().kind == "waiting"
    with kernel.storage.transaction(write=False) as tx:
        wait = tx.one("SELECT started_at,deadline FROM waits WHERE subject='ticket:2'")
        assert tuple(wait) == (100.25, 700.25)
    scenario.kernel = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    scenario.kernel.clock = lambda: 700.25
    restarted = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    monkeypatch.setattr(restarted.runner.ports, "source", read_outage)
    expired = restarted.once()
    assert (expired.kind, expired.reason) == ("paused", "supervision_timeout")
    assert "supervision_timeout" in scenario.kernel.status("ticket:2")["gates"]
    assert_original_boundary(scenario, writes)


@pytest.fixture
def pending_read(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2, 3)), tuple(
        Issue(number, "Ticket", "Original requirement", "OPEN", frozenset({"ready-for-agent"}))
        for number in (2, 3)))
    kernel, _ = source_run(tmp_path / "kernel.sqlite3", source)
    execution = kernel.claim_executor("guard-reader")
    original = kernel.delivery("ticket:2")
    pr = PullRequest(11, "example/project", original.source_ref, original.target_ref,
        original.base, original.base, "Published Ticket", "Original publication",
        actor="publisher", head_repository="example/project")
    # Upstream publication facts, not a claim that this fixture published a PR.
    with kernel.storage.transaction() as tx:
        run = tx.one("SELECT * FROM runs")
        payload = tx.put(original.key, "publication_input", b"{}")
        tx.execute("INSERT INTO attempts(run_id,id,subject,generation,ordinal,role,purpose,window,parent_revision,"
            "graph_revision,revision,base_sha,completion_revision,context_id,status,outcome) "
            "VALUES (?,'original-work',?,1,1,'development','requirements',1,?,?,?,?,'no-tickets',?,'consumed','pass')",
            (run["id"], original.key, run["parent_revision"], run["graph_revision"], original.revision,
             original.base.sha, payload))
        receipt = tx.put(original.key, "publication_receipt", wire_json({"pr": asdict(pr)}))
        tx.execute("UPDATE subjects SET phase='checks',pr_number=11,publication_sha=?,publication_tree=? WHERE id=?",
            (original.base.sha, original.base.tree, original.key))
        tx.execute("INSERT INTO effects(run_id,id,subject,operation,status,parent_revision,graph_revision,revision,"
            "generation,source_ref,target_ref,before_sha,base_sha,base_tree,actor,payload_id,receipt_id,pr_number) "
            "VALUES (?,'publication',?,'pr_write','confirmed',?,?,?,1,?,?,?,?,?,'publisher',?,?,11)",
            (run["id"], original.key, run["parent_revision"], run["graph_revision"], original.revision,
             original.source_ref, original.target_ref, original.base.sha, original.base.sha, original.base.tree,
             payload, receipt))
    source = replace(source, issues=tuple(replace(issue, body="Clarified original requirement")
        if issue.number == 2 else issue for issue in source.issues))
    ports = ReadOnlyPorts(source, original.source_ref, original.target_ref,
        DeliveryFacts(original.base, original.base, pr=pr))
    world = observe_control(kernel, original.key, lambda _binding: ports)
    replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
        checkout=str(tmp_path / "next-generation"))
    assert kernel.control("requeue", "requeue-original", subject=original.key, world=world,
        replacement=replacement) == "pending"
    return kernel, execution, ports, replacement


def assert_pending_read(kernel):
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT generation FROM subjects WHERE id='ticket:2'")[0] == 1
        assert tx.one("SELECT status FROM actions WHERE id='requeue-original'")[0] == "pending"
        assert not tx.rows("SELECT id FROM effects WHERE operation='retire_pr'")


@pytest.mark.parametrize("blocked", ["sibling", "cleanup", "other_gate", "other_block"])
def test_requeue_read_permission_never_opens_other_work(pending_read, blocked: str) -> None:
    """Internal guard-consumer matrix, with public runner/control preconditions.

    This inspects the exact sibling/cleanup token admission contract. Public
    operation reads, restarts and in-flight control fencing are tested separately.
    """
    kernel, execution, ports, _ = pending_read
    if blocked == "other_gate":
        kernel.control("stop", "stop-original-subject", subject="ticket:2")
    elif blocked == "other_block":
        # A real graph change supplies a non-requeue Run block; no SQL-created
        # Final state or other delivery's generation is borrowed for this unit.
        ports.requirements = replace(ports.requirements,
            parent=replace(ports.requirements.parent, sub_issue_numbers=(2, 3, 4)),
            issues=(*ports.requirements.issues,
                    Issue(4, "New scope", "Another delivery", "OPEN", frozenset({"ready-for-agent"}))))
        observe_control(kernel, "ticket:2", lambda _binding: ports)
        assert kernel.source_status()["blocked"] == "unsupported_scope_change"
    subject = "ticket:3" if blocked == "sibling" else "ticket:2"
    with pytest.raises(KernelError, match="automatic_step_paused"):
        kernel.begin_observation(subject, execution=execution, cleanup=blocked == "cleanup")
    assert_pending_read(kernel)


@pytest.mark.parametrize("change", ["stop", "replacement"])
def test_requeue_read_is_bound_to_the_same_action_after_external_io(
    pending_read, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    kernel, execution, ports, replacement = pending_read
    operations = CoreOperations(kernel, execution, lambda _binding: ports)
    delivery = operations.runner.ports.delivery
    observed = []

    def changed(request):
        facts = delivery(request)
        observed.append(request)
        if change == "stop":
            kernel.control("stop", "stop-during-read")
        else:
            ports.requirements = replace(ports.requirements, issues=tuple(
                replace(issue, body="Another clarification during the read") if issue.number == 2 else issue
                for issue in ports.requirements.issues))
            fresh = ReadOnlyPorts(ports.requirements, ports.source_ref, ports.target_ref, ports.facts)
            world = observe_control(kernel, "ticket:2", lambda _binding: fresh)
            ended = CoreOperations(kernel, execution, lambda _binding: fresh).once(agent_capacity=0)
            assert ended.reason == "requeue_scope_changed"
            assert kernel.control("requeue", "requeue-replacement", subject="ticket:2", world=world,
                                  replacement=replacement) == "pending"
        return facts

    monkeypatch.setattr(operations.runner.ports, "delivery", changed)
    fenced = operations.once()
    assert len(observed) == 1
    assert fenced.reason == ("executor_fenced" if change == "stop" else "automatic_step_superseded")
    assert kernel.status("ticket:2")["generation"] == 1
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='retire_pr'")
        if change == "replacement":
            assert tx.one("SELECT status FROM actions WHERE id='requeue-original'")[0] == "superseded"
            assert tx.one("SELECT status FROM actions WHERE id='requeue-replacement'")[0] == "pending"
