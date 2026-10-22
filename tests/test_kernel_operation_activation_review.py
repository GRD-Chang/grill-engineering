"""Independent activation contracts through public admission and synthetic Git."""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel.model import EffectResult, Execution, HostObservation
from agent_run_kernel import DeliveryKind, Policy
from agent_run_kernel import executor
from agent_run_kernel.offline import GraphScenario, Scenario
from agent_run_kernel.operation_runner import ReadUnavailable
from test_kernel_requirements import independent_source
from test_kernel_operation_review_host import _NoNotifications


def gated_host(tmp_path):
    s = GraphScenario(tmp_path / "graph", independent_source())
    subjects = tuple(f"ticket:{n}" for n in s.requirements.parent.sub_issue_numbers)
    s.port.force_read["workspace"] = "third"
    for _ in range(16):
        assert s.step().call is None
        if all(s.kernel.status(sub)["gates"] == ["write_conflict"] for sub in subjects):
            break
    else:
        raise AssertionError("Original workspace intents did not reach conflict")
    originals = tuple(dict(s.kernel.status(sub)["pending_write"], subject=sub) for sub in subjects)
    s.port.force_read.clear()
    admit(s, tmp_path)
    return s, originals


def admit(s, tmp_path):
    k = s.kernel
    k.control("stop", "retire-fixture")
    k.confirm_executor_exit(s.execution)
    k.control("resume", "prepare-managed")
    req = k.admit_host("activation-review", kind="run", command=("fixture-native-host",),
                       environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert k.dispatch_host(req)
    group = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    assert k.observe_host(req, HostObservation("running", req.unit_name, "controlled-" + req.nonce,
        group, Path("/proc/sys/kernel/random/boot_id").read_text().strip(), os.getpid(), start,
        usage_ready=True)) == "running"
    s.execution = Execution(req.run_id, req.owner, req.epoch)
    return s


def state_of(s, original):
    with s.kernel.storage.transaction(write=False) as tx:
        return tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original["id"],)))


def test_readwait_defers_without_consuming_and_second_intent_is_observed(tmp_path, monkeypatch):
    s, originals = gated_host(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    runner = s.runner()
    actual = runner.ports.reconcile
    calls = []

    def physical(effect):
        calls.append(effect.effect_id)
        if len(calls) == 1:
            raise ReadUnavailable("github_read_failed", "independent initial read outage")
        return actual(effect)

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("observation grants no write"))
    runner.activate_observation()
    first = runner.run_once()
    assert (first.kind, first.retry_at) == ("waiting", 105.25), first
    assert runner.run_once().kind == "progress"
    assert calls == [item["id"] for item in originals]
    assert state_of(s, originals[0]) == ("sent", 1)
    assert state_of(s, originals[1]) == ("observed", 1)
    assert runner.state().disposition == "waiting"
    runner.activate_observation()
    assert runner.state().wake_at == 105.25
    now[0] = 105.25
    assert runner.run_once().kind == "progress"
    assert calls == [originals[0]["id"], originals[1]["id"], originals[0]["id"]]
    assert runner.state().disposition == "quiescent"


def test_original_sent_read_finishing_after_deadline_keeps_original_wait(tmp_path, monkeypatch):
    s, originals = gated_host(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    runner = s.runner()
    actual = runner.ports.reconcile
    calls = []

    def physical(effect):
        calls.append(effect.effect_id)
        if len(calls) == 1:
            raise ReadUnavailable("github_read_failed", "independent initial read outage")
        result = actual(effect)
        if effect.effect_id == originals[0]["id"]:
            now[0] = 701.25
        return result

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    runner.activate_observation()
    assert runner.run_once().retry_at == 105.25
    assert runner.run_once().kind == "progress"
    now[0] = 105.25
    late = runner.run_once()
    with s.kernel.storage.transaction(write=False) as tx:
        waits = [dict(row) for row in tx.rows("SELECT started_at,deadline FROM waits WHERE subject=?", (originals[0]["subject"],))]
    assert (late.kind, late.reason) == ("paused", "supervision_timeout"), {"step": late, "effect": state_of(s, originals[0]), "waits": waits}
    assert waits == [{"started_at": 100.25, "deadline": 700.25}]


def test_prior_observation_does_not_rearm_or_write_and_other_intent_finishes(tmp_path, monkeypatch):
    s, originals = gated_host(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    runner = s.runner()
    actual = runner.ports.reconcile
    calls = []

    def physical(effect):
        calls.append(effect.effect_id)
        return EffectResult("prior") if effect.effect_id == originals[0]["id"] else actual(effect)

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("prior read grants no write"))
    runner.activate_observation()
    assert runner.run_once().kind == "waiting"
    assert runner.run_once().kind == "progress"
    assert state_of(s, originals[0]) == ("sent", 1)
    assert state_of(s, originals[1]) == ("observed", 1)
    assert calls == [item["id"] for item in originals]
    assert "write_conflict" in s.kernel.status(originals[0]["subject"])["gates"]


def test_approval_source_cas_is_deferred_without_consuming_the_observation(tmp_path, monkeypatch):
    s = admit(Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY), tmp_path)
    assert s.run().kind == "approval"
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    runner = s.runner()
    actual = runner.ports.delivery
    reads = []
    original_budget = s.kernel.status(s.delivery.key)["budgets"]

    def physical(request):
        facts = actual(request)
        reads.append(request)
        if len(reads) == 1:
            # Another normal reader commits changed source metadata while the
            # delivery read is in flight. For a Parent without Tickets, order
            # reliability does not change accepted requirement/graph identity.
            s.requirements = replace(s.requirements, parent=replace(s.requirements.parent,
                sub_issue_order_reliable=False))
            s.kernel.refresh(lambda: s.requirements)
        return facts

    monkeypatch.setattr(runner.ports, "delivery", physical)
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("CAS recovery grants no write"))
    runner.activate_observation()
    first = runner.run_once()
    assert (first.kind, first.retry_at) == ("waiting", 105.25), {"step": first, "state": runner.state(), "status": s.kernel.status(s.delivery.key)}
    runner.activate_observation()
    assert runner.state().disposition == "waiting"
    assert runner.state().wake_at == 105.25
    assert len(reads) == 1
    now[0] = 105.25
    second = runner.run_once()
    assert second.kind == "approval"
    assert len(reads) == 2
    assert s.kernel.status(s.delivery.key)["gates"] == []
    assert s.kernel.status(s.delivery.key)["budgets"] == original_budget
    assert runner.state().disposition == "quiescent"


def test_sent_unknown_read_after_public_stop_adds_no_new_gate(tmp_path, monkeypatch):
    s, originals = gated_host(tmp_path)
    runner = s.runner()

    def physical(effect):
        s.kernel.control("stop", "stop-during-original-read")
        return EffectResult("unknown")

    monkeypatch.setattr(runner.ports, "reconcile", physical)
    runner.activate_observation()
    step = runner.run_once()
    assert (step.kind, step.reason) == ("paused", "executor_fenced")
    assert state_of(s, originals[0]) == ("sent", 1)
    assert "write_result_unknown" not in s.kernel.status(originals[0]["subject"])["gates"]


@pytest.mark.parametrize("change", ["source", "target"])
def test_approval_initial_observation_keeps_original_change_gates(tmp_path, monkeypatch, change):
    s = admit(Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY,
                       policy=Policy(resync_limit=0)), tmp_path)
    assert s.run().kind == "approval"
    budget = s.kernel.status(s.delivery.key)["budgets"]
    runner = s.runner()
    runner.activate_observation()
    if change == "source":
        s.requirements = replace(s.requirements, parent=replace(s.requirements.parent,
            body=s.requirements.parent.body + "\nA new requirement"))
        expected_gate = "requeue_required"
    else:
        s.port.external_remote_append(s.delivery.target_ref, {"external.txt": "Unrelated external work"}, "approval-change")
        expected_gate = "integration_resync_exhausted"
    monkeypatch.setattr(runner.ports, "execute", lambda *a: pytest.fail("approval observation cannot write"))
    step = runner.run_once()
    assert step.kind == "paused"
    assert expected_gate in s.kernel.status(s.delivery.key)["gates"]
    assert s.kernel.status(s.delivery.key)["budgets"] == budget
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"


def test_formal_host_readwait_observes_second_before_retrying_first(tmp_path, monkeypatch):
    s, originals = gated_host(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    ports = s.runner().ports
    actual = ports.reconcile
    calls = []

    def physical(effect):
        calls.append(effect.effect_id)
        assert len(calls) <= 3, "Formal Host must terminate after its finite observations"
        if len(calls) == 1:
            raise ReadUnavailable("github_read_failed", "independent Host read outage")
        if len(calls) == 2:
            assert effect.effect_id == originals[1]["id"]
            now[0] = 105.25
        else:
            assert effect.effect_id == originals[0]["id"]
            with s.kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.one("SELECT started_at,deadline FROM waits WHERE subject=?", (effect.subject,))) == (100.25, 700.25)
        return actual(effect)

    monkeypatch.setattr(ports, "reconcile", physical)
    monkeypatch.setattr(ports, "execute", lambda *a: pytest.fail("Host observation grants no write"))
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", _NoNotifications)
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex",
                        lambda *a, **kw: pytest.fail("Host observation allocates no Agent"))
    assert executor.run(s.kernel, s.execution, {}) == 0
    assert calls == [originals[0]["id"], originals[1]["id"], originals[0]["id"]]
    assert all(state_of(s, item) == ("observed", 1) for item in originals)


def test_formal_host_approval_cas_retries_without_consuming_its_observation(tmp_path, monkeypatch):
    s = admit(Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY), tmp_path)
    assert s.run().kind == "approval"
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    ports = s.runner().ports
    actual = ports.delivery
    reads, sleeps = [], []

    def physical(request):
        reads.append(request)
        assert len(reads) <= 2, "One CAS invalidation must not become endless Host polling"
        facts = actual(request)
        if len(reads) == 1:
            s.requirements = replace(s.requirements, parent=replace(s.requirements.parent,
                sub_issue_order_reliable=False))
            s.kernel.refresh(lambda: s.requirements)
        return facts

    def idle(delay):
        sleeps.append(delay)
        assert len(sleeps) == 1
        with s.kernel.storage.transaction(write=False) as tx:
            assert tuple(tx.one("SELECT started_at,deadline,next_retry_at FROM waits WHERE subject=?", (s.delivery.key,))) == (100.25, 700.25, 105.25)
        now[0] = 105.25

    monkeypatch.setattr(ports, "delivery", physical)
    monkeypatch.setattr(ports, "execute", lambda *a: pytest.fail("CAS recovery grants no write"))
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    monkeypatch.setattr(executor.time, "sleep", idle)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", _NoNotifications)
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex",
                        lambda *a, **kw: pytest.fail("CAS recovery allocates no Agent"))
    assert executor.run(s.kernel, s.execution, {}) == 0
    assert len(reads) == 2 and len(sleeps) == 1
    assert s.kernel.status(s.delivery.key)["phase"] == "approval"
    assert s.kernel.status(s.delivery.key)["gates"] == []
