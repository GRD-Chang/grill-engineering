"""Notify actual formal promotion, rather than any passing Reviewer result."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import DeliveryKind, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.notifications.sender import NativeNotifications
from support.offline import GraphScenario, Scenario
from test_kernel_requirements import independent_source


def _enabled_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
                      *, reviews: tuple[str, ...] = ("pass",), mode: str = "concise", language: str = "en") -> Scenario:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language=language, notifications={"enabled": False, "mode": mode})
    original_create = Kernel.create

    def create(_cls: type[Kernel], path: Path, spec: RunSpec, **kwargs: Any) -> Kernel:
        spec = freeze_creation(spec, defaults,
                               prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={"enabled": True, "mode": mode,
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
        return original_create(path, replace(spec, language=language, inputs=inputs), **kwargs)

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


@pytest.mark.parametrize("mode,approval", [("detailed", True), ("detailed", False), ("concise", True)],
                         ids=["detailed-approval", "detailed-checks-pending", "concise-approval"])
def test_healthy_final_pr_and_approval_in_one_observation_share_only_the_approval_card(
    tmp_path, monkeypatch, mode, approval,
):
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, mode=mode)
    if approval:
        assert scenario.run().kind == "approval"
    else:
        # Stop after the genuine PR receipt is consumed, before checks grant
        # approval. Creation alone remains a useful detailed-mode milestone.
        for _ in range(80):
            step = scenario.step()
            status = scenario.kernel.status(scenario.delivery.key)
            if status["phase"] == "checks":
                break
            assert step.kind not in {"paused", "waiting", "approval", "complete"}
        else:
            raise AssertionError("Normal producer did not consume its Final PR creation")
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None
    from agent_run.queries.run import read
    # Discard mutable notification files from the read-only product comparison.
    database_before = scenario.kernel.storage.path.read_bytes()
    sent = []

    def transport(_config, rendered, _identity, *_args, **_kwargs):
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
    try:
        observer.close()
        assert observer.failure_reason is None
    finally:
        observer.close(drain=False)
    journal = outbox.read_notifications(tmp_path, scenario.spec.run_id)
    assert journal.get("unavailable_reason") is None
    records = journal["records"]
    created = [item for item in records if item["kind"] == "pr_created"]
    assert len([item for item in created if item["outcome"] != "cancelled"]) == int(mode == "detailed" and not approval)
    if approval and mode == "detailed":
        assert len(created) == 1 and created[0]["outcome"] == "cancelled" and created[0]["attempts"] == 0
        assert created[0]["reason"] == "included in current Final PR approval"
    if approval:
        assert not any(item["header"]["title"]["content"] == f"Final PR #{pr.number} created" for item in sent)
    approval_cards = [item for item in sent if item["header"]["title"]["content"] == "Please approve the PR merge"]
    assert bool(approval_cards) is approval
    if approval:
        assert any(f"https://github.com/example/project/pull/{pr.number}" in str(item) for item in approval_cards)
    assert scenario.kernel.storage.path.read_bytes() == database_before
    assert read(scenario.kernel.storage.path, "status").data["subjects"][0]["phase"] == ("approval" if approval else "checks")


@pytest.mark.parametrize("language,prefix", [("en", "Feishu notifications unavailable; development continues"),
                                            ("zh", "飞书通知不可用（开发继续）")])
def test_notification_degradation_uses_frozen_language_and_original_technical_reason(
    tmp_path, monkeypatch, capsys, language, prefix,
):
    import agent_run.notifications.sender as sender

    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, language=language)
    # A later personal preference cannot change the Run's frozen language.
    UserDefaultsStore(tmp_path / "defaults.json").configure(language="en" if language == "zh" else "zh")
    reason = "Original技术原因: EIO / local synthetic failure"

    def unavailable(*_args, **_kwargs):
        raise OSError(reason)

    monkeypatch.setattr(sender, "read_boundaries", unavailable)
    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path,
                                   transport=lambda *_a, **_kw: {"outcome": "success", "reason": None})
    try:
        assert observer.failure_reason == reason
        error = capsys.readouterr().err
        assert prefix in error and reason in error
    finally:
        observer.close(drain=False)
    document = outbox.read_notifications(tmp_path, scenario.spec.run_id)
    assert document["unavailable_reason"] == reason


@pytest.mark.parametrize("change", ["none", "subject", "generation", "url", "missing-pr", "not-current",
                                     "exhausted", "unknown", "sending", "already-attempted", "wrong-kind", "invalid-pr"], ids=str)
def test_final_pr_coalescing_requires_an_exact_current_approval_and_an_unsent_creation(change):
    from copy import deepcopy
    from agent_run.notifications.envelope import event_envelope

    state = {"run_id": "original", "repository": "example/project", "language": "en", "parent": {"number": 1}}
    url = "https://github.com/example/project/pull/2"
    creation = event_envelope(state, "pr_created", 2, "Created", work_subject="parent:1", generation=1, url=url)
    approval = event_envelope(state, "boundary", "approval", "Approve", current=True,
                              work_subject="parent:1", generation=1, approval_pr=2, url=url)
    if change == "subject":
        approval["work_subject"] = "other-subject"
    elif change == "generation":
        approval["generation"] = 2
    elif change == "url":
        approval["url"] = "https://github.com/foreign/project/pull/2"
    elif change == "missing-pr":
        approval.pop("approval_pr")
    elif change == "not-current":
        approval["current"] = False
    elif change == "wrong-kind":
        approval["kind"] = "stage_end"
    elif change == "invalid-pr":
        approval["approval_pr"] = "2"
    document = {"pending": [], "records": []}
    outbox.enqueue(document, creation, 100.25)
    outbox.enqueue(document, approval, 100.25)
    first, second = document["pending"]
    if change == "exhausted":
        second["exhausted"] = True
    elif change in {"unknown", "sending", "already-attempted"}:
        first["attempts"] = 1
        first["outcome"] = "pending" if change == "already-attempted" else change
        first["attempt_history"] = [{"number": 1, "outcome": first["outcome"], "started_at": 100.5}]
    before = deepcopy(document)
    NativeNotifications._coalesce_final_pr(document)
    if change == "none":
        assert document["pending"] == [before["pending"][1]]
        record, = document["records"]
        assert (record["id"], record["attempts"], record["outcome"]) == (creation["id"], 0, "cancelled")
        assert record["deadline_at"] == before["pending"][0]["deadline_at"]
    else:
        assert document == before


def test_healthy_multi_page_observation_does_not_repeat_pr_creation_after_approval_was_sent(tmp_path, monkeypatch):
    import threading
    import time

    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, mode="detailed")
    sent = []

    def transport(_config, rendered, _identity, *_args, **_kwargs):
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
    try:
        assert scenario.run().kind == "approval"
        wake = observer._wake_pending
        approval_retired = []

        def wake_after_approval(document, serial):
            wake(document, serial)
            if threading.current_thread() is observer.thread or approval_retired:
                return
            # Force the legitimate sender to finish the important approval
            # before this same read observation reaches later history pages.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                records = outbox.read_notifications(tmp_path, scenario.spec.run_id).get("records", [])
                if any(item["kind"] == "boundary" and item["outcome"] == "success" for item in records):
                    approval_retired.append(True)
                    return
                time.sleep(.005)
            raise AssertionError("The real local sender did not finish its approval card")

        monkeypatch.setattr(observer, "_wake_pending", wake_after_approval)
        observer.observe_committed()
        observer.close()
        assert approval_retired == [True]
        assert observer.failure_reason is None
        titles = [item["header"]["title"]["content"] for item in sent]
        assert titles.count("Please approve the PR merge") == 1
        assert not any(title.startswith("Final PR #") and title.endswith(" created") for title in titles)
        records = outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
        assert not any(item["kind"] == "pr_created" and item["attempts"] for item in records)
    finally:
        observer.close(drain=False)
