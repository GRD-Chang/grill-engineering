"""Notify actual formal promotion, rather than any passing Reviewer result."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_run import notification_outbox as outbox
from agent_run.prompt_resources import RESOURCE_KEYS
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import DeliveryKind, Kernel, RunSpec
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.notifications import NativeNotifications
from agent_run_kernel.offline import GraphScenario, Scenario
from test_kernel_requirements import independent_source


def _enabled_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
                      *, reviews: tuple[str, ...] = ("pass",)) -> Scenario:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", notifications={"enabled": False, "mode": "concise"})
    original_create = Kernel.create

    def create(_cls: type[Kernel], path: Path, spec: RunSpec, **kwargs: Any) -> Kernel:
        spec = freeze_creation(spec, defaults,
                               prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
        return original_create(path, replace(spec, language="en", inputs=inputs), **kwargs)

    source = independent_source()
    if kind == DeliveryKind.PARENT_ONLY:
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=()), issues=())
    else:
        first = source.issues[0]
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=(first.number,)), issues=(first,))
    # Freeze only input configuration. Workspace and local marker ownership are
    # produced by the real Core/effect path, never by a synthetic receipt.
    with monkeypatch.context() as patch:
        patch.setattr(Kernel, "create", classmethod(create))
        if kind == DeliveryKind.FINAL_RUN:
            # d847 builds and retains actual historical Ticket Completion.
            scenario = Scenario(tmp_path, kind, reviews=reviews)
            assert len(scenario.kernel.source_status()["completions"]) == 1
        else:
            scenario = GraphScenario(tmp_path, source, reviews=reviews if kind == DeliveryKind.PARENT_ONLY else ("pass",))
    return scenario


@pytest.mark.parametrize("kind", [DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
def test_formal_acceptance_notification_waits_for_real_promotion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, kind)
    sent: list[dict[str, Any]] = []

    def transport(_config: Any, rendered: dict[str, Any], _identity: str,
                  *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    def observed_acceptances() -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
        observer.close()
        assert observer.failure_reason is None
        return [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]

    # The real core first consumes a passing initial Reviewer, before its Git
    # integration and formal delivery promotion. That raw verdict is not yet
    # the overall accepted result a maintainer is being notified about.
    for _ in range(80):
        step = scenario.step()
        if step.call is not None and step.call.role.value == "review":
            break
        assert step.kind not in {"paused", "waiting", "complete", "approval"}
    else:
        pytest.fail("No initial Reviewer reached")
    assert observed_acceptances() == []
    assert scenario.step().kind == "progress"  # Consume that original raw.
    assert observed_acceptances() == []

    # Follow the actual shared delivery path through confirmed local Git
    # integration and, for Final Run, publication of its Run Branch.
    for _ in range(80):
        if scenario.kernel.status(scenario.delivery.key)["phase"] == "publish":
            break
        step = scenario.step()
        assert step.kind not in {"paused", "waiting", "complete", "approval"}
    else:
        pytest.fail("No formal publication boundary reached")
    accepted = observed_acceptances()
    assert len(accepted) == 1
    assert accepted[0]["outcome"] == "success"
    assert observed_acceptances() == accepted  # No new budget after reopening.

    assert scenario.run(approve=True).kind == "complete"
    assert observed_acceptances() == accepted


@pytest.mark.parametrize("changed_branch", ["source", "target"])
def test_observed_run_branch_write_does_not_promote_a_superseded_acceptance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_branch: str) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    issued = []
    execute = scenario.port.execute
    def capture(effect, authorize=None):
        if effect.operation == "run_branch":
            issued.append(effect)
        return execute(effect, authorize)
    monkeypatch.setattr(scenario.port, "execute", capture)
    for _ in range(100):
        step = scenario.step()
        if issued:
            effect = issued[0]
            break
        assert step.kind not in {"paused", "waiting", "complete", "approval"}
    else:
        pytest.fail("No actual Run Branch write reached")
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0] == "observed"
        assert not tx.rows("SELECT position FROM history WHERE kind='acceptance_promoted'")
    original_reviews = scenario.review_count
    ref = scenario.delivery.source_ref if changed_branch == "source" else scenario.delivery.target_ref
    filename = f"foreign-{changed_branch}.txt"
    foreign = scenario.port.external_remote_append(ref, {filename: "Independent retained work\n"}, "before-consume")
    assert scenario.step().kind == "progress"  # Settle the original write, not a fresh acceptance.
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0] == "confirmed"
        assert not tx.rows("SELECT position FROM history WHERE kind='acceptance_promoted'")

    def transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
    observer.close()
    assert observer.failure_reason is None
    assert not [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]
    assert scenario.run(approve=True).kind == "complete"
    assert scenario.review_count > original_reviews
    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
    observer.close()
    assert observer.failure_reason is None
    accepted = [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]
    assert len(accepted) == 1
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    assert scenario.port.git.run("show", f"{target.sha}:{filename}").stdout == "Independent retained work\n"
    scenario.port.git.run("merge-base", "--is-ancestor", foreign.sha, target.sha)
