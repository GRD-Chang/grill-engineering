"""Completion cards use current relationships and original completed work."""
import json
import time

import pytest

from agent_run.notifications.cards import card
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.event_projection import completion_context
from agent_run.queries.read_facts import read_boundaries
from support.run_scoped_kernel import domain_kernel
from support.notification import _completions


def test_ticket_completion_keeps_original_summary_and_current_parent_progress(dependent_ticket):
    scenario, _ = dependent_ticket
    with domain_kernel(scenario.kernel) as scenario.kernel:
        for completed_count, number in enumerate((3, 4), 1):
            for _ in range(200):
                if scenario.kernel.status(f"ticket:{number}")["phase"] == "complete":
                    break
                step = scenario.step()
                assert step.kind not in {"paused", "waiting", "approval"}, step
            else:
                raise AssertionError("The real Ticket did not complete")
            completed = _completions(scenario.kernel)
            assert len(completed) == completed_count
            first = completed[0]
            assert first["ticket_progress"] == {"completed": completed_count, "total": 2}
            assert first["parent_only"] is False
            assert first["task_title"] == next(issue.title for issue in scenario.requirements.issues if issue.number == 3)
            assert first["url"].endswith("/issues/3")
            assert first["summary"] == "Changed synthetic behavior 1"
            assert first["next_step"] == ("继续处理其他子任务" if completed_count == 1 else "继续完成整体交付；当前步骤见状态查询。")
            assert first["total_seconds"] is None
            rendered = json.dumps(card(first), ensure_ascii=False)
            assert f"子任务已完成 {completed_count}/2" in rendered
            assert "Changed synthetic behavior 1" in rendered
            if completed_count == 1:
                original = first
                assert scenario.kernel.control("stop", "stop-sibling", subject="ticket:4") == "applied"
                paused, = _completions(scenario.kernel)
                assert paused["id"] == original["id"]
                assert paused["next_step"] == "agent-run status --repo example/project -- offline-run"
                assert paused["completion_actions"] == [{"issue": 4, "scope": "local",
                    "reasons": ["operator_stopped"],
                    "next_action": "agent-run resume --repo example/project --ticket 4 -- offline-run"}]
                rendered = json.dumps(card(paused), ensure_ascii=False)
                assert "operator\\_stopped" in rendered.replace("\\\\", "\\")
                assert "--ticket 4" in rendered
                assert scenario.kernel.control("resume", "resume-sibling", subject="ticket:4") == "applied"
            else:
                assert first["id"] == original["id"]
                assert first["source_material_id"] == original["source_material_id"]
                assert completed[1]["summary"] == "Changed synthetic behavior 2"
        with scenario.kernel.storage.transaction(write=False) as tx:
            view = read_boundaries(tx, time.time())
            assert view["is_terminal"] is False and view["closures"] == []


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
