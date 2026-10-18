"""原英文完整交付的展示组合；生命周期由真实 CLI 代表保留。"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_events
from support.notification import state_with_round


def _detailed_ticket_state(language: str) -> dict[str, Any]:
    """仅准备投影输入；三轮事实及期望值由测试独立指定。"""
    state = state_with_round()
    invocation = state["agent_invocation_history"][0]
    state.update(
        language=language, created_at="2026-01-01T00:00:00+00:00",
        notifications={"mode": "detailed"}, status="run_approval_pending",
        ticket_graph={"tickets": {"2": {"title": "原始 Ticket title"}}},
        ticket_jobs={"2": {"phase": "completed", "generation": 1}},
        run_publication={"pr_number": 42, "required_checks_evidence": {"result": "none"}},
        semantic_agent_attempts=[], agent_invocation_history=[],
    )
    for role, outcome in (("development", "candidate"), ("review", "acceptance_artifact"),
                          ("publication", "publication_artifact")):
        for ordinal in range(1, 4) if role != "publication" else (1,):
            attempt = {
                "attempt_id": f"{role}-{ordinal}", "role": role, "ordinal": ordinal,
                "generation": 1, "status": "completed", "outcome": outcome,
                "work_subject": "run:r" if role == "publication" else "ticket:2",
                "development_summary": "原始 Agent 摘要",
            }
            if role == "review":
                attempt["acceptance_artifact"] = {"checks": {
                    lane: {"status": "fail" if ordinal < 3 and lane == "spec" else "pass",
                           "findings": ["原始 finding"] if ordinal < 3 and lane == "spec" else []}
                    for lane in ("e2e", "standards", "spec")
                }}
            state["semantic_agent_attempts"].append(attempt)
            state["agent_invocation_history"].append({
                **deepcopy(invocation), "semantic_attempt": deepcopy(attempt),
                "model": "fixture-model", "reasoning_effort": "high",
            })
    return state


@pytest.mark.parametrize("language", ["zh", "en"])
def test_detailed_ticket_delivery_card_fields(language: str) -> None:
    state = _detailed_ticket_state(language)
    original = deepcopy(state)
    projected = events(state)
    created = projected[0]
    assert created["title"] == ("任务已创建" if language == "zh" else "Task created")
    created_card = card(created)
    assert ("创建时间" if language == "zh" else "Created at") in str(created_card)
    assert created_card["body"]["elements"][-1]["text"]["content"] == (
        "打开 Issue" if language == "zh" else "Open Issue")
    starts = [event for event in projected if event["kind"] == "stage_start"
              and event["role"] == "development"]
    assert [event["round"] for event in starts] == [2, 3]
    assert all(event["task_number"] == 2 and event["task_title"] == "原始 Ticket title"
               for event in starts)
    assert all(event["title"] == ("正在开发：原始 Ticket title" if language == "zh" else
                                  "Developing: 原始 Ticket title") for event in starts)
    reviews = [event for event in projected if event["kind"] == "stage_start"
               and event["role"] == "review"]
    assert [event["round"] for event in reviews] == [1, 2, 3]
    assert all(event["title"] == ("正在验收：原始 Ticket title" if language == "zh" else
                                  "Reviewing: 原始 Ticket title") for event in reviews)
    assert not any(event["title"] == ("任务已完成" if language == "zh" else "Task completed")
                   for event in projected)
    failed = [event for event in projected if event["kind"] == "stage_end"
              and event.get("role") == "review" and event["color"] == "orange"]
    assert [event["round"] for event in failed] == [1, 2]
    for event in failed:
        assert event["title"] == ("验收未通过，将自动修复" if language == "zh" else
                                  "Review failed; automatic repair will follow")
        assert "原始 finding" in str(card(event))
    assert any(event["title"] == ("最终 PR 说明已准备好" if language == "zh" else
                                   "Final PR description is ready") for event in projected)
    assert not any(event["kind"] == "pr_created" for event in projected)
    completed_ticket = next(event for event in projected if event["kind"] == "ticket_completed")
    assert completed_ticket["task_number"] == 2
    assert completed_ticket["task_title"] == "原始 Ticket title"
    assert completed_ticket["total_seconds"] == 54
    rendered = json.dumps(card(completed_ticket), ensure_ascii=False)
    for role, label in (("development", "开发投入" if language == "zh" else "Development effort"),
                        ("review", "验收投入" if language == "zh" else "Review effort")):
        facts = completed_ticket["effort"][role]
        assert facts["rounds"] == 3
        assert facts["execution_seconds"] == 27
        configuration, = facts["configurations"]
        assert (configuration["model"], configuration["reasoning_effort"],
                configuration["rounds"], configuration["execution_seconds"]) == (
                    "fixture-model", "high", 3, 27)
        assert label in rendered
    for fragment in ("原始 Ticket title", "fixture-model", "high", "agent-run history r --details",
                     "3 轮" if language == "zh" else "3 rounds"):
        assert fragment in rendered
    assert all(card(event)["schema"] == "2.0" for event in projected)
    assert state == original

    approval = next(event for event in projected if event.get("current"))
    for event in (approval, recovery_events(state, projected)[-1]):
        assert event["task_number"] == 241
        assert event["url"] == "https://github.com/o/repo/pull/42"
        assert card(event)["header"]["subtitle"]["content"] == "o/repo · #241"
    state["status"] = "completed"
    completed_title = "任务已完成" if language == "zh" else "Task completed"
    completed, = [event for event in events(state) if event["title"] == completed_title]
    assert recovery_events(state, [approval]) == [completed]
    assert completed["color"] == "green"
    assert card(completed)["body"]["elements"][-1]["text"]["content"] == (
        "打开 PR" if language == "zh" else "Open PR")


@pytest.mark.parametrize("check_result,expected", [
    ("none", "No pre-merge checks configured"), ("pass", "Passed"),
    ("pending", "Pending"), ("fail", "Failed"), ("unknown", "Cannot currently confirm"),
])
def test_english_approval_card_check_results(check_result: str, expected: str) -> None:
    state = _detailed_ticket_state("en")
    state["run_publication"]["required_checks_evidence"]["result"] = check_result
    approval = next(event for event in events(state) if event.get("current"))
    assert approval["title"] == "Please approve merging PR #42"
    assert f"Checks: {expected}" in approval["summary"]
    assert expected in str(card(approval))
