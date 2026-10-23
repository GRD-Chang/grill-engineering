"""Public Core operation seam: selection, bounded I/O and durable settlement."""
from pathlib import Path
from dataclasses import replace

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, Requirements, RunSpec
from agent_run_kernel.model import Checks, Issue, ParentIssue
from agent_run_kernel.model import KernelError
from support.offline import GraphScenario
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
        return s.port.source(s.requirements, parent_number)

    def delivery(self, request):
        s = self.scenario
        return s.port.delivery(request, ('none',), [], publisher_identity=s.spec.inputs.runner_identity)

    def inspect(self, resources):
        return self.scenario.port.inspect_resources(resources)

    def execute(self, effect, authorize):
        return self.scenario.port.execute(effect, authorize)

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


@pytest.mark.parametrize("uncertain", ["unknown", "third"])
def test_sent_merge_can_settle_a_late_readback_without_new_write_or_attempt(tmp_path, uncertain):
    from agent_run_kernel.model import EffectResult

    source = Requirements(ParentIssue(1, "Parent", "One exact ticket", (2,)),
                          (Issue(2, "Ticket", "Ticket requirement", "OPEN", frozenset({"ready-for-agent"})),))
    s = GraphScenario(tmp_path / "graph", source)
    ports = Ports(s)
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    execute, reconcile = ports.execute, ports.reconcile
    sent = []

    def lost(effect, authorize):
        result = execute(effect, authorize)
        if effect.operation == "merge":
            sent.append(effect)
            raise OSError("Original merge response lost after the physical write")
        return result

    ports.execute = lost
    for _ in range(100):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
        if sent:
            break
    else:
        raise AssertionError("Core never sent the original merge")
    effect, = sent
    assert step.reason == "original_write_readback_required"
    ports.reconcile = lambda selected: EffectResult(uncertain) if selected == effect else reconcile(selected)
    assert runner.run_once().kind == "paused"
    with s.kernel.storage.transaction(write=False) as tx:
        attempts = tx.one("SELECT count(*) FROM attempts")[0]
        original = tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (effect.effect_id,)))
    assert original == ("sent", 1)
    writes = s.port.writes()
    # A known manual gate does not turn Host supervision into a busy poll.
    assert runner.state().disposition == "quiescent"
    ports.reconcile = reconcile
    assert runner.run_once().kind == "progress"
    with s.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts")[0] == attempts
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (effect.effect_id,))) == ("observed", 1)
    assert s.port.writes() == writes
    assert sum(write["operation"] == "merge" for write in writes) == 1
    assert ("write_result_unknown" if uncertain == "unknown" else "write_conflict") in s.kernel.status(effect.subject)["gates"]


def test_original_approved_prior_merge_continues_new_review_without_a_new_control(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)),
                          (Issue(2, "Ticket", "Ticket requirement", "OPEN", frozenset({"ready-for-agent"})),))
    s = GraphScenario(tmp_path / "graph", source)
    ports = Ports(s)
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    for _ in range(120):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
        if s.kernel.source_status()["final_subject"] is not None and s.kernel.status("final:1")["phase"] == "approval":
            break
    else:
        raise AssertionError("Final did not reach its real first approval")
    s.kernel.control("approve", "original-final-approval", subject="final:1", world=runner.observe("final:1"))
    execute = ports.execute
    sent = []

    def interrupted(effect, authorize):
        if effect.subject == "final:1" and effect.operation == "merge":
            authorize()
            sent.append(effect)
            raise OSError("Original merge was interrupted before remote application")
        return execute(effect, authorize)

    ports.execute = interrupted
    for _ in range(10):
        runner.run_once()
        if sent:
            break
    effect, = sent
    assert ports.reconcile(effect).disposition == "prior"
    s.requirements = replace(source, parent=replace(source.parent, body="Changed after the original approval"))
    runner.observe("final:1")
    ports.execute = execute
    assert runner.run_once().kind == "progress"
    assert s.kernel.source_status()["blocked"] == "final_refresh"
    with pytest.raises(KernelError, match="final_approval_noncancellable"):
        s.kernel.control("requeue", "cannot-replace-approval", subject="final:1", world=s.kernel.observe_control("final:1", lambda binding: ports))
    assert runner.state().disposition == "ready"
    for _ in range(80):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
        status = s.kernel.status("final:1")
        if status["phase"] == "approval":
            break
    else:
        raise AssertionError("The original proved-unapplied approval never continued")
    assert status["generation"] == 2
    assert status["budgets"]["integration"]["reviews_used"] == 2
    assert not [write for write in s.port.writes() if write["subject"] == "final:1" and write["operation"] == "merge"]


@pytest.mark.parametrize("external_change", ["foreign", "absent"])
def test_cleanup_records_foreign_observation_and_keeps_absence_admission(tmp_path, external_change):
    source = Requirements(ParentIssue(1, "Parent", "One ticket", (2,)),
                          (Issue(2, "Ticket", "Ticket requirement", "OPEN", frozenset({"ready-for-agent"})),))
    s = GraphScenario(tmp_path / "graph", source)
    ports = Ports(s)
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    execute = ports.execute
    inspected = []
    foreign = []

    def changed(effect, authorize):
        if effect.operation != "cleanup_local_ref":
            return execute(effect, authorize)
        inspected.append(effect)
        expected = effect.payload["expected_head"]
        if external_change == "foreign":
            prior = s.port.git.commit(expected)
            other = s.port.git.make_commit(prior.tree, (prior.sha,), "Independent foreign ref")
            foreign.append(other)
            s.port.git.run("update-ref", effect.source_ref, other.sha, expected)
        else:
            s.port.git.run("update-ref", "-d", effect.source_ref, expected)
        guard = authorize if external_change == "absent" else lambda: pytest.fail("A foreign observation cannot authorize a write")
        return s.port.local.execute(effect, guard)

    ports.execute = changed
    for _ in range(100):
        step = runner.run_once()
        if step.call is not None:
            settle_agent(s, runner, step.call)
        if inspected:
            break
    effect, = inspected
    assert s.kernel.status(effect.subject)["phase"] == "complete"
    with s.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT status,attempts,receipt_id FROM effects WHERE id=?", (effect.effect_id,))
        assert row["attempts"] == (0 if external_change == "foreign" else 1)
        dispatched = tx.rows("SELECT position FROM history WHERE kind='effect_dispatched' AND identity=?", (effect.effect_id,))
        assert len(dispatched) == (0 if external_change == "foreign" else 1)
        cleanup = tx.one("SELECT stage,state,outcome FROM resource_cleanup WHERE effect_id=?", (effect.effect_id,))
        assert cleanup["stage"] == 1 and cleanup["state"] == "pending"
        if external_change == "foreign":
            assert step.kind == "paused" and row["status"] == "prepared" and row["receipt_id"] is None
            assert cleanup["outcome"] == "third"
            assert s.port.git.commit(effect.source_ref) == foreign[0]
        else:
            assert step.kind == "progress" and row["status"] == "observed" and row["receipt_id"]
    if external_change == "absent":
        assert runner.run_once().kind == "progress"
        assert s.kernel.status(effect.subject)["resource_cleanup"]["stage"] == 2
