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
from agent_run.notifications.event_projection import completion_context, event_state, project_event
from agent_run.notifications.progress import obsolete_progress
from agent_run.config.defaults import UserDefaultsStore
from support.notification import _enabled_scenario, _project, observe_and_wait, recorded_agents


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
    status_api = None

    def transport(_config: Any, rendered: dict[str, Any], _identity: str,
                  *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered)
        active, = [item["event"] for item in outbox.read_notifications(tmp_path, scenario.spec.run_id)["pending"]
                   if item["outcome"] == "sending"]
        captured.append(active)
        return {"outcome": "success", "reason": None}

    def observe(kind: str = "acceptance_passed") -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport,
                                       status_api=status_api, storage=scenario.kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        return [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == kind]

    def completion():
        from agent_run.queries.read_facts import read_boundaries
        with scenario.kernel.storage.transaction(write=False) as tx:
            return completion_context(read_boundaries(tx, time.time()), event_state(tx))

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
            approval = completion()
            assert approval["parent_only"] is True and approval["ticket_progress"] is None
            assert approval["next_step"] == "agent-run status --repo example/project -- offline-run"
            assert approval["completion_actions"][0]["next_action"] == "agent-run approve --repo example/project -- offline-run"
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
    context = completion()
    assert context["parent_only"] is True and context["completion_actions"] == []
    assert context["next_step"] == "agent-run resume --repo example/project -- offline-run"
    # The same committed close owns both notification and Status guidance.
    from agent_run.queries.status_comment import project_status
    from agent_run.queries.read_facts import action_guidance
    from support.status_comments import CommentsAPI
    import shlex
    projection = project_status(scenario.kernel.storage.path, scenario.delivery.key, storage=scenario.kernel.storage)
    status_api = CommentsAPI(projection.target)
    database_before = scenario.kernel.storage.path.read_bytes()
    quoted = action_guidance("resume", run_id="a'  b", repository=scenario.spec.repository, language="en")
    assert shlex.split(quoted) == ["agent-run", "resume", "--repo", scenario.spec.repository, "--", "a'  b"]
    pending = observe("boundary")
    assert len(pending) == 1
    assert sent[-1]["header"]["template"] == "yellow"
    assert sent[-1]["header"]["title"]["content"] == event_copy({"language": "en"}, "cleanup")
    terminal, = [event for event in captured if event["id"] == pending[0]["id"]]
    assert terminal["terminal"] and terminal["next_step"] == context["next_step"]
    assert terminal["parent_only"] is True and terminal["ticket_progress"] is None
    assert terminal["completion_actions"] == [] and terminal["next_step"] in projection.body
    assert [command[1] for command in status_api.commands] == ["POST"]
    assert scenario.kernel.storage.path.read_bytes() == database_before
    assert observe("boundary") == pending

    assert scenario.run(approve=True).kind == "complete"
    context = completion()
    assert context["parent_only"] is True and context["ticket_progress"] is None
    assert context["next_step"] == "No further delivery action required."
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


def test_observed_run_branch_write_does_not_promote_a_superseded_acceptance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    active, planned = [], None

    def started(call):
        if call.subject != scenario.delivery.key or call.role != Role.DEVELOPMENT:
            return
        repair = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "acceptance_repair"][-1]
        assert repair["live"] is True and repair["attempt_id"] == call.attempt_id
        if not active:
            assert scenario.kernel.control("stop", "stop-current-repair", subject=call.subject) == "applied"
            stopped = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "acceptance_repair"][-1]
            assert stopped["id"] == repair["id"] and stopped["live"] is False
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, [repair], "concise", current_boundary=False) == set()
        active.append(repair)

    recorded_agents(scenario, monkeypatch, started=started)
    issued = []
    execute = scenario.port.execute
    def capture(effect, authorize=None):
        if effect.operation == "run_branch":
            issued.append(effect)
        return execute(effect, authorize)
    monkeypatch.setattr(scenario.port, "execute", capture)
    for _ in range(100):
        if active and "operator_stopped" in scenario.kernel.status(scenario.delivery.key)["gates"]:
            # The controlled original process has returned and exited before Resume.
            assert scenario.kernel.control("resume", "continue-original-repair", subject=scenario.delivery.key) == "applied"
        step = scenario.step()
        if planned is None:
            # The initial failed Final review creates the planned repair.
            # Earlier Ticket history does not need projecting at every step.
            with scenario.kernel.storage.transaction(write=False) as tx:
                consumed = tx.rows("SELECT * FROM history WHERE run_id=? AND subject=? "
                                   "AND kind='role_consumed' ORDER BY position DESC LIMIT 1",
                                   (scenario.spec.run_id, scenario.delivery.key))
                if consumed:
                    events = project_event(tx, dict(consumed[0]), event_state(tx), "concise")
                    for event in events:
                        if event.content["kind"] == "acceptance_repair" and event.visible:
                            planned = event.content
                            assert planned["live"] is False
                            assert obsolete_progress(tx, [planned], "concise", current_boundary=False) == set()
                            assert obsolete_progress(tx, [planned], "concise", current_boundary=True) == {planned["id"]}
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
    filename = "foreign-source.txt"
    foreign = scenario.port.external_remote_append(scenario.delivery.source_ref, {filename: "Independent retained work\n"}, "before-consume")
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
    for _ in range(100):
        scenario.step()
        with scenario.kernel.storage.transaction(write=False) as tx:
            rows = tx.rows("SELECT * FROM history WHERE run_id=? AND subject=? AND kind='acceptance_promoted'",
                           (scenario.spec.run_id, scenario.delivery.key))
            if rows:
                row, = rows
                event, = project_event(tx, dict(row), event_state(tx), "concise")
                assert event.visible and event.content["kind"] == "acceptance_passed"
                promotion = event.content
                break
    else:
        pytest.fail("The current Review never reached formal promotion")
    assert planned is not None and active[0]["id"] == planned["id"]
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [active[0], promotion], "concise", current_boundary=False) == {planned["id"]}
        original = dict(tx.one("SELECT * FROM history WHERE kind='role_consumed' AND identity=?", (promotion["attempt_id"],)))
        suppressed, = project_event(tx, original, event_state(tx), "detailed")
        assert not suppressed.visible and suppressed.applicable is False
        assert obsolete_progress(tx, [suppressed.content], "detailed", current_boundary=False) == {suppressed.content["id"]}
    assert scenario.run().kind == "approval"
    assert scenario.review_count > original_reviews
    assert not any(event["kind"] == "approval_update" for event in _project(scenario.kernel, "concise"))
    scenario.port.external_remote_append(scenario.delivery.target_ref, {"external.txt": "New target\n"}, "approval-update")
    assert scenario.step().reason == "updating"
    update, = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "approval_update"]
    with scenario.kernel.storage.transaction(write=False) as tx:
        invalidated = tx.one("SELECT * FROM history WHERE subject=? AND kind='boundary_update' ORDER BY position DESC LIMIT 1", (scenario.delivery.key,))
        assert (update["source_position"], update["source_material_id"]) == (invalidated["position"], invalidated["material_id"])
        assert update["work_subject"] == scenario.delivery.key and update["generation"] == 1
        assert obsolete_progress(tx, [promotion, update], "concise", current_boundary=False) == {promotion["id"]}
    assert scenario.run().kind == "approval"
    projected = _project(scenario.kernel, "concise")
    assert [event for event in projected if event["kind"] == "approval_update"] == [update]
    assert not any(event["kind"] == "approval_ready" for event in projected)
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [update], "concise", current_boundary=True) == {update["id"]}
    assert scenario.run(approve=True).kind == "complete"
    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
    observer.close()
    assert observer.failure_reason is None
    accepted = [row for row in outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                if row["kind"] == "acceptance_passed"]
    # Source recovery and the later target invalidation each promote one new Review.
    assert len(accepted) == len({row["id"] for row in accepted}) == 2
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert target is not None
    assert scenario.port.git.run("show", f"{target.sha}:{filename}").stdout == "Independent retained work\n"
    scenario.port.git.run("merge-base", "--is-ancestor", foreign.sha, target.sha)


@pytest.mark.parametrize("target_changed", [False, True], ids=["current", "target-drift"])
def test_run_branch_receipt_promotes_only_the_reviewed_target(target_changed):
    """Consume recorded Review/effect inputs; the source-race story owns Git production."""
    from contextlib import closing
    from dataclasses import asdict
    from agent_run.model import Commit, EffectResult, World, wire_json
    from support.repository_scope import connection, insert, seed
    from support.run_scoped_kernel import ScopedKernel
    from support.offline import review_result

    run, subject = "promotion-consumer", "final:1"
    base, candidate = Commit("a" * 40, "b" * 40), Commit("c" * 40, "d" * 40)
    target = Commit("e" * 40, "f" * 40) if target_changed else base
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, run, subject=subject)
        db.commit()
        kernel = ScopedKernel(db, run)
        with kernel.storage.transaction() as tx:
            context = tx.put(subject, "role_context", wire_json({}))
            result = tx.put(subject, "agent_result", wire_json(review_result("pass")))
            insert(db, "attempts", dict(run_id=run, id="review", subject=subject,
                generation=1, ordinal=1, role="review", purpose="integration", window=1,
                revision="r", parent_revision="r", graph_revision="g", base_sha=base.sha,
                candidate_sha=candidate.sha, candidate_tree=candidate.tree,
                expected_tree=candidate.tree, completion_revision="completed",
                context_id=context, result_id=result, status="consumed", outcome="pass"))
            tx.execute("UPDATE subjects SET kind='final_run',phase='ready_publication',purpose='integration',"
                       "candidate_sha=?,candidate_tree=?,integrated_sha=?,integrated_tree=?,expected_tree=?,"
                       "completion_revision='completed',review_attempt='review' WHERE id=?",
                       (candidate.sha, candidate.tree, candidate.sha, candidate.tree, candidate.tree, subject))
            payload = tx.put(subject, "effect_intent", wire_json({}))
            receipt = tx.put(subject, "effect_receipt", wire_json(asdict(EffectResult("target", commit=candidate))))
            insert(db, "effects", dict(run_id=run, id="run-branch", subject=subject,
                operation="run_branch", status="observed", executor="owner", epoch=1, actor="publisher",
                parent_revision="r", graph_revision="g", revision="r", generation=1,
                source_ref="refs/heads/work", target_ref="refs/heads/main",
                before_sha=base.sha, after_sha=candidate.sha, base_sha=base.sha,
                base_tree=base.tree, payload_id=payload, receipt_id=receipt, attempts=1))
        world = World("r", "g", "r", target, candidate, completion_revision="completed")
        with kernel.storage.transaction() as tx:
            header, job = tx.one("SELECT * FROM runs"), tx.one("SELECT * FROM subjects")
            effect = tx.one("SELECT * FROM effects")
            assert kernel._apply_effect(tx, header, job, effect, world).kind == "progress"
            assert tx.one("SELECT status FROM effects")[0] == "confirmed"
            assert tx.one("SELECT phase FROM subjects")[0] == "publish"
            promoted = tx.rows("SELECT identity FROM history WHERE kind='acceptance_promoted'")
            assert [row[0] for row in promoted] == ([] if target_changed else ["review"])


def test_healthy_final_pr_and_approval_in_one_observation_share_only_the_approval_card(
    tmp_path, monkeypatch,
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
    original_page, pages = NativeNotifications._page, []

    def page(self, after):
        result = original_page(self, after)
        pages.append((after, result[1]))
        return result

    monkeypatch.setattr(NativeNotifications, "_page", page)
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY, mode="detailed")
    # Read the same real PR at checks and approval from independent journals.
    for _ in range(80):
        step = scenario.step()
        if scenario.kernel.status(scenario.delivery.key)["phase"] == "checks":
            break
        assert step.kind not in {"paused", "waiting", "approval", "complete"}
    else:
        raise AssertionError("Normal producer did not consume its Final PR creation")
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None
    assert pr.repository == "EXAMPLE/PROJECT"
    from agent_run.queries.run import read
    for approval in (False, True):
        if approval:
            # The first observer is closed. Preserve its receipt separately so
            # approval is observed with a fresh journal under the same owned root.
            outbox.notification_path(tmp_path, scenario.spec.run_id).rename(tmp_path / "checks-notifications.json")
            assert scenario.run().kind == "approval"
        # Discard mutable notification files from the read-only product comparison.
        database_before = scenario.kernel.storage.path.read_bytes()
        sent = []

        def transport(_config, rendered, _identity, *_args, **_kwargs):
            sent.append(rendered)
            return {"outcome": "success", "reason": None}

        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
        try:
            observe_and_wait(observer)
            if approval:
                from support.worker_process_timing import wait_until
                retired = wait_until(lambda: [item for item in
                    outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
                    if item["kind"] == "boundary" and item["outcome"] == "success"], timeout=3)
                assert len(retired) == 1
                # A fresh wake after a retired approval must not resend its PR.
                observe_and_wait(observer)
            observer.close()
            assert observer.failure_reason is None
        finally:
            observer.close(drain=False)
        assert observer.closed
        journal = outbox.read_notifications(tmp_path, scenario.spec.run_id)
        assert journal.get("unavailable_reason") is None
        records = journal["records"]
        created = [item for item in records if item["kind"] == "pr_created"]
        assert len([item for item in created if item["outcome"] != "cancelled"]) == int(not approval)
        if approval:
            assert len(created) == 1 and created[0]["outcome"] == "cancelled" and created[0]["attempts"] == 0
            assert created[0]["reason"] == "included in current Final PR approval"
            assert not any(item["header"]["title"]["content"] == f"Final PR #{pr.number} created" for item in sent)
        approval_cards = [item for item in sent if item["header"]["title"]["content"] == "Please approve the PR merge"]
        assert bool(approval_cards) is approval
        if approval:
            assert any(f"https://github.com/example/project/pull/{pr.number}" in str(item) for item in approval_cards)
            assert len(approval_cards) == 1
            assert [item for item in records if item["id"] == retired[0]["id"]] == retired
            assert not any(item["kind"] == "pr_created" and item["attempts"] for item in records)
            assert any(more for _after, more in pages)
            with scenario.kernel.storage.transaction(write=False) as tx:
                assert journal["native_cursor"] == tx.one("SELECT max(position) FROM history")[0]
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
