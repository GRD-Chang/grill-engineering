from __future__ import annotations

from typing import Any

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_event


def state_with_round(role: str = "development", outcome: str = "candidate") -> dict[str, Any]:
    attempt = {"attempt_id": "a1", "role": role, "work_subject": "run:r",
               "ordinal": 1, "generation": 1, "status": "completed", "outcome": outcome,
               "development_summary": "已实现输入校验"}
    return {"run_id": "r", "repository": "o/repo", "parent": {"number": 241, "title": "飞书通知"},
            "status": "run_review_pending", "semantic_agent_attempts": [attempt],
            "agent_invocation_history": [{"semantic_attempt": attempt.copy(), "status": "completed",
                                          "started_at": "2026-01-01T00:00:00+00:00",
                                          "ended_at": "2026-01-01T00:00:09+00:00"}]}


def test_semantic_round_identity_survives_resumption_and_wording_changes() -> None:
    state = state_with_round()
    before = events(state)
    state["agent_invocation_history"].append({**state["agent_invocation_history"][0], "resume_id": "resume-1"})
    state["semantic_agent_attempts"][0]["development_summary"] = "摘要措辞更新"
    after = events(state)
    assert [event["id"] for event in before] == [event["id"] for event in after]
    assert [event["kind"] for event in after] == ["run_start", "stage_start", "stage_end"]
    state["semantic_agent_attempts"][0].update(attempt_id="a2", ordinal=2)
    assert len([event for event in events(state) if event["kind"] == "stage_end"]) == 2


def test_process_exit_does_not_finish_pending_round() -> None:
    state = state_with_round()
    state["semantic_agent_attempts"][0].update(status="pending", outcome=None)
    assert not any(event["kind"] == "stage_end" for event in events(state))


def test_review_findings_and_publication_meanings() -> None:
    state = state_with_round("review", "acceptance_artifact")
    state["semantic_agent_attempts"][0]["acceptance_artifact"] = {
        "checks": {"spec": {"status": "fail", "findings": ["缺少超时回收", "重复通知"]}}}
    event = events(state)[-1]
    assert event["color"] == "orange"
    assert event["findings_count"] == 2
    assert "自动修复" in event["next_step"]
    assert "缺少超时回收" in event["summary"]
    state["status"] = "ready_for_human"
    assert [event for event in events(state) if event["kind"] == "stage_end"][0]["color"] == "yellow"
    publication = events(state_with_round("publication", "publication_artifact"))[-1]
    assert "发布说明已准备" in publication["title"]
    assert "整体交付完成" not in publication["title"]
    assert not any(event["kind"] == "pr_created" for event in events(state_with_round("publication", "publication_artifact")))


@pytest.mark.parametrize(("status", "color"), [
    ("ready_for_human", "yellow"), ("run_approval_pending", "yellow"),
    ("execution_failed", "red"), ("completed", "green"), ("operator_stopped", "yellow"),
])
def test_boundary_colors_and_recovery_use_current_state(status: str, color: str) -> None:
    state = state_with_round()
    state.update(status="ready_for_human", human_blockers=["旧凭据待处理"])
    missed = events(state)
    state.update(status=status, human_blockers=[])
    event = events(state)[-1]
    assert event["current"] and event["color"] == color
    summary = recovery_event(state, missed)
    assert "旧凭据" not in summary["summary"]
    assert summary["color"] == color


def test_card_compact_unknown_duration_and_safe_navigation() -> None:
    event = events(state_with_round())[-1]
    event.update(duration_seconds=None, summary="长原因" * 200)
    value = card(event)
    assert value["schema"] == "2.0"
    assert value["config"]["width_mode"] == "compact"
    assert "o/repo #241" in value["config"]["summary"]["content"]
    assert "耗时**：未知" in value["body"]["elements"][0]["content"]
    assert "agent-run history r --details" in value["body"]["elements"][1]["content"]
    button = value["body"]["elements"][-1]
    assert button["behaviors"] == [{"type": "open_url", "default_url": "https://github.com/o/repo/issues/241"}]
    event["url"] = "javascript:alert(1)"
    assert not any(element["tag"] == "button" for element in card(event)["body"]["elements"])


def test_start_waits_for_task_title_and_pr_uses_persisted_fact() -> None:
    state = state_with_round()
    state["parent"]["title"] = None
    assert not any(event["kind"] == "run_start" for event in events(state))
    state["parent"]["title"] = "真实标题"
    state["run_publication"] = {"pr_number": 99}
    projected = events(state)
    assert projected[0]["task_title"] == "真实标题"
    assert [event["url"] for event in projected if event["kind"] == "pr_created"] == ["https://github.com/o/repo/pull/99"]


def test_failure_is_red_with_actual_diagnostic_and_no_success_claim() -> None:
    state = state_with_round()
    state["semantic_agent_attempts"][0].update(status="pending", outcome=None)
    state["agent_invocation_history"][0]["status"] = "failed"
    state.update(status="execution_failed", diagnostics=[{"message": "输出管道断开"}])
    projected = events(state)
    ended = [event for event in projected if event["kind"] == "stage_end"]
    assert len(ended) == 1 and ended[0]["color"] == "red"
    assert "执行失败" in ended[0]["title"]
    assert "输出管道断开" in projected[-1]["summary"]


def test_review_failed_check_without_findings_and_updated_result() -> None:
    state = state_with_round("review", "acceptance_artifact")
    attempt = state["semantic_agent_attempts"][0]
    attempt["acceptance_artifact"] = {"checks": {"spec": {"status": "fail", "findings": []}}}
    failed = events(state)[-1]
    assert failed["color"] == "orange"
    attempt["acceptance_artifact"]["checks"]["spec"]["status"] = "pass"
    passed = events(state)[-1]
    assert passed["color"] == "green" and failed["id"] != passed["id"]


@pytest.mark.parametrize("delivery_type,owner,status", [
    ("multi_ticket", "run_publication", "run_approval_pending"),
    ("parent_only", "parent_job", "parent_approval_pending"),
])
@pytest.mark.parametrize("check_result,description", [
    ("none", "未配置合并前检查"), ("pass", "已通过"),
    ("pending", "待处理"), ("fail", "未通过"), ("unknown", "暂时无法确认"),
])
def test_approval_card_uses_persisted_checks_and_final_pr(
    delivery_type: str, owner: str, status: str, check_result: str, description: str,
) -> None:
    state = state_with_round("review", "acceptance_artifact")
    state.update(delivery_type=delivery_type, status=status)
    state[owner] = {"pr_number": 42, "required_checks_evidence": {"result": check_result}}
    projected = events(state)
    approval = next(event for event in projected if event["current"])
    assert f"检查：{description}" in approval["summary"]
    assert next(event for event in projected if event["kind"] == "pr_created")["url"].endswith("/pull/42")


def test_manual_resume_uses_resume_identity_once() -> None:
    state = state_with_round()
    state["resume_audit"] = {"history": [{
        "resume_id": "resume-1", "requested_at": "2026-01-01T00:01:00+00:00",
        "work_subject": "run:r", "semantic_attempt_id": "a1", "source_status": "ready_for_human",
    }]}
    first = [event for event in events(state) if event["kind"] == "resume"]
    assert len(first) == 1
    state["resume_audit"]["history"][0]["requested_at"] = "2026-01-01T00:02:00+00:00"
    assert [event["id"] for event in events(state) if event["kind"] == "resume"] == [first[0]["id"]]


@pytest.mark.parametrize("with_findings", [False, True])
def test_pending_review_reports_blocked_result_then_passes_in_same_round(with_findings: bool) -> None:
    state = state_with_round("review", "acceptance_artifact")
    attempt = state["semantic_agent_attempts"][0]
    attempt.update(status="pending", outcome=None)
    attempt.pop("development_summary")
    attempt["acceptance_artifact"] = {
        "checks": {"e2e": {"status": "blocked", "findings": [], "evidence": "缺少测试凭据"}}}
    if with_findings:
        attempt["acceptance_artifact"]["checks"]["spec"] = {"status": "fail", "findings": ["缺少字段校验"]}
    state["status"] = "ready_for_human"
    blocked = [event for event in events(state) if event["kind"] == "stage_end"]
    assert len(blocked) == 1
    assert blocked[0]["title"] == "验收 Agent · 验收受阻"
    assert blocked[0]["color"] == "yellow"
    assert "缺少测试凭据" in blocked[0]["summary"]
    assert blocked[0]["round"] == 1
    assert blocked[0]["duration_seconds"] == 9
    start = next(event["id"] for event in events(state) if event["kind"] == "stage_start")
    # Resuming the still-pending round preserves the existing result identity.
    state["status"] = "run_acceptance_pending"
    assert next(event["id"] for event in events(state) if event["kind"] == "stage_end") == blocked[0]["id"]
    attempt.update(status="completed", outcome="acceptance_artifact")
    attempt["acceptance_artifact"]["checks"] = {"e2e": {"status": "pass", "findings": []}}
    passed = next(event for event in events(state) if event["kind"] == "stage_end")
    assert passed["id"] != blocked[0]["id"]
    assert passed["color"] == "green"
    assert passed["round"] == 1
    assert next(event["id"] for event in events(state) if event["kind"] == "stage_start") == start
