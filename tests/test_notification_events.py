from __future__ import annotations

from typing import Any

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_event


def state_with_round(role: str = "development", outcome: str = "candidate") -> dict[str, Any]:
    attempt = {"attempt_id": "a1", "role": role, "work_subject": "run:r",
               "ordinal": 1, "generation": 1, "status": "completed", "outcome": outcome,
               "development_summary": "已实现输入校验"}
    return {"language": "zh", "run_id": "r", "repository": "o/repo", "parent": {"number": 241, "title": "飞书通知"},
            "status": "run_review_pending", "semantic_agent_attempts": [attempt],
            "agent_invocation_history": [{"semantic_attempt": attempt.copy(), "status": "completed", "reported_thread_id": "thread-1",
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
    assert publication["title"] == "最终 PR 说明已准备好"
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
    assert "耗时" not in value["body"]["elements"][0]["content"]
    assert "agent-run history r --details" in value["body"]["elements"][1]["content"]
    button = value["body"]["elements"][-1]
    assert button["behaviors"] == [{"type": "open_url", "default_url": "https://github.com/o/repo/issues/241"}]
    event["url"] = "javascript:alert(1)"
    assert not any(element["tag"] == "button" for element in card(event)["body"]["elements"])


def test_start_waits_for_task_title_and_pr_uses_persisted_fact() -> None:
    state = state_with_round()
    state["parent"]["title"] = None
    assert next(event for event in events(state) if event["kind"] == "run_start")["task_title"] == ""
    state["parent"]["title"] = "真实标题"
    state["run_publication"] = {"pr_number": 99}
    projected = events(state)
    assert projected[0]["task_title"] == "真实标题"
    assert [event["url"] for event in projected if event["kind"] == "pr_created"] == ["https://github.com/o/repo/pull/99"]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_failure_is_red_with_actual_diagnostic_and_no_success_claim(
    language: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_round()
    state["semantic_agent_attempts"][0].update(status="pending", outcome=None)
    state["agent_invocation_history"][0]["status"] = "failed"
    state.update(language=language, status="execution_failed", diagnostics=[{"message": "输出管道断开"}])
    from agent_run import notification_events
    original = notification_events.history_records

    def changed_copy(*args: Any, **kwargs: Any) -> Any:
        records = original(*args, **kwargs)
        for record in records:
            record["status_text"] = "Unrelated display text"
        return records

    monkeypatch.setattr(notification_events, "history_records", changed_copy)
    projected = events(state)
    ended = [event for event in projected if event["kind"] == "stage_end"]
    assert len(ended) == 1 and ended[0]["color"] == "red"
    assert ("执行失败" if language == "zh" else "Execution failed") in ended[0]["title"]
    assert "输出管道断开" in projected[-1]["summary"]


def test_review_failed_check_without_findings_and_updated_result() -> None:
    state = state_with_round("review", "acceptance_artifact")
    attempt = state["semantic_agent_attempts"][0]
    attempt["acceptance_artifact"] = {"checks": {lane: {"status": "fail" if lane == "spec" else "pass", "findings": []} for lane in ("e2e", "standards", "spec")}}
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
    assert approval["url"].endswith("/pull/42")
    assert not any(event["kind"] == "pr_created" for event in projected)


def test_manual_resume_uses_resume_identity_once() -> None:
    state = state_with_round()
    state["resume_audit"] = {"history": [{
        "resume_id": "resume-1", "requested_at": "2026-01-01T00:01:00+00:00",
        "work_subject": "run:r", "semantic_attempt_id": "a1", "source_status": "ready_for_human",
    }]}
    assert not any(event["kind"] == "resume" for event in events(state))
    state["_manual_resume_result"] = {"id": "resume-1", "outcome": "started", "evidence": "worker_started"}
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
    attempt["acceptance_artifact"]["checks"] = {lane: {"status": "pass", "findings": []} for lane in ("e2e", "standards", "spec")}
    passed = next(event for event in events(state) if event["kind"] == "stage_end")
    assert passed["id"] != blocked[0]["id"]
    assert passed["color"] == "green"
    assert passed["round"] == 1
    assert next(event["id"] for event in events(state) if event["kind"] == "stage_start") == start


def test_modes_ticket_identity_and_logical_e2e_counts() -> None:
    state = state_with_round()
    state['ticket_graph'] = {'tickets': {'2': {'title': '限制列表数量 2026-09-15'}}}
    state['ticket_jobs'] = {'2': {'phase': 'completed', 'generation': 1}}
    state['semantic_agent_attempts'] = []
    state['agent_invocation_history'] = []
    for index, role in enumerate(['development', 'review', 'publication', 'review', 'publication']):
        attempt = {'attempt_id': f'a{index}', 'role': role, 'ordinal': 1, 'status': 'completed',
                   'work_subject': 'ticket:2' if index < 3 else 'run:r',
                   'outcome': {'development': 'candidate', 'review': 'acceptance_artifact', 'publication': 'publication_artifact'}[role]}
        state['semantic_agent_attempts'].append(attempt)
        state['agent_invocation_history'].append({'semantic_attempt': attempt.copy(), 'status': 'completed', 'reported_thread_id': 'thread-1',
            'started_at': f'2026-01-01T00:0{index}:00+00:00', 'ended_at': f'2026-01-01T00:0{index}:30+00:00'})
    state.update(status='run_approval_pending', run_publication={'pr_number': 9})
    detailed = events(state)
    assert len(detailed) == 13  # start + ten round events + ticket + approval
    ticket = next(event for event in detailed if event['kind'] == 'ticket_completed')
    assert ticket['task_title'] == '限制列表数量 2026-09-15'
    assert ticket['url'].endswith('/issues/2')
    assert ticket['total_seconds'] == 90
    assert next(event for event in detailed if event['kind'] == 'stage_start')['task_number'] == 2
    state['notifications'] = {'mode': 'concise'}
    concise = events(state)
    assert [event['kind'] for event in concise] == ['run_start', 'ticket_started', 'ticket_completed', 'boundary']
    state['status'] = 'completed'
    assert events(state)[-1]['title'] == '任务已完成'
    assert len({event['id'] for event in concise + events(state)}) == 5
    state['notifications']['mode'] = 'detailed'
    assert len({event['id'] for event in detailed + events(state)}) == 14


def test_round_resume_timing_and_missing_data_are_not_fabricated() -> None:
    state = state_with_round()
    invocation = state['agent_invocation_history'][0]
    state['agent_invocation_history'].append({**invocation, 'resume_id': 'r2',
        'started_at': '2026-01-01T01:00:00+00:00', 'ended_at': '2026-01-01T01:07:55+00:00'})
    ended = events(state)[-1]
    assert ended['duration_seconds'] == 484
    assert '8 分 4 秒' in str(card(ended))
    state['agent_invocation_history'][0].pop('ended_at')
    ended = events(state)[-1]
    assert ended['duration_seconds'] is None
    assert '耗时' not in str(card(ended))


def test_latest_recovery_keeps_mode_and_omits_resolved_todo() -> None:
    state = state_with_round()
    state['notifications'] = {'mode': 'concise'}
    state.update(status='ready_for_human', human_blockers=['旧待办'])
    pending = [events(state)[-1]]
    state.update(status='run_review_pending', human_blockers=[])
    assert recovery_event(state, pending) is None
    state['status'] = 'completed'
    recovered = recovery_event(state, pending)
    assert recovered and recovered['title'] == '任务已完成'
    assert '恢复' not in str(card(recovered))
    assert '旧待办' not in str(card(recovered))


def test_boundary_times_use_business_event_and_omit_incomplete_totals() -> None:
    state = state_with_round()
    state.update(created_at='2026-01-01T00:00:00+00:00', status='completed',
                 timeline=[{'at': '2026-01-01T00:10:00+00:00', 'status': 'completed'}])
    result = events(state)[-1]
    assert result['total_seconds'] == 9
    assert result['elapsed_seconds'] == 600
    state['agent_invocation_history'][0].pop('ended_at')
    result = events(state)[-1]
    assert 'total_seconds' not in result
    assert result['elapsed_seconds'] == 600


def test_ticket_total_omits_incomplete_unassociated_execution() -> None:
    state = state_with_round()
    state["ticket_jobs"] = {"2": {"phase": "completed", "generation": 1}}
    attempt = state["semantic_agent_attempts"][0]
    attempt["work_subject"] = "ticket:2"
    state["agent_invocation_history"][0]["semantic_attempt"]["work_subject"] = "ticket:2"
    state["agent_invocation_history"].append({
        "work_subject": "ticket:2", "started_at": "2026-01-01T01:00:00+00:00",
        "status": "completed",
    })
    completed = next(event for event in events(state) if event["kind"] == "ticket_completed")
    assert "total_seconds" not in completed


@pytest.mark.parametrize("status", ["blocked", "unsupported_scope_change", "deterministic_contradiction", "requeue_required", "publication_pending", "abandonment_pending"])
def test_concise_preserves_other_human_boundaries(status: str) -> None:
    state = state_with_round()
    state.update(status=status, blocked_reason="需要核对外部分支", notifications={"mode": "concise"})
    boundary = events(state)[-1]
    assert boundary["kind"] == "boundary"
    assert boundary["current"] is True
    assert "需要核对外部分支" in boundary["summary"]
    assert boundary["next_step"]


def test_cleanup_failure_is_not_final_completion() -> None:
    state = state_with_round()
    state.update(status="completed", run_publication={"phase": "merged", "pr_number": 9, "parent_closed": True},
                 delivery_cleanup={"status": "failed"}, notifications={"mode": "concise"})
    pending = events(state)[-1]
    assert pending["title"] == "已合并，待清理"
    assert pending["color"] == "yellow"
    assert pending["next_step"]
    state["delivery_cleanup"]["status"] = "completed"
    completed = events(state)[-1]
    assert completed["title"] == "任务已完成"
    assert completed["id"] != pending["id"]


@pytest.mark.parametrize("role, outcome", [("development", "no_code_changes"), ("publication", "currentness_invalidated")])
def test_incomplete_round_does_not_claim_work_prepared(role: str, outcome: str) -> None:
    ended = events(state_with_round(role, outcome))[-1]
    assert "等待验收" not in ended["title"]
    assert "已准备好" not in ended["title"]


def test_missing_review_lane_cannot_imply_acceptance() -> None:
    state = state_with_round("review", "acceptance_artifact")
    state["semantic_agent_attempts"][0]["acceptance_artifact"] = {
        "checks": {"spec": {"status": "pass"}}}
    ended = events(state)[-1]
    assert ended["title"] == "验收结果尚未确认"
    assert ended["color"] == "yellow"


@pytest.mark.parametrize("mode", ["concise", "detailed"])
@pytest.mark.parametrize("status", ["ready_for_human", "blocked", "execution_failed", "progress_exhausted", "supervision_timeout"])
def test_ticket_todo_uses_current_object_even_with_older_pr(mode: str, status: str) -> None:
    state = state_with_round()
    state.update(status=status, notifications={"mode": mode},
                 ticket_graph={"tickets": {"3": {"title": "子任务原始标题 2026-09-15"}}},
                 active_ticket_job={"ticket_number": 3, "phase": "blocked", "human_blockers": ["当前子任务问题"]},
                 ticket_jobs={"2": {"phase": "blocked", "human_blockers": ["旧任务问题"]}},
                 run_publication={"pr_number": 9, "phase": "completed"})
    pending = next(event for event in events(state) if event.get("current"))
    recovered = recovery_event(state, [pending])
    assert recovered is not None
    for notification in (pending, recovered):
        assert notification["task_number"] == 3
        assert notification["task_title"] == "子任务原始标题 2026-09-15"
        assert notification["url"] == "https://github.com/o/repo/issues/3"
        assert "当前子任务问题" in notification["summary"]
        assert "旧任务问题" not in notification["summary"]
    state["active_ticket_job"]["ticket_number"] = 4
    changed = next(event for event in events(state) if event.get("current"))
    assert changed["id"] != pending["id"]
    assert changed["task_number"] == 4
    assert changed["task_title"] == ""


@pytest.mark.parametrize("mode", ["concise", "detailed"])
def test_overall_todo_does_not_borrow_completed_ticket_identity(mode: str) -> None:
    state = state_with_round()
    state.update(status="ready_for_human", notifications={"mode": mode},
                 active_ticket_job={"ticket_number": 3, "phase": "completed"},
                 run_acceptance={"phase": "blocked", "human_blockers": ["整体验收问题"]},
                 run_publication={"pr_number": 9, "phase": "completed"})
    notification = next(event for event in events(state) if event.get("current"))
    assert notification["task_number"] == state["parent"]["number"]
    assert notification["task_title"] == state["parent"]["title"]
    assert notification["url"] == "https://github.com/o/repo/issues/241"
    assert "整体验收问题" in notification["summary"]


@pytest.mark.parametrize("mode", ["concise", "detailed"])
@pytest.mark.parametrize("status", [
    "run_review_pending", "ready_for_human", "operator_stopped", "abandoned",
    "execution_failed", "run_approval_pending", "completed",
])
def test_language_and_history_copy_do_not_change_business_facts(
    mode: str, status: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy
    from agent_run import notification_events

    state = state_with_round("review", "acceptance_artifact")
    state.update(status=status, notifications={"mode": mode},
                 human_blockers=["原始 blocker 原样"],
                 run_publication={"pr_number": 42, "required_checks_evidence": {"result": "pass"}})
    state["semantic_agent_attempts"][0]["acceptance_artifact"] = {
        "checks": {"spec": {"status": "fail", "findings": ["原始 finding 原样"]}}}
    chinese = events(state)
    original = notification_events.history_records

    def changed_copy(*args: Any, **kwargs: Any) -> Any:
        records = deepcopy(original(*args, **kwargs))
        for record in records:
            record.update(status_text="Arbitrary display text", role_label="任意角色文字")
        return records

    monkeypatch.setattr(notification_events, "history_records", changed_copy)
    assert events(state) == chinese
    state["language"] = "en"
    english = events(state)
    facts = ("id", "kind", "color", "current", "task_number", "task_title", "url", "query", "checks")
    assert [{key: event.get(key) for key in facts} for event in chinese] == [
        {key: event.get(key) for key in facts} for event in english]
    assert [event["title"] for event in chinese] != [event["title"] for event in english]
    assert all(event["language"] == "en" for event in english)
    ended = [event for event in english if event["kind"] == "stage_end"]
    if ended:
        assert "原始 finding 原样" in ended[0]["summary"]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_card_localizes_labels_and_preserves_external_text(language: str) -> None:
    state = state_with_round()
    state["language"] = language
    event = events(state)[-1]
    event.update(summary="原始 Agent 摘要", next_step="agent-run resume r", duration_seconds=484,
                 checks={"e2e": "pass", "spec": "blocked", "review": "fail"})
    rendered = card(event)
    content = rendered["body"]["elements"][0]["content"]
    assert "原始 Agent 摘要" in content
    assert "agent-run resume r" in content
    assert state["parent"]["title"] in content
    for fragment in (("功能验证：通过", "需求核对：受阻", "工程审查：未通过", "8 分 4 秒", "下一步")
                     if language == "zh" else
                     ("Functional verification: Passed", "Requirements verification: Blocked",
                      "Engineering review: Failed", "8 min 4 s", "Next step")):
        assert fragment in content


@pytest.mark.parametrize("language", ["zh", "en"])
def test_concise_ticket_start_requires_invocation_and_survives_rework(language: str) -> None:
    state = state_with_round()
    state.update(language=language, notifications={"mode": "concise"})
    attempt = state["semantic_agent_attempts"][0]
    attempt.update(work_subject="ticket:2", started_at="2026-01-01T00:00:00+00:00")
    state["ticket_graph"] = {"tickets": {"2": {"title": "Actual ticket"}}}
    invocation = state["agent_invocation_history"].pop()
    invocation["semantic_attempt"] = attempt.copy()
    assert not any(event["kind"] == "ticket_started" for event in events(state))
    state["agent_invocation_history"].append({key: value for key, value in invocation.items() if key != "reported_thread_id"})
    assert not any(event["kind"] == "ticket_started" for event in events(state))
    state["agent_invocation_history"][-1]["reported_thread_id"] = "actual-thread"
    started = next(event for event in events(state) if event["kind"] == "ticket_started")
    assert started["task_title"] == "Actual ticket"
    assert started["language"] == language
    later = {**invocation, "semantic_attempt": {**attempt, "attempt_id": "rework", "ordinal": 2}}
    state["agent_invocation_history"].append(later)
    assert [event["id"] for event in events(state) if event["kind"] == "ticket_started"] == [started["id"]]


def _formal_review_state(parent_only: bool = False) -> dict[str, Any]:
    state = state_with_round("reviewer", "acceptance_artifact")
    artifact = {"checks": {lane: {"status": "pass", "findings": []} for lane in ("e2e", "standards", "spec")}}
    subject = "parent:241" if parent_only else "run-acceptance:r"
    state["semantic_agent_attempts"][0].update(work_subject=subject, acceptance_artifact=artifact)
    state["agent_invocation_history"][0]["semantic_attempt"] = state["semantic_agent_attempts"][0].copy()
    state.update(delivery_type="parent_only" if parent_only else "multi_ticket", ticket_jobs={}, ticket_graph={"tickets": {}}, notifications={"mode": "concise"})
    state["parent_job" if parent_only else "run_acceptance"] = {
        "phase": "accepted", "candidate_sha": "head", "generation": 1,
        "acceptance_record": {"reviewed_candidate_sha": "head", "artifact": artifact}}
    return state


@pytest.mark.parametrize("parent_only", [False, True])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_formal_pass_and_approval_are_independent(parent_only: bool, language: str) -> None:
    state = _formal_review_state(parent_only)
    state["language"] = language
    before = events(state)
    kinds = [event["kind"] for event in before]
    assert kinds == (["run_start", "acceptance_passed"] if parent_only else
                     ["run_start", "acceptance_started", "acceptance_passed"])
    passed = next(event for event in before if event["kind"] == "acceptance_passed")
    state["status"] = "parent_approval_pending" if parent_only else "run_approval_pending"
    if parent_only:
        state["parent_job"]["phase"] = "ready_for_approval"
    after = events(state)
    assert next(event["id"] for event in after if event["kind"] == "acceptance_passed") == passed["id"]
    assert after[-1]["kind"] == "boundary"
    state["notifications"]["mode"] = "detailed"
    assert not any(event["kind"] == "stage_end" for event in events(state))


@pytest.mark.parametrize("invalid", ["stale", "missing_lane", "no_checks", "failed_without_findings", "unpromoted", "local_only", "missing_findings", "parent_revision", "graph_revision"])
def test_only_current_formal_full_acceptance_can_pass(invalid: str) -> None:
    state = _formal_review_state()
    run = state["run_acceptance"]
    artifact = run["acceptance_record"]["artifact"]
    if invalid == "stale":
        run["candidate_sha"] = "new-head"
    elif invalid == "missing_lane":
        del artifact["checks"]["spec"]
    elif invalid == "no_checks":
        artifact["checks"] = {}
    elif invalid == "failed_without_findings":
        artifact["checks"]["spec"]["status"] = "fail"
    elif invalid == "unpromoted":
        run.update(phase="repairing", repair_job={"acceptance_record": run["acceptance_record"]})
    elif invalid == "local_only":
        run["candidate_acceptance"] = run.pop("acceptance_record")
    elif invalid == "missing_findings":
        del artifact["checks"]["spec"]["findings"]
    elif invalid == "parent_revision":
        state["parent"]["revision"] = "changed"
    elif invalid == "graph_revision":
        state["ticket_graph"] = {"revision": "changed"}
    assert not any(event["kind"] == "acceptance_passed" for event in events(state))


def test_first_run_repair_uses_execution_fact_and_stable_identity() -> None:
    state = _formal_review_state()
    run = state["run_acceptance"]
    run["phase"] = "repairing"
    run["acceptance_record"]["artifact"]["checks"]["spec"]["status"] = "fail"
    planned = next(event for event in events(state) if event["kind"] == "acceptance_repair")
    assert "将" in planned["title"] and not planned["live"]
    state["active_agent_invocation"] = {"work_subject": "run-repair:r", "role": "development", "status": "running"}
    assert next(event for event in events(state) if event["kind"] == "acceptance_repair")["title"] == planned["title"]
    state["active_agent_invocation"]["started_at"] = "2026-01-01T00:00:01+00:00"
    state["active_agent_invocation"]["reported_thread_id"] = "actual-thread"
    active = next(event for event in events(state) if event["kind"] == "acceptance_repair")
    assert active["live"] and "正在" in active["title"] and active["id"] == planned["id"]
    run.update(repair_generation=2, repair_job={"repair_generation": 2})
    assert next(event for event in events(state) if event["kind"] == "acceptance_repair")["id"] == planned["id"]
    state["delivery_type"] = "parent_only"
    assert not any(event["kind"] in {"acceptance_started", "acceptance_repair"} for event in events(state))


def test_acceptance_merge_inspection_starts_before_reviewer() -> None:
    state = state_with_round()
    state.update(semantic_agent_attempts=[], agent_invocation_history=[], notifications={"mode": "concise"},
                 run_acceptance={"acceptance_generation": 1, "phase": "repairing"})
    assert [event["kind"] for event in events(state)] == ["run_start", "acceptance_started", "acceptance_repair"]


def test_preparing_invocation_does_not_emit_detailed_start_then_duplicate() -> None:
    state = state_with_round()
    state["agent_invocation_history"][0].pop("reported_thread_id")
    assert not any(event["kind"] == "stage_start" for event in events(state))


def test_formal_pass_for_new_reviewed_base_has_distinct_identity() -> None:
    state = _formal_review_state()
    first = next(event for event in events(state) if event["kind"] == "acceptance_passed")
    state["run_acceptance"]["acceptance_record"]["reviewed_default_base_sha"] = "new-base"
    second = next(event for event in events(state) if event["kind"] == "acceptance_passed")
    assert first["id"] != second["id"]


def test_detailed_unpromoted_review_does_not_claim_formal_pass() -> None:
    state = _formal_review_state()
    state["notifications"]["mode"] = "detailed"
    state["run_acceptance"]["phase"] = "repairing"
    state["run_acceptance"]["repair_job"] = {"phase": "accepted"}
    result = next(event for event in events(state) if event["kind"] == "stage_end")
    assert result["title"] == "验收结果已返回，等待正式确认"
    assert not any(event["kind"] == "acceptance_passed" for event in events(state))


def test_approval_summary_does_not_infer_pass_from_missing_findings() -> None:
    state = _formal_review_state()
    state["status"] = "run_approval_pending"
    del state["run_acceptance"]["acceptance_record"]["artifact"]["checks"]["spec"]["findings"]
    approval = next(event for event in events(state) if event["kind"] == "boundary")
    assert "尚无有效通过结论" in approval["summary"]
    assert not any(event["kind"] == "acceptance_passed" for event in events(state))
