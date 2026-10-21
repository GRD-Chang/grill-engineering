"""Current publisher witnesses and original comment receipts bind remote writes."""

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import DeliveryKind, Kernel, KernelError
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.model import Effect, EffectResult
from agent_run_kernel.offline import Scenario


def publisher_scenario(path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    scenario = Scenario(path, DeliveryKind.PARENT_ONLY)
    scenario.spec = freeze_creation(scenario.spec, UserDefaultsStore(path / "personal.json"))
    scenario.kernel = Kernel.create(path / "frozen.sqlite3", scenario.spec)
    scenario.execution = scenario.kernel.claim_executor("publisher-owner")
    original = scenario.world
    monkeypatch.setattr(scenario, "world", lambda: replace(original(), publisher_actor="actual-runner",
                                                         publisher_identity={"source": "host"}))
    return scenario


def reach_phase(scenario: Scenario, phase: str) -> None:
    for _ in range(80):
        if scenario.kernel.status(scenario.delivery.key)["phase"] == phase:
            return
        scenario.step()
    raise AssertionError("delivery never reached " + phase)


def prepare_comment(scenario: Scenario) -> Effect:
    assert scenario.run().kind == "approval"
    scenario.kernel.control("approve", "original-approval", subject=scenario.delivery.key, world=scenario.world())
    reach_phase(scenario, "completion_comment")
    step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    assert step.effect is not None and step.effect.operation == "completion_comment"
    return step.effect


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
    reach_phase(scenario, "push")
    world = scenario.world()
    assert world.publisher_actor is not None and world.publisher_identity is None
    with pytest.raises(KernelError, match="publisher_identity_unproven"):
        scenario.kernel.advance(scenario.delivery.key, scenario.execution, world)


@pytest.mark.parametrize("changed", ["missing-actor", "missing-selection", "other-selection"])
def test_remote_intent_requires_the_observed_frozen_runner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    reach_phase(scenario, "push")
    world = scenario.world()
    if changed == "missing-actor":
        world = replace(world, publisher_actor=None)
    elif changed == "missing-selection":
        world = replace(world, publisher_identity=None)
    else:
        world = replace(world, publisher_identity={"source": "pat", "pat": {"env": "OTHER_TOKEN"}})
    before = scenario.kernel.history(scenario.delivery.key)
    with pytest.raises(KernelError, match="publisher_identity_unproven"):
        scenario.kernel.advance(scenario.delivery.key, scenario.execution, world)
    assert scenario.kernel.history(scenario.delivery.key) == before


def test_actual_publisher_is_frozen_and_rechecked_before_each_remote_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    reach_phase(scenario, "push")
    world = scenario.world()
    effect = scenario.kernel.advance(scenario.delivery.key, scenario.execution, world).effect
    assert effect is not None and effect.operation == "push"
    assert effect.actor == "actual-runner"
    changed = replace(world, publisher_actor="different-account")
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        scenario.kernel.dispatch(effect, scenario.execution, changed)
    assert scenario.kernel.dispatch(effect, scenario.execution, world)
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        scenario.kernel.guard_effect(effect, scenario.execution, changed)
    scenario.kernel.guard_effect(effect, scenario.execution, world)
    scenario.port.execute(effect)
    scenario.kernel.reconcile(effect, scenario.port.reconcile(effect), scenario.execution, world)
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, world).kind == "progress"
    with pytest.raises(KernelError, match="publisher_identity_changed"):
        scenario.kernel.advance(scenario.delivery.key, scenario.execution, replace(scenario.world(), publisher_actor="different-account"))
    with scenario.kernel.storage.transaction(write=False) as tx:
        local = tx.rows("SELECT actor FROM effects WHERE operation IN ('candidate','integrate','publication_commit')")
        assert local and all(row[0] is None for row in local)


@pytest.mark.parametrize("foreign", [False, True])
def test_close_inherits_only_the_confirmed_original_comment_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario)
    assert scenario.kernel.dispatch(comment, scenario.execution, scenario.world())
    intent = close_intent(comment)
    if foreign:
        intent["merge_sha"] = "another-merge"
    scenario.kernel.reconcile(comment, EffectResult("target", raw={"close_intent": intent}),
                              scenario.execution, scenario.world())
    if foreign:
        with pytest.raises(KernelError, match="foreign_close_intent"):
            scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        assert scenario.kernel.status(scenario.delivery.key)["phase"] == "completion_comment"
        return
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "progress"
    reopened = Kernel(scenario.kernel.storage.path)
    close = reopened.advance(scenario.delivery.key, scenario.execution, scenario.world()).effect
    assert close is not None and close.operation == "close"
    assert close.payload.get("close_intent") == intent
    assert close.actor == comment.actor
    assert close.effect_id != comment.effect_id


def test_same_actor_close_seen_before_first_dispatch_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario)
    assert scenario.kernel.dispatch(comment, scenario.execution, scenario.world())
    scenario.port.execute(comment)
    scenario.kernel.reconcile(comment, scenario.port.reconcile(comment), scenario.execution, scenario.world())
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "progress"
    step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
    close = step.effect
    assert close is not None and close.operation == "close"
    # Even the exact actor and prospective target cannot prove a request which
    # this Run has not dispatched. This is a separate external-store change.
    with sqlite3.connect(scenario.port.database) as db:
        db.execute("INSERT INTO closed VALUES (?,?,?,?)", (close.payload["issue"], close.effect_id,
                                                          close.actor, close.payload["merge_sha"]))
    scenario.handle(step)
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "paused"
    with scenario.kernel.storage.transaction(write=False) as tx:
        original = tx.one("SELECT status,attempts,receipt_id FROM effects WHERE id=?", (close.effect_id,))
        assert tuple(original) == ("prepared", 0, None)
    assert "write_conflict" in scenario.kernel.status(scenario.delivery.key)["gates"]
    assert not any(write["operation"] == "close" for write in scenario.port.writes())


def test_rearmed_close_settles_the_original_late_write_without_dispatching_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = publisher_scenario(tmp_path / "scenario", monkeypatch)
    comment = prepare_comment(scenario)
    assert scenario.kernel.dispatch(comment, scenario.execution, scenario.world())
    scenario.port.execute(comment)
    scenario.kernel.reconcile(comment, scenario.port.reconcile(comment), scenario.execution, scenario.world())
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "progress"
    close = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).effect
    assert close is not None and close.operation == "close"
    assert scenario.kernel.dispatch(close, scenario.execution, scenario.world())
    prior = scenario.port.reconcile(close)
    assert prior.disposition == "prior"
    assert scenario.kernel.reconcile(close, prior, scenario.execution, scenario.world()).kind == "progress"
    rearmed = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).effect
    assert rearmed is not None and rearmed.effect_id == close.effect_id
    assert rearmed.dispatch_serial == 2
    # The original dispatched request completes after its earlier prior read.
    scenario.port.execute(close)
    target = scenario.port.reconcile(rearmed)
    assert target.disposition == "target"
    assert scenario.kernel.reconcile(rearmed, target, scenario.execution, scenario.world()).kind == "progress"
    assert scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world()).kind == "progress"
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (close.effect_id,))) == ("confirmed", 1)
    assert sum(write["operation"] == "close" for write in scenario.port.writes()) == 1
