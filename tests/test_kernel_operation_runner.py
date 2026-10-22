"""Public Core operation seam: selection, bounded I/O and durable settlement."""
from pathlib import Path
from dataclasses import replace
from contextlib import closing
import sqlite3

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, Requirements, RunSpec
from agent_run_kernel.model import Checks, Issue, ParentIssue
from agent_run_kernel.offline import GraphScenario
from agent_run_kernel.operation_runner import DeliveryFacts
from test_kernel_requirements import independent_source


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


def setup(tmp_path, *, concurrency=1):
    s = GraphScenario(tmp_path / "graph", independent_source(), policy=Policy(concurrency=concurrency))
    ports = Ports(s)
    return s, ports, s.kernel.operation_runner(s.execution, lambda binding: ports)


def test_readonly_selection_pages_130_tickets_without_allocating(tmp_path: Path):
    numbers = tuple(range(2, 132))
    source = Requirements(ParentIssue(1, "Parent", "Shared outcome", numbers), tuple(
        Issue(number, f"Ticket {number}", "Synthetic requirement", "OPEN", frozenset({"ready-for-agent"}))
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
    assert state.disposition == "ready" and state.active == ()
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
            return
    raise AssertionError("Core did not admit Final after original Ticket completion")


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
