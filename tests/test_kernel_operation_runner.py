"""Public Core operation seam: selection, bounded I/O and durable settlement."""
from pathlib import Path
from dataclasses import replace
from contextlib import closing
import sqlite3

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, Requirements, RunSpec
from agent_run_kernel.model import Checks, Issue, ParentIssue
from agent_run_kernel.model import KernelError
from agent_run_kernel.offline import GraphScenario
from agent_run_kernel.operation_runner import DeliveryFacts
from agent_run_kernel.operation_runner import ReadUnavailable
from agent_run_kernel.query import read
from test_kernel_requirements import independent_source, native_input


class Ports:
    def __init__(self, scenario):
        self.scenario = scenario
        self.reads = 0

    def source(self, parent_number):
        self.reads += 1
        s = self.scenario
        assert parent_number == s.requirements.parent.number
        with closing(sqlite3.connect(s.port.database)) as db:
            closed = {row[0] for row in db.execute("SELECT issue FROM closed")}
        return replace(s.requirements, issues=tuple(replace(issue,
            state="CLOSED" if issue.number in closed else issue.state,
            blocked_by=tuple(replace(blocker, state="CLOSED") if blocker.number in closed else blocker for blocker in issue.blocked_by))
            for issue in s.requirements.issues))

    def delivery(self, request):
        s = self.scenario
        source, target = s.port.remote_commit(request.source_ref), s.port.remote_commit(request.target_ref)
        subject = next(item.key for item in s.spec.deliveries if item.source_ref == request.source_ref)
        pr = s.port.pull_request(subject, request.source_ref) if request.read_pr else None
        checks = None if pr is None else Checks(pr.number, pr.head.sha, "none")
        ancestry = tuple((before, after.sha) for before in request.anchors for after in (source, target)
                         if after is not None and s.port.git.is_ancestor(before, after.sha))
        return DeliveryFacts(target, source, pr, checks, ancestry, "offline-publisher", s.spec.inputs.runner_identity)

    def inspect(self, resources):
        return self.scenario.port.inspect_resources(resources)

    def execute(self, effect, authorize):
        acknowledgement = self.scenario.port.execute(effect, authorize)
        return acknowledgement if effect.operation == "issue_link" else self.reconcile(effect)

    def reconcile(self, effect):
        return self.scenario.port.reconcile(effect)

    def prepare_publication(self, effect):
        return self.scenario.port.local.prepare_publication(effect)

    def prepare_retirement(self, effect):
        return self.scenario.port.prepare_retirement(effect)


def setup(tmp_path, *, concurrency=1, dependent=False):
    source = Requirements.from_wire(native_input()["initial_requirements"]) if dependent else independent_source()
    s = GraphScenario(tmp_path / "graph", source, policy=Policy(concurrency=concurrency))
    ports = Ports(s)
    return s, ports, s.kernel.operation_runner(s.execution, lambda binding: ports)


@pytest.mark.parametrize("ready", [True, False])
def test_readonly_selection_pages_130_tickets_without_allocating(tmp_path: Path, ready):
    numbers = tuple(range(2, 132))
    source = Requirements(ParentIssue(1, "Parent", "Shared outcome", numbers), tuple(
        Issue(number, f"Ticket {number}", "Synthetic requirement", "OPEN", frozenset({"ready-for-agent"}) if ready else frozenset())
        for number in numbers))
    base = Commit("synthetic-base", "synthetic-tree")
    deliveries = tuple(Delivery(f"ticket:{n}", DeliveryKind.TICKET, n, "untrusted", base,
                                 f"refs/heads/ticket-{n}", "refs/heads/run") for n in numbers)
    deliveries += (Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "untrusted", base,
                            "refs/heads/run", "refs/heads/main"),)
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", RunSpec("large-run", "example/project", 1,
        "untrusted", "untrusted", deliveries, Policy(concurrency=1)), requirements=source)
    before = kernel.history("ticket:2")
    state = kernel.operation_state()
    assert state.disposition == ("ready" if ready else "quiescent") and state.active == ()
    assert kernel.history("ticket:2") == before
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT id FROM attempts")


def test_first_observation_accepts_facts_but_never_advances_or_allocates(tmp_path):
    s, ports, runner = setup(tmp_path)
    before = s.kernel.status("ticket:3")
    world = runner.observe("ticket:3")
    assert world.target == s.port.remote_commit("refs/heads/run")
    assert ports.reads == 1
    after = s.kernel.status("ticket:3")
    assert after["phase"] == before["phase"] == "workspace"
    assert after["pending_attempt"] is None
    assert after["budgets"] == before["budgets"]


def test_allocated_agent_returns_immediately_without_post_allocation_read(tmp_path):
    s, ports, runner = setup(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    source = ports.source
    delivery = ports.delivery
    def slow_source(parent):
        now[0] += 20
        return source(parent)
    def slow_delivery(request):
        now[0] += 10
        return delivery(request)
    ports.source, ports.delivery = slow_source, slow_delivery
    for _ in range(20):
        step = runner.run_once()
        if step.call is not None:
            assert s.kernel.status(step.subject)["pending_attempt"] == step.call.attempt_id
            assert step.call.deadline_at == now[0] + step.call.deadline_seconds
            assert not runner.worker_settled(step.call)
            return
    raise AssertionError("Core did not return the allocated AgentCall")


def settle_agent(s, runner, call):
    assert s.kernel.dispatch_agent(call)
    runner.authorize_agent(call)
    process = {"pid": 4242, "start_token": "synthetic", "pgid": 4242,
               "host_pid": 4241, "host_start_token": "synthetic-host"}
    s.kernel.agent_started(call, process)
    s.select(call.subject)  # Observe the returned call; never choose runnable work.
    s.kernel.receive(call, s.agent(call))
    assert not runner.worker_settled(call)
    s.kernel.agent_exited(call, process, 0)
    assert runner.worker_settled(call)


def test_one_operation_returns_committed_publication_final_and_cleanup_boundaries(tmp_path):
    s, ports, runner = setup(tmp_path)
    published = set()
    for _ in range(160):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
        if step.subject and s.kernel.status(step.subject)["phase"] == "publish":
            published.add(step.subject)
        if s.kernel.source_status()["final_subject"] is not None:
            assert published >= {"ticket:3", "ticket:4"}
            assert len(s.kernel.source_status()["completions"]) == 2
            for subject in ("ticket:3", "ticket:4"):
                assert s.kernel.status(subject)["resource_cleanup"]["state"] == "complete"
            break
    else:
        raise AssertionError("Core did not admit Final after original Ticket completion")
    final = s.kernel.source_status()["final_subject"]
    drive_until(s, runner, lambda step: s.kernel.status(final)["phase"] == "approval")
    assert runner.state().disposition == "quiescent"
    s.kernel.control("approve", "approve-synthetic-final", subject=final, world=runner.observe(final))
    for _ in range(80):
        if runner.state().disposition == "complete":
            assert s.kernel.status(final)["phase"] == "complete"
            assert s.kernel.status(final)["resource_cleanup"]["state"] == "complete"
            return
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
    raise AssertionError("Approved Final did not close and settle its resources")


def drive_until(s, runner, predicate):
    for _ in range(160):
        step = runner.run_once()
        if predicate(step):
            return step
        if step.call is not None:
            settle_agent(s, runner, step.call)
    raise AssertionError("Core did not reach the requested public boundary")


def test_lost_merge_response_records_original_readback_before_any_source_read(tmp_path):
    s, ports, runner = setup(tmp_path, dependent=True)
    s.port.lose_response.add("merge")
    lost = drive_until(s, runner, lambda step: step.reason == "original_write_readback_required")
    original = s.kernel.status(lost.subject)["pending_write"]
    assert original["status"] == "sent" and original["operation"] == "merge"
    assert s.kernel.status(lost.subject)["holds_integration"]
    reads = ports.reads
    polled = runner.run_once()
    assert polled.kind == "progress" and ports.reads == reads
    assert s.kernel.status(lost.subject)["pending_write"]["id"] == original["id"]
    assert len([item for item in s.port.writes() if item["operation"] == "merge"]) == 1
    assert any(row["identity"] == original["id"] and row["kind"] == "effect_readback"
               for row in s.kernel.history(lost.subject))


def test_unknown_original_write_quiesces_and_keeps_integration_owner(tmp_path):
    s, ports, runner = setup(tmp_path, dependent=True)
    s.port.lose_response.add("merge")
    lost = drive_until(s, runner, lambda step: step.reason == "original_write_readback_required")
    s.port.force_read["merge"] = "unknown"
    assert runner.run_once().kind == "paused"
    assert s.kernel.status(lost.subject)["gates"] == ["write_result_unknown"]
    assert s.kernel.status(lost.subject)["holds_integration"]
    assert runner.state().disposition == "quiescent"
    assert len([item for item in s.port.writes() if item["operation"] == "merge"]) == 1


def test_read_outage_poll_and_restart_keep_original_deadline(tmp_path):
    s, ports, runner = setup(tmp_path, dependent=True)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "synthetic unavailable source")
    ports.source = unavailable
    assert runner.run_once().retry_at == 105.25
    assert runner.state().disposition == "waiting"
    s.kernel = Kernel(s.kernel.storage.path)
    s.kernel.clock = lambda: now[0]
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    now[0] = 700.25
    assert runner.run_once().reason == "supervision_timeout"
    assert runner.state().disposition == "quiescent"


def test_stop_during_source_read_cannot_apply_late_requirements(tmp_path):
    s, ports, runner = setup(tmp_path, dependent=True)
    before = s.kernel.source_status()["observation"]
    source = ports.source
    def stop_during_read(parent):
        result = source(parent)
        s.kernel.control("stop", "stop-original-read")
        return replace(result, parent=replace(result.parent, body="late unaccepted requirement"))
    ports.source = stop_during_read
    step = runner.run_once()
    assert step.kind == "paused" and step.reason == "executor_fenced"
    assert s.kernel.source_status()["observation"] == before
    assert runner.state().disposition == "quiescent"


def test_binding_factory_runs_after_core_fixes_frozen_inputs(tmp_path):
    s = GraphScenario(tmp_path / "binding", independent_source())
    seen = []
    def make_ports(binding):
        assert binding.repository == "example/project"
        assert binding.parent_number == 1
        assert binding.inputs.runner_identity == {"source": "host"}
        seen.append(binding)
        return Ports(s)
    runner = s.kernel.operation_runner(s.execution, make_ports)
    assert seen == [runner.binding]
    assert runner.binding.repository_root == s.port.git.root.resolve()


def test_expired_unlaunched_allocation_needs_no_slow_source_read(tmp_path):
    s, ports, runner = setup(tmp_path, dependent=True)
    selected = drive_until(s, runner, lambda step: step.call is not None)
    s.kernel.clock = lambda: selected.call.deadline_at
    def forbidden_read(parent):
        raise AssertionError("Local unlaunched expiry must not wait for GitHub")
    ports.source = forbidden_read
    assert runner.state().disposition == "ready"
    assert runner.state().wake_at is None
    assert runner.run_once().reason == "invocation_deadline_exceeded"
    assert runner.worker_settled(selected.call)


def test_n1_long_agent_keeps_independent_ci_poll_and_exact_seat(tmp_path):
    s, ports, runner = setup(tmp_path)
    drive_until(s, runner, lambda step: s.kernel.status("ticket:3")["phase"] == "checks")
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    delivery = ports.delivery
    polled = []
    def pending(request):
        facts = delivery(request)
        if request.source_ref == "refs/heads/ticket-3" and facts.pr is not None:
            polled.append(request)
            return replace(facts, checks=Checks(facts.pr.number, facts.pr.head.sha, "pending"))
        return facts
    ports.delivery = pending
    selected = drive_until(s, runner, lambda step: step.call is not None)
    assert selected.call.subject == "ticket:4"
    assert s.kernel.dispatch_agent(selected.call)
    runner.authorize_agent(selected.call)
    process = {"pid": 4242, "start_token": "synthetic", "pgid": 4242,
               "host_pid": 4241, "host_start_token": "synthetic-host"}
    s.kernel.agent_started(selected.call, process)
    before = len(polled)
    now[0] = 106.25
    step = runner.run_once(agent_capacity=0)
    assert step.call is None and len(polled) > before
    assert s.kernel.status("ticket:3")["holds_integration"]
    assert runner.state().active == (selected.call.lease_id,)
    assert not runner.worker_settled(selected.call)


def test_ready_fifo_is_preserved_while_host_has_no_new_agent_slot(tmp_path):
    s, ports, runner = setup(tmp_path)
    first = drive_until(s, runner, lambda step: step.call is not None)
    assert first.call.subject == "ticket:3"
    before = s.kernel.status("ticket:4")["budgets"]
    for _ in range(8):
        step = runner.run_once(agent_capacity=0)
        assert step.call is None
        if step.kind == "needs_agent" and step.subject == "ticket:4":
            break
    else:
        raise AssertionError("ready Ticket never retained its FIFO position")
    assert s.kernel.status("ticket:4")["budgets"] == before
    settle_agent(s, runner, first.call)
    s.kernel = Kernel(s.kernel.storage.path)
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    second = drive_until(s, runner, lambda step: step.call is not None)
    assert second.call.subject == "ticket:4"


def test_explicit_approval_poll_observes_target_append_without_background_polling(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Original Parent requirement", ()), ())
    s = GraphScenario(tmp_path / "parent", source, policy=Policy(resync_limit=0))
    ports = Ports(s)
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    drive_until(s, runner, lambda step: s.kernel.status("parent:1")["phase"] == "approval")
    assert runner.state().disposition == "quiescent"
    unchanged = runner.run_once()
    assert unchanged.kind == "approval"
    assert runner.state().disposition == "quiescent"
    budgets = s.kernel.status("parent:1")["budgets"]
    s.port.external_remote_append(s.delivery.target_ref, {"external.txt": "Original external work"}, "append")
    assert runner.run_once().kind == "paused"
    data = read(s.kernel.storage.path, "status").data
    assert data["integration_resync"]["limit"] == 0 and data["integration_resync"]["used"] == 0
    assert data["integration_resync"]["window"] == 1 and data["integration_resync"]["open"] is False
    action = next(item for item in data["operator_actions"] if item["reason"] == "integration_resync_exhausted")
    assert action["operation"] == "resume" and action["action"]["command"] == "resume"
    assert s.kernel.status("parent:1")["budgets"] == budgets
    s.kernel.control("resume", "explicit-zero-resync-resume", subject="parent:1")
    assert runner.run_once().kind == "paused"
    resumed = read(s.kernel.storage.path, "status").data["integration_resync"]
    assert resumed["limit"] == 0 and resumed["used"] == 0
    assert resumed["window"] == 1 and resumed["open"] is False
    assert s.kernel.status("parent:1")["budgets"] == budgets
