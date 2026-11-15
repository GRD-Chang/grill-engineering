"""An admitted Host gets one finite observation pass, never new write authority."""
from support.control_scenarios import managed_parent, admit_scenario, sent_workspaces
from support.core_operations import prepare_scripted_agent
from pathlib import Path

import pytest

from agent_run.model import DeliveryKind, EffectResult
from agent_run.errors import KernelError
from agent_run.model import HostObservation, Issue, ParentIssue, Requirements
from agent_run.errors import ObservationSuperseded
from support.offline import GraphScenario, Scenario
from agent_run.queries.run import read
from agent_run.core.operation_contract import ReadUnavailable


def test_activation_rejects_direct_claim_without_external_reads(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    runner = scenario.runner()
    monkeypatch.setattr(runner.ports, "source", lambda parent: pytest.fail("activation is not a physical read"))
    with pytest.raises(KernelError, match="managed_observation_required"):
        runner.activate_observation()
    assert scenario.kernel.status(scenario.delivery.key)["pending_attempt"] is None


def test_managed_activation_reads_the_original_sent_target_once(tmp_path, monkeypatch, parent_before_push):
    scenario = admit_scenario(parent_before_push, tmp_path)
    kernel = scenario.kernel
    now = [100.25]
    kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("push")
    assert scenario.run().kind == "waiting"
    pending = kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == "push" and pending["status"] == "sent"
    runner = scenario.runner()
    reconcile = scenario.port.reconcile
    with monkeypatch.context() as conflict:
        conflict.setattr(scenario.port, "reconcile", lambda effect:
            EffectResult("third") if effect.operation == "push" else reconcile(effect))
        now[0] = 105.25
        assert runner.run_once().kind == "paused"
    assert runner.state().disposition == "quiescent"
    reads = []
    reconcile = runner.ports.reconcile

    def physical(effect):
        reads.append(effect)
        return reconcile(effect)

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("activation grants no new write"))
    monkeypatch.setattr(runner.ports, "source", lambda *a: pytest.fail("original sent readback precedes source"))
    runner.activate_observation()
    assert runner.state().disposition == "ready"
    assert kernel.operation_state().disposition == "quiescent"
    assert runner.run_once().kind == "progress"
    assert len(reads) == 1 and reads[0].effect_id == pending["id"]
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    read(kernel.storage.path, 'history', storage=kernel.storage)
    read(kernel.storage.path, 'status', storage=kernel.storage)
    assert len(reads) == 1
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (pending["id"],))) == ("observed", 1)
    assert "write_conflict" in kernel.status(scenario.delivery.key)["gates"]
    assert sum(item["operation"] == "push" for item in scenario.port.writes()) == 1


def test_managed_approval_observation_is_finite_and_passive_afterwards(tmp_path, monkeypatch, parent_at_approval):
    scenario = admit_scenario(parent_at_approval, tmp_path)
    assert scenario.run().kind == "approval"
    before = scenario.kernel.status(scenario.delivery.key)["budgets"]
    runner = scenario.runner()
    source, delivery = runner.ports.source, runner.ports.delivery
    calls = []

    def original_source(parent):
        calls.append("source")
        return source(parent)

    def original_delivery(request):
        calls.append("delivery")
        return delivery(request)

    monkeypatch.setattr(runner.ports, "source", original_source)
    monkeypatch.setattr(runner.ports, "delivery", original_delivery)
    runner.activate_observation()
    assert calls == [] and runner.state().disposition == "ready"
    assert runner.run_once().kind == "approval"
    assert calls == ["source", "delivery"]
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == before
    assert calls == ["source", "delivery"]



@pytest.mark.parametrize("first_result", ["unknown", "third"])
def test_each_captured_sent_intent_is_read_once_without_starving_the_next(tmp_path, first_result):
    scenario, runner, sent = sent_workspaces(tmp_path)
    reconcile = runner.ports.reconcile
    reads = []

    def physical(effect):
        reads.append(effect.effect_id)
        return EffectResult(first_result) if effect.effect_id == sent[0].effect_id else reconcile(effect)

    runner.ports.reconcile = physical
    runner.activate_observation()
    assert runner.run_once().kind == "paused"
    assert runner.state().disposition == "ready"
    assert runner.run_once().kind == "progress"
    assert reads == [effect.effect_id for effect in sent]
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert reads == [effect.effect_id for effect in sent]
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts")
        assert [row[0] for row in tx.rows("SELECT attempts FROM effects WHERE operation='workspace'")] == [1, 1]


@pytest.mark.parametrize("failure", ["read_wait", "cas"])
def test_first_failed_observation_defers_to_next_and_keeps_original_deadline(tmp_path, failure):
    scenario, runner, sent = sent_workspaces(tmp_path)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    reconcile = runner.ports.reconcile
    reads = []
    fail = [True]

    def physical(effect):
        reads.append(effect.effect_id)
        if effect.effect_id == sent[0].effect_id and fail[0]:
            if failure == "cas":
                scenario.kernel.refresh(lambda: scenario.requirements)
                raise ObservationSuperseded("automatic_step_superseded")
            raise ReadUnavailable("synthetic_read_unavailable", "Original read has no response")
        return reconcile(effect)

    runner.ports.reconcile = physical
    runner.activate_observation()
    first = runner.run_once()
    assert first.kind == "waiting" and first.retry_at == 105.25
    assert runner.state().disposition == "ready"
    assert runner.run_once().kind == "progress"
    assert reads == [sent[0].effect_id, sent[1].effect_id]
    assert runner.state().disposition == "waiting" and runner.state().wake_at == 105.25
    now[0] = 105.25
    again = runner.run_once()
    assert again.kind == "waiting" and again.retry_at == 115.25
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT started_at,deadline,retry_count FROM waits WHERE subject=?", (sent[0].subject,))) == (100.25, 700.25, 2)
        assert tx.one("SELECT status FROM effects WHERE id=?", (sent[0].effect_id,))[0] == "sent"
    fail[0] = False
    now[0] = 115.25
    assert runner.run_once().kind == "progress"
    assert runner.state().disposition == "quiescent"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT subject FROM waits")
        assert not tx.rows("SELECT id FROM attempts")


def test_prior_readback_never_rearms_the_original_write(tmp_path):
    scenario, runner, sent = sent_workspaces(tmp_path, count=1, execute=False)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    runner.activate_observation()
    assert runner.run_once().kind == "waiting"
    assert runner.state().disposition == "waiting" and runner.state().wake_at == 105.25
    runner.activate_observation()
    assert runner.state().wake_at == 105.25
    now[0] = 105.25
    assert runner.run_once().kind == "waiting"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (sent[0].effect_id,))) == ("sent", 1)
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
        assert not tx.rows("SELECT id FROM attempts")
    assert not scenario.port.writes()


def test_stop_cancels_the_pass_without_reading_or_reactivating(tmp_path, monkeypatch):
    scenario, runner, sent = sent_workspaces(tmp_path)
    runner.activate_observation()
    monkeypatch.setattr(runner.ports, "reconcile", lambda *a: pytest.fail("the old executor is fenced"))
    scenario.kernel.control("stop", "stop-observation")
    assert runner.state().disposition == "quiescent"
    assert runner.run_once().reason == "executor_fenced"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"


@pytest.mark.parametrize("kind,change", [("parent", "source"), ("final", "target")])
def test_approval_observation_executes_real_currentness_and_resync_checks(tmp_path, kind, change, request):
    if kind == "parent":
        scenario = admit_scenario(request.getfixturevalue("parent_at_approval"), tmp_path)
    else:
        scenario = admit_scenario(request.getfixturevalue("graph_final_at_approval"), tmp_path)
    assert scenario.run().kind == "approval"
    before = scenario.kernel.status(scenario.delivery.key)
    runner = scenario.runner()
    runner.activate_observation()
    if change == "source":
        from dataclasses import replace
        scenario.requirements = replace(scenario.requirements, parent=replace(scenario.requirements.parent, body="Changed actual source"))
    elif change == "target":
        scenario.port.external_remote_append(scenario.delivery.target_ref, {"target-advance.txt": "new target"}, "external-target")
    step = prepare_scripted_agent(scenario.kernel, runner.run_once())
    after = scenario.kernel.status(scenario.delivery.key)
    assert step.call is None
    if change == "source":
        assert "requeue_required" in after["gates"]
        assert after["budgets"] == before["budgets"]
        assert runner.state().disposition == "quiescent"
    else:
        assert step.kind == "progress" and after["resync"]["used"] == before["resync"]["used"] + 1
        assert after["phase"] != "approval"



@pytest.mark.parametrize("kind", [DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
@pytest.mark.parametrize("change", ["unchanged", "source", "target"])
def test_approval_consumer_preserves_budget_and_charges_only_target_resync(monkeypatch, kind, change):
    from dataclasses import replace
    from agent_run.core.operation_contract import Work
    from agent_run.model import Commit, Execution, PullRequest, World
    from support.repository_scope import insert
    from support.run_scoped_kernel import effect_runs

    # Persisted publication inputs for the real Core consumer. Physical source
    # refresh and Git append reach this consumer in the two Host stories above.
    base = Commit("a" * 40, "b" * 40)
    subject = "parent:1"
    with effect_runs(monkeypatch) as (db, kernels):
        kernel = kernels["A"]
        db.execute("UPDATE subjects SET kind=?,phase='approval',purpose='integration',"
            "publication_sha=?,publication_tree=?,integration_target=?,pr_number=11 WHERE run_id='A'",
            (kind, base.sha, base.tree, base.sha))
        insert(db, "budgets", {"run_id": "A", "subject": subject, "purpose": "integration",
            "window": 1, "development_limit": 3, "review_limit": 3, "review_required": 1,
            "developments_used": 1, "reviews_used": 2})
        insert(db, "resync_allowances", {"run_id": "A", "subject": subject, "generation": 1, "allowance": 3})
        insert(db, "integration_snapshots", {"run_id": "A", "subject": subject, "window": 1,
            "allowance_generation": 1, "source_ref": "refs/heads/work", "target_ref": "refs/heads/main",
            "source_sha": base.sha, "target_sha": base.sha, "open": 0})
        pr = PullRequest(11, "example/project", "refs/heads/work", "refs/heads/main", base, base,
            "Approved subject", "Frozen publication", actor="controlled-actor", head_repository="example/project")
        world = World("r", "g", "r", base, base, pr=pr)
        if change == "source":
            world = replace(world, parent_revision="new-requirements", revision="new-requirements")
        elif change == "target":
            target = Commit("c" * 40, "d" * 40)
            world = replace(world, target=target, ancestry=((base.sha, target.sha),))
        budgets = list(db.execute("SELECT * FROM budgets WHERE run_id='A'"))
        work = Work(Execution("A", "same-owner", 1), "observation", subject, False, False, None, None,
            approval_observed=True)
        with kernel.storage.transaction() as tx:
            step = kernel._advance(tx, work, world)
        assert step.call is None
        assert list(db.execute("SELECT * FROM budgets WHERE run_id='A'")) == budgets
        phase = db.execute("SELECT phase FROM subjects WHERE run_id='A'").fetchone()[0]
        used = db.execute("SELECT used FROM resync_allowances WHERE run_id='A'").fetchone()[0]
        if change == "source":
            assert step.kind == "paused" and phase == "approval" and used == 0
            assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == "requeue_required"
        elif change == "target":
            assert step.kind == "progress" and phase == "integration" and used == 1
        else:
            assert step.kind == "approval" and phase == "approval" and used == 0
        assert not db.execute("SELECT id FROM attempts WHERE run_id='A'").fetchall()
        assert not db.execute("SELECT id FROM effects WHERE run_id='A'").fetchall()


def test_observation_timeout_quiesces_without_consuming_or_renewing_the_intent(tmp_path):
    scenario, runner, sent = sent_workspaces(tmp_path, count=1)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    reads = []

    def unavailable(effect):
        reads.append(effect.effect_id)
        raise ReadUnavailable("synthetic_read_unavailable", "Same original outage")

    runner.ports.reconcile = unavailable
    runner.activate_observation()
    assert runner.run_once().retry_at == 105.25
    now[0] = 700.25
    assert runner.run_once().reason == "supervision_timeout"
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert reads == [sent[0].effect_id]
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (sent[0].effect_id,))) == ("sent", 1)
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)


def test_reactivation_cannot_capture_an_intent_created_after_the_pass(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)),
        (Issue(2, "Ticket", "Task", "OPEN", frozenset({"ready-for-agent"})),))
    scenario = admit_scenario(GraphScenario(tmp_path / "graph", source), tmp_path)
    runner = scenario.runner()
    runner.activate_observation()
    execute = runner.ports.execute
    sent = []

    def uncertain(effect, authorize):
        if effect.operation == "workspace":
            assert execute(effect, authorize).disposition == "target"
            sent.append(effect)
            return EffectResult("unknown")
        return execute(effect, authorize)

    runner.ports.execute = uncertain
    for _ in range(8):
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        assert step.call is None
        if sent:
            break
    assert len(sent) == 1 and runner.state().disposition == "quiescent"
    runner.ports.reconcile = lambda *a: pytest.fail("activation is a snapshot, not a refill")
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert prepare_scripted_agent(scenario.kernel, runner.run_once()).kind == "quiescent"


def test_temporary_unknown_host_readiness_does_not_consume_the_pass(tmp_path):
    scenario, runner, sent = sent_workspaces(tmp_path, count=1)
    runner.activate_observation()
    request = scenario.kernel.host_request("start")
    assert scenario.kernel.observe_host(request, HostObservation("unknown", request.unit_name)) == "unknown"
    assert runner.state().disposition == "quiescent"
    assert scenario.kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "start", usage_ready=True)) == "running"
    assert runner.state().disposition == "ready"
    assert runner.run_once().kind == "progress"
    assert runner.state().disposition == "quiescent"
