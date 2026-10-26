"""Completion cards use current relationships and original completed work."""
import json
import time

import pytest

from agent_run.model import Issue, ParentIssue, Requirements
from agent_run.notifications.cards import card
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.event_projection import completion_context, event_state, project_event
from agent_run.queries.read_facts import read_boundaries
from support.offline import GraphScenario


def _completions(scenario):
    with scenario.kernel.storage.transaction(write=False) as tx:
        state = event_state(tx)
        return [item.content for row in tx.all("SELECT * FROM history ORDER BY position")
                for item in project_event(tx, dict(row), state, "concise")
                if item.content["kind"] == "ticket_completed"]


def test_ticket_completion_keeps_original_summary_and_current_parent_progress(tmp_path):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Overall requirement", "Scope", (2, 3)), (
            Issue(2, "First task", "First scope", "OPEN", labels=("ready-for-agent",)),
            Issue(3, "Second task", "Second scope", "OPEN", labels=("ready-for-agent",)),)))
    for number in (2, 3):
        for _ in range(200):
            if scenario.kernel.status(f"ticket:{number}")["phase"] == "complete":
                break
            step = scenario.step()
            assert step.kind not in {"paused", "waiting", "approval"}, step
        else:
            raise AssertionError("The real Ticket did not complete")
        completed = _completions(scenario)
        assert len(completed) == number - 1
        first = completed[0]
        assert first["ticket_progress"] == {"completed": number - 1, "total": 2}
        assert first["parent_only"] is False
        assert first["task_title"] == "First task"
        assert first["url"].endswith("/issues/2")
        assert first["summary"] == "Changed synthetic behavior 1"
        assert first["next_step"] == ("继续处理其他子任务" if number == 2 else "继续完成整体交付；当前步骤见状态查询。")
        assert first["total_seconds"] is None
        rendered = json.dumps(card(first), ensure_ascii=False)
        assert f"子任务已完成 {number - 1}/2" in rendered
        assert "Changed synthetic behavior 1" in rendered
        if number == 2:
            original = first
            assert scenario.kernel.control("stop", "stop-sibling", subject="ticket:3") == "applied"
            paused, = _completions(scenario)
            assert paused["id"] == original["id"]
            assert paused["next_step"] == "agent-run status --repo example/project -- offline-run"
            assert paused["completion_actions"] == [{"issue": 3, "scope": "local",
                "reasons": ["operator_stopped"],
                "next_action": "agent-run resume --repo example/project --ticket 3 -- offline-run"}]
            rendered = json.dumps(card(paused), ensure_ascii=False)
            assert "operator\\_stopped" in rendered.replace("\\\\", "\\")
            assert "--ticket 3" in rendered
            assert scenario.kernel.control("resume", "resume-sibling", subject="ticket:3") == "applied"
        else:
            assert first["id"] == original["id"]
            assert first["source_material_id"] == original["source_material_id"]
            assert completed[1]["summary"] == "Changed synthetic behavior 2"
    with scenario.kernel.storage.transaction(write=False) as tx:
        view = read_boundaries(tx, time.time())
        assert view["is_terminal"] is False and view["closures"] == []


def test_parent_only_completion_context_uses_actual_cleanup_and_approval_boundaries(tmp_path):
    scenario = GraphScenario(tmp_path, Requirements(ParentIssue(1, "Direct delivery", "Scope", ()), ()))
    assert scenario.run().kind == "approval"

    def context():
        with scenario.kernel.storage.transaction(write=False) as tx:
            return completion_context(read_boundaries(tx, time.time()), event_state(tx))

    approval = context()
    assert approval["parent_only"] is True and approval["ticket_progress"] is None
    assert approval["next_step"] == "agent-run status --repo example/project -- offline-run"
    assert len(approval["completion_actions"]) == 1
    assert approval["completion_actions"][0]["next_action"] == "agent-run approve --repo example/project -- offline-run"
    assert scenario.kernel.control("approve", "original-approval", subject="parent:1", world=scenario.world()) == "pending"
    for _ in range(100):
        if scenario.kernel.status("parent:1")["phase"] == "complete":
            break
        step = scenario.step()
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The original close did not complete")
    pending = context()
    assert pending["parent_only"] is True
    assert pending["next_step"] == "agent-run resume --repo example/project -- offline-run"
    assert pending["completion_actions"] == []
    assert scenario.run().kind == "complete"
    closed = context()
    assert closed["next_step"] == "无需继续执行交付操作。"
    assert closed["parent_only"] is True and closed["ticket_progress"] is None


@pytest.mark.parametrize("language", ["zh", "en"])
def test_completion_cards_keep_all_current_actions_and_explicit_parent_only(language):
    # The card boundary is pure; these are existing read_boundaries fields,
    # not business producers or new completion/authorization evidence.
    state = {"run_id": "example-run", "repository": "example/project", "language": language,
             "parent": {"number": 1, "title": "Original requirement"}}
    actions = [{"issue": 3, "scope": "local", "reasons": ["Original blocker / 原始原因"],
                "next_action": "agent-run resume --repo example/project --ticket 3 -- example-run"},
               {"issue": None, "scope": "global", "reasons": ["execution_failed"],
                "next_action": "agent-run status --repo example/project -- example-run"}]
    view = {"subjects": [{"kind": "ticket", "phase": "complete"}, {"kind": "ticket", "phase": "development"}],
            "actions": actions, "status": "execution_failed", "abandonment": None, "delivery_cleanup": None}
    event = event_envelope(state, "ticket_completed", "original-completion", "Completed Ticket",
                           summary="Original work summary / 原始摘要", **completion_context(view, state))
    rendered = json.dumps(card(event), ensure_ascii=False)
    assert ("子任务已完成 1/2" if language == "zh" else "Tickets completed: 1/2") in rendered
    assert "Original blocker / 原始原因" in rendered and "--ticket 3" in rendered
    assert "execution\\_failed" in rendered.replace("\\\\", "\\")
    # A local Gate can coexist with a global failure. Label its scope without
    # promising that independent work can run past the global boundary.
    assert ("independent work can continue" if language == "en" else "其他独立工作可继续") not in rendered
    assert ("Ticket boundary" if language == "en" else "子任务待办") in rendered
    assert event["next_step"] == "agent-run status --repo example/project -- example-run"
    view.update(subjects=[{"kind": "parent_only", "phase": "complete"}], actions=[], status="completed")
    event.update(completion_context(view, state))
    rendered = json.dumps(card(event), ensure_ascii=False)
    assert ("直接交付整体需求（无子任务）" if language == "zh" else "Parent-only delivery (no Tickets)") in rendered
    assert "0/0" not in rendered and "1/2" not in rendered
    assert ("无需继续执行交付操作。" if language == "zh" else "No further delivery action required.") in rendered
    view["subjects"] = []
    unknown = completion_context(view, state)
    assert unknown["parent_only"] is False and unknown["ticket_progress"] is None
