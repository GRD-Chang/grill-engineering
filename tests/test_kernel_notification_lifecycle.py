"""Notify actual formal promotion, rather than any passing Reviewer result."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
from agent_run.model import DeliveryKind, Role
from agent_run.notifications.sender import NativeNotifications
from agent_run.notifications.envelope import event_copy, event_envelope
from agent_run.notifications.event_projection import event_state
from agent_run.config.defaults import UserDefaultsStore
from support.notification import _enabled_scenario, observe_and_wait, recorded_agents


def test_acceptance_and_terminal_notifications_follow_committed_delivery_boundaries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY)
    now = [time.time()]
    scenario.kernel.clock = lambda: now[0]

    def started(call):
        seconds = {Role.DEVELOPMENT: 2, Role.REVIEW: 3, Role.PUBLICATION: 5}[call.role]
        if call.role != Role.PUBLICATION:
            scenario.kernel.observe_worker_activity(call, "start", active=True, observed_at=now[0] + 1, evidence={"type": "turn.started"})
            scenario.kernel.observe_worker_activity(call, "end", active=False, observed_at=now[0] + 1 + seconds, evidence={"type": "turn.completed"})
        now[0] += seconds + 2

    recorded_agents(scenario, monkeypatch, started=started, activity=False)
    captured = []
    sent: list[dict[str, Any]] = []

    def transport(_config: Any, rendered: dict[str, Any], _identity: str,
                  *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered)
        active, = [item["event"] for item in outbox.read_notifications(tmp_path, scenario.spec.run_id)["pending"]
                   if item["outcome"] == "sending"]
        captured.append(active)
        return {"outcome": "success", "reason": None}

    def observe(kind: str = "acceptance_passed") -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        return [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == kind]

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
    assert observe() == []
    assert scenario.step().kind == "progress"  # Consume that original raw.
    assert observe() == []

    # Follow real local integration through formal delivery promotion.
    for _ in range(80):
        if scenario.kernel.status(scenario.delivery.key)["phase"] == "publish":
            break
        step = scenario.step()
        assert step.kind not in {"paused", "waiting", "complete", "approval"}
    else:
        pytest.fail("No formal publication boundary reached")
    accepted = observe()
    assert len(accepted) == 1
    assert accepted[0]["outcome"] == "success"
    assert observe() == accepted  # No new budget after reopening.

    # The same real delivery now verifies close, cleanup and completion.
    closed = []
    original_execute = scenario.port.execute

    def execute(effect, authorize=None):
        result = original_execute(effect, authorize)
        if effect.subject == scenario.delivery.key and effect.operation == "close":
            assert observe("boundary") == []  # Remote close is not yet consumed.
            closed.append(effect)
        return result

    monkeypatch.setattr(scenario.port, "execute", execute)
    for _ in range(100):
        step = scenario.step()
        if step.kind == "approval":
            scenario.kernel.control("approve", "original-approval", subject=scenario.delivery.key, world=scenario.world())
            continue
        if closed:
            break
        assert step.kind not in {"paused", "waiting", "complete"}
    else:
        pytest.fail("Original close operation not reached")
    assert scenario.step().kind == "progress"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT state FROM resource_cleanup WHERE subject=?", (scenario.delivery.key,))[0] != "complete"
        assert tx.one("SELECT count(*) FROM effects WHERE subject=? AND operation='close' AND status='confirmed'", (scenario.delivery.key,))[0] == 1
    pending = observe("boundary")
    assert len(pending) == 1
    assert sent[-1]["header"]["template"] == "yellow"
    assert sent[-1]["header"]["title"]["content"] == event_copy({"language": "en"}, "cleanup")
    assert observe("boundary") == pending

    assert scenario.run(approve=True).kind == "complete"
    completed = observe("boundary")
    assert len(completed) == 2 and completed[0] == pending[0]
    assert completed[1]["id"] != completed[0]["id"]
    assert sent[-1]["header"]["template"] == "green"
    assert sent[-1]["header"]["title"]["content"] == event_copy({"language": "en"}, "completed")
    assert "/pull/" in str(sent[-1])
    assert observe("boundary") == completed
    assert observe() == accepted

    with scenario.kernel.storage.transaction(write=False) as tx:
        close = tx.one("SELECT e.id,e.receipt_id FROM effects e JOIN history h ON h.identity=e.id AND h.material_id=e.receipt_id "
                       "WHERE e.operation='close' AND e.status='confirmed' AND h.kind='effect_consumed'")
        state = event_state(tx)
    terminal, = [event for event in captured if event["id"] == completed[1]["id"]]
    assert terminal["terminal"] and terminal["status_code"] == "completed"
    assert terminal["id"] == event_envelope(state, "boundary", ["completed", close["id"], close["receipt_id"]], "")["id"]
    assert terminal["execution_timing_complete"] is False and terminal["total_seconds"] is None
    assert terminal["recorded_execution_seconds"] == 5
    assert terminal["effort"]["development"]["execution_seconds"] == 2
    assert terminal["effort"]["review"]["execution_seconds"] == 3
    assert terminal["effort"]["development"]["rounds"] == terminal["effort"]["review"]["rounds"] == 1


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

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
    observer.close()
    assert observer.failure_reason is None
    assert not [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]
    assert scenario.run(approve=True).kind == "complete"
    assert scenario.review_count > original_reviews
    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
    observer.close()
    assert observer.failure_reason is None
    accepted = [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]
    assert len(accepted) == 1
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    assert scenario.port.git.run("show", f"{target.sha}:{filename}").stdout == "Independent retained work\n"
    scenario.port.git.run("merge-base", "--is-ancestor", foreign.sha, target.sha)


@pytest.mark.parametrize("approval", [True, False],
                         ids=["canonical-case-approval", "canonical-case-checks-pending"])
def test_healthy_final_pr_and_approval_in_one_observation_share_only_the_approval_card(
    tmp_path, monkeypatch, approval,
):
    from support.offline_api import SyntheticGitHub

    original_pr = SyntheticGitHub._with_refs

    def canonical_pr(value, refs):
        # Preserve canonical provider spelling through the actual PR receipt.
        response = original_pr(value, refs)
        for field in ("head", "base"):
            response[field]["repo"]["full_name"] = response[field]["repo"]["full_name"].upper()
        return response

    monkeypatch.setattr(SyntheticGitHub, "_with_refs", staticmethod(canonical_pr))
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, mode="detailed")
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
    assert pr.repository == "EXAMPLE/PROJECT"
    from agent_run.queries.run import read
    # Discard mutable notification files from the read-only product comparison.
    database_before = scenario.kernel.storage.path.read_bytes()
    sent = []

    def transport(_config, rendered, _identity, *_args, **_kwargs):
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
    try:
        observer.close()
        assert observer.failure_reason is None
    finally:
        observer.close(drain=False)
    journal = outbox.read_notifications(tmp_path, scenario.spec.run_id)
    assert journal.get("unavailable_reason") is None
    records = journal["records"]
    created = [item for item in records if item["kind"] == "pr_created"]
    assert len([item for item in created if item["outcome"] != "cancelled"]) == int(not approval)
    if approval:
        assert len(created) == 1 and created[0]["outcome"] == "cancelled" and created[0]["attempts"] == 0
        assert created[0]["reason"] == "included in current Final PR approval"
    if approval:
        assert not any(item["header"]["title"]["content"] == f"Final PR #{pr.number} created" for item in sent)
    approval_cards = [item for item in sent if item["header"]["title"]["content"] == "Please approve the PR merge"]
    assert bool(approval_cards) is approval
    if approval:
        assert any(f"https://github.com/example/project/pull/{pr.number}" in str(item) for item in approval_cards)
    assert scenario.kernel.storage.path.read_bytes() == database_before
    assert read(scenario.kernel.storage.path, 'status', storage=scenario.kernel.storage).data["subjects"][0]["phase"] == ("approval" if approval else "checks")


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
    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=lambda *_a, **_kw: {'outcome': 'success', 'reason': None}, storage=scenario.kernel.storage)
    try:
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason == reason, timeout=3)
        observer.close(drain=False)
        error = capsys.readouterr().err
        assert prefix in error and reason in error
    finally:
        observer.close(drain=False)
    document = outbox.read_notifications(tmp_path, scenario.spec.run_id)
    assert document["unavailable_reason"] == reason


@pytest.mark.parametrize("change", ["none", "subject", "generation", "url", "missing-pr", "not-current",
                                     "exhausted", "unknown", "sending", "already-attempted", "wrong-kind", "invalid-pr", "repo-case", "foreign-host",
                                     "credentials", "query", "fragment", "bad-repository", "wrong-route",
                                     "noncanonical-number", "wrong-scheme", "number-coherence"], ids=str)
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
    if change == "repo-case":
        creation["url"] = "https://github.com/EXAMPLE/PROJECT/pull/2"
    elif change == "foreign-host":
        creation["url"] = "https://other.example/example/project/pull/2"
    elif change in {"credentials", "query", "fragment", "bad-repository", "wrong-route",
                    "noncanonical-number", "wrong-scheme"}:
        altered = {"credentials": "https://user@github.com/example/project/pull/2", "query": url + "?x=1",
                   "fragment": url + "#issuecomment-1", "bad-repository": "https://github.com/../project/pull/2",
                   "wrong-route": "https://github.com/example/project/issues/2",
                   "noncanonical-number": "https://github.com/example/project/pull/02",
                   "wrong-scheme": "http://github.com/example/project/pull/2"}[change]
        creation["url"] = approval["url"] = altered
    elif change == "number-coherence":
        approval["approval_pr"] = 99
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
    if change in {"none", "repo-case"}:
        assert document["pending"] == [before["pending"][1]]
        record, = document["records"]
        assert (record["id"], record["attempts"], record["outcome"]) == (creation["id"], 0, "cancelled")
        assert record["deadline_at"] == before["pending"][0]["deadline_at"]
    else:
        assert document == before


def test_healthy_multi_page_observation_does_not_repeat_pr_creation_after_approval_was_sent(tmp_path, monkeypatch):
    from support.worker_process_timing import wait_until

    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, mode="detailed")
    sent = []
    pages = []
    original_page = NativeNotifications._page

    def observed_page(self, after):
        result = original_page(self, after)
        pages.append((after, result[1]))
        return result

    monkeypatch.setattr(NativeNotifications, "_page", observed_page)

    def transport(_config, rendered, _identity, *_args, **_kwargs):
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
    try:
        assert scenario.run().kind == "approval"
        observe_and_wait(observer)
        approval_retired = wait_until(lambda: [item for item in
            outbox.read_notifications(tmp_path, scenario.spec.run_id).get("records", [])
            if item["kind"] == "boundary" and item["outcome"] == "success"], timeout=3)
        assert len(approval_retired) == 1
        assert any(more for _after, more in pages), "The real observation never reached a second history page"
        # A later wake rechecks the durable receipt after the same worker has
        # completed paginated observation and retired the important approval.
        observe_and_wait(observer)
        observer.close()
        assert observer.failure_reason is None
        titles = [item["header"]["title"]["content"] for item in sent]
        assert titles.count("Please approve the PR merge") == 1
        assert not any(title.startswith("Final PR #") and title.endswith(" created") for title in titles)
        records = outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
        assert [item for item in records if item["id"] == approval_retired[0]["id"]] == approval_retired
        assert not any(item["kind"] == "pr_created" and item["attempts"] for item in records)
    finally:
        observer.close(drain=False)
