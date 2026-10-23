"""Current publisher witnesses and original comment receipts bind remote writes."""

from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import DeliveryKind
from agent_run.core.kernel import Kernel
from agent_run.errors import GitHubReadError, KernelError
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.config.creation import freeze_creation
from agent_run.model import Effect, EffectResult
from support.offline import Scenario
from support.offline_api import SyntheticGitHub
from test_kernel_remote_effects import EffectBoundary, effect_boundary


def publisher_scenario(path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    # Declare the synthetic account before any Core observation or API write.
    monkeypatch.setattr("support.offline.SyntheticGitHub", partial(SyntheticGitHub, actor="actual-runner"))
    scenario = Scenario(path, DeliveryKind.PARENT_ONLY)
    remote = scenario.spec.inputs.repository_remote
    frozen = freeze_creation(scenario.spec, UserDefaultsStore(path / "personal.json"))
    scenario.spec = replace(frozen, inputs=replace(frozen.inputs, repository_remote=remote))
    scenario.kernel = Kernel.create(path / "frozen.sqlite3", scenario.spec, requirements=scenario.requirements)
    scenario.execution = scenario.kernel.claim_executor("publisher-owner")
    scenario.publisher = {"actor": "actual-runner", "identity": {"source": "host"}}
    make_ports = scenario.make_ports

    def selected(binding):
        ports = make_ports(binding)
        delivery = ports.delivery
        ports.delivery = lambda request: replace(delivery(request), publisher_actor=scenario.publisher["actor"],
            publisher_identity=scenario.publisher["identity"])
        return ports

    monkeypatch.setattr(scenario, "make_ports", selected)
    return scenario


def reach_phase(scenario: Scenario, phase: str) -> None:
    for _ in range(80):
        if scenario.kernel.status(scenario.delivery.key)["phase"] == phase:
            return
        scenario.step()
    raise AssertionError("delivery never reached " + phase)


def capture_effect(scenario, monkeypatch, operation):
    with effect_boundary(scenario, monkeypatch, operation):
        with pytest.raises(EffectBoundary) as boundary:
            scenario.run()
    return boundary.value


def prepare_comment(scenario: Scenario, monkeypatch) -> Effect:
    assert scenario.run().kind == "approval"
    scenario.kernel.control("approve", "original-approval", subject=scenario.delivery.key, world=scenario.world())
    reach_phase(scenario, "completion_comment")
    effect = capture_effect(scenario, monkeypatch, "completion_comment").effect
    assert effect.operation == "completion_comment"
    return effect


def close_intent(effect: Effect) -> dict:
    return {"run_id": effect.execution.run_id, "subject": effect.subject, "issue": effect.payload["issue"],
            "merge_sha": effect.payload["merge_sha"], "actor": effect.actor, "pr_number": effect.pr_number,
            "kind": effect.payload["kind"], "baseline_event_id": 41, "effect_id": effect.effect_id,
            "comment_id": 52, "comment_created_at": "2026-10-21T00:00:00Z", "comment_body": "Exact original comment"}


def test_unconfigured_run_cannot_publish_with_two_missing_identity_selections(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    scenario.spec = replace(scenario.spec, inputs=None)
    scenario.kernel = Kernel.create(tmp_path / "unconfigured.sqlite3", scenario.spec)
    scenario.execution = scenario.kernel.claim_executor("unconfigured-owner")
    # A deliberately unconfigured input is a publisher-validation unit, not
    # a managed native Run. The runner rejects its missing creation binding.
    from agent_run.model import World
    world = World("parent", "graph", "subject", scenario.initial,
                  publisher_actor="offline-publisher", publisher_identity=None)
    assert world.publisher_actor is not None and world.publisher_identity is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs")
        with pytest.raises(KernelError, match="publisher_identity_unproven"):
            scenario.kernel._publisher_actor(tx, run, world)


@pytest.mark.parametrize("changed", ["missing-actor", "missing-selection", "other-selection"])
def test_remote_intent_requires_the_observed_frozen_runner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    reach_phase(scenario, "push")
    if changed == "missing-actor":
        scenario.publisher["actor"] = None
    elif changed == "missing-selection":
        scenario.publisher["identity"] = None
    else:
        scenario.publisher["identity"] = {"source": "pat", "pat": {"env": "OTHER_TOKEN"}}
    before = scenario.kernel.history(scenario.delivery.key)
    with pytest.raises(KernelError, match="publisher_identity_unproven"):
        scenario.runner().run_once()
    assert scenario.kernel.history(scenario.delivery.key) == before


def test_actual_publisher_is_frozen_and_rechecked_before_each_remote_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    reach_phase(scenario, "push")
    boundary = capture_effect(scenario, monkeypatch, "push")
    effect = boundary.effect
    assert effect.operation == "push"
    assert effect.actor == "actual-runner"
    scenario.publisher["actor"] = "different-account"
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        boundary.authorize.prepare()()
    scenario.publisher["actor"] = "actual-runner"
    boundary.authorize.prepare()()
    scenario.publisher["actor"] = "different-account"
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        boundary.authorize.prepare()()
    scenario.publisher["actor"] = "actual-runner"
    boundary.authorize.prepare()()
    scenario.port.execute(effect, boundary.authorize)
    assert scenario.runner().run_once().kind == "progress"
    assert scenario.runner().run_once().kind == "progress"
    scenario.publisher["actor"] = "different-account"
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        scenario.runner().run_once()
    with scenario.kernel.storage.transaction(write=False) as tx:
        local = tx.rows("SELECT actor FROM effects WHERE operation IN ('candidate','integrate','publication_commit')")
        assert local and all(row[0] is None for row in local)


@pytest.mark.parametrize("foreign", [False, True])
def test_close_inherits_only_the_confirmed_original_comment_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario, monkeypatch)
    intent = close_intent(comment)
    if foreign:
        intent["merge_sha"] = "another-merge"
    make_ports = scenario.make_ports

    def comment_receipt(binding):
        ports = make_ports(binding)
        execute = ports.execute

        def written(effect, authorize):
            if effect.operation == "completion_comment":
                assert effect == comment
                authorize.prepare()()
                return EffectResult("target", raw={"close_intent": intent})
            return execute(effect, authorize)

        ports.execute = written
        return ports

    with monkeypatch.context() as receipt_patch:
        receipt_patch.setattr(scenario, "make_ports", comment_receipt)
        assert scenario.runner().run_once().kind == "progress"
    if foreign:
        with pytest.raises(KernelError, match="foreign_close_intent"):
            scenario.runner().run_once()
        assert scenario.kernel.status(scenario.delivery.key)["phase"] == "completion_comment"
        return
    assert scenario.runner().run_once().kind == "progress"
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    close = capture_effect(scenario, monkeypatch, "close").effect
    assert close.operation == "close"
    assert close.payload.get("close_intent") == intent
    assert close.actor == comment.actor
    assert close.effect_id != comment.effect_id


def test_same_actor_close_seen_before_first_dispatch_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario, monkeypatch)
    assert scenario.runner().run_once().kind == "progress"
    assert scenario.runner().run_once().kind == "progress"
    close = capture_effect(scenario, monkeypatch, "close").effect
    assert close.operation == "close"
    # Even the exact actor and prospective target cannot prove a request which
    # this Run has not dispatched. This is a separate external-store change.
    scenario.port.objects.set_state(close.payload["issue"], "closed", close.actor)
    assert scenario.runner().run_once().kind == "paused"
    with scenario.kernel.storage.transaction(write=False) as tx:
        original = tx.one("SELECT status,attempts,receipt_id FROM effects WHERE id=?", (close.effect_id,))
        assert tuple(original) == ("prepared", 0, None)
    assert "write_conflict" in scenario.kernel.status(scenario.delivery.key)["gates"]
    assert not any(write["operation"] == "close" for write in scenario.port.writes())


def test_rearmed_close_settles_the_original_late_write_without_dispatching_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario, monkeypatch)
    assert scenario.runner().run_once().kind == "progress"
    assert scenario.runner().run_once().kind == "progress"
    boundary = capture_effect(scenario, monkeypatch, "close")
    close = boundary.effect
    assert close.operation == "close"

    def original_state():
        with scenario.kernel.storage.transaction(write=False) as tx:
            return tuple(tx.one("SELECT status,attempts,receipt_id FROM effects WHERE id=?", (close.effect_id,)))

    assert original_state() == ("prepared", 0, None)
    physical_write = SyntheticGitHub.write
    queued = []

    def queue_original_close(provider, endpoint, method, body):
        if endpoint == f"repos/{close.repository}/issues/{close.payload['issue']}" and method == "PATCH":
            # Queue the actual request only after typed final admission. Retain
            # its real provider/callback so later server completion produces
            # the original raw transition and write audit, not a new grant.
            queued.append((provider, endpoint, method, dict(body)))
            raise GitHubReadError("write_response_unavailable", "Original close request remains in flight")
        return physical_write(provider, endpoint, method, body)

    with monkeypatch.context() as in_flight:
        in_flight.setattr(SyntheticGitHub, "write", queue_original_close)
        with pytest.raises(ReadUnavailable, match="Original close request remains in flight"):
            scenario.port.execute(close, boundary.authorize)
    assert len(queued) == 1
    assert original_state() == ("sent", 1, None)
    assert not [write for write in scenario.port.writes() if write["operation"] == "close"]
    prior = scenario.port.reconcile(close)
    assert prior.disposition == "prior"
    assert scenario.runner().run_once().kind == "progress"
    rearmed = capture_effect(scenario, monkeypatch, "close").effect
    assert rearmed.effect_id == close.effect_id
    assert rearmed.dispatch_serial == 2
    assert original_state() == ("prepared", 1, None)
    # The original dispatched request completes after its earlier prior read.
    provider, endpoint, method, body = queued[0]
    assert physical_write(provider, endpoint, method, body).returncode == 0
    target = scenario.port.reconcile(rearmed)
    assert target.disposition == "target"
    assert scenario.runner().run_once().kind == "progress"
    observed = original_state()
    assert observed[0:2] == ("observed", 1) and observed[2] is not None
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "close"
    assert scenario.runner().run_once().kind == "progress"
    assert original_state() == ("confirmed", 1, observed[2])
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (close.effect_id,))) == ("confirmed", 1)
    assert sum(write["operation"] == "close" for write in scenario.port.writes()) == 1
