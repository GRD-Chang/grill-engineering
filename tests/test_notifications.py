from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications


def state(status: str = "starting", language: str = "zh") -> dict[str, Any]:
    return {"language": language, "run_id": "run-test", "repository": "example/project", "parent": {"number": 1, "title": "任务"},
            "status": status, "notifications": {"enabled": True, "profile": "work", "open_id": "ou_test", "app_id": "cli_test"}}


@pytest.fixture(params=["zh", "en"])
def language(request: pytest.FixtureRequest) -> str:
    return str(request.param)

def test_disabled_has_no_sender_or_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any) -> None:
        pytest.fail("disabled notification invoked transport")
    monkeypatch.setattr("agent_run.notifications.send", unexpected)
    current = state()
    current["notifications"] = {"enabled": False}
    sender = Notifications(tmp_path, current)
    sender.observe(current)
    sender.close()
    assert not (tmp_path / "notifications").exists()


def test_delivery_deduplicates_and_notifies_a_new_blocker_occurrence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    calls: list[dict[str, Any]] = []
    def sent(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state("ready_for_human", language=language)
    sender = Notifications(tmp_path, current)
    sender.observe(current)
    sender.close()
    first = len(calls)
    current["language"] = "en" if language == "zh" else "zh"
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == first
    sender = Notifications(tmp_path, current)
    sender.observe(state("running", language=language))
    sender.observe(current)
    sender.close()
    assert len(calls) == first + 1
    assert read_notifications(tmp_path, "run-test")["pending"] == []


def test_slow_send_does_not_block_observation_and_close_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    stopped = threading.Event()
    def slow(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        entered.set()
        assert cancel.wait(5)
        stopped.set()
        return {"outcome": "unknown", "reason": "cancelled"}
    monkeypatch.setattr("agent_run.notifications.send", slow)
    sender = Notifications(tmp_path, state())
    try:
        assert entered.wait(2)
        sender.observe(state("ready_for_human"))
        assert not stopped.is_set()
    finally:
        sender.close()
    assert stopped.is_set()
    assert any(item["outcome"] == "unknown" for item in read_notifications(tmp_path, "run-test")["pending"])


def test_failure_retry_is_bounded_and_recovery_uses_current_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    calls: list[dict[str, Any]] = []
    def failed(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "failed", "reason": "offline"}
    monkeypatch.setattr("agent_run.notifications.send", failed)
    current = state("ready_for_human", language=language)
    current["human_blockers"] = ["OLD BLOCKER"]
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == 6
    assert all(item["attempts"] == 3 for item in read_notifications(tmp_path, "run-test")["pending"])
    def sent(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    sender = Notifications(tmp_path, state("completed", language=language))
    sender.close()
    assert "OLD BLOCKER" not in json.dumps(calls[-1])
    assert calls[-1]["header"]["title"]["content"] == ("任务已完成" if language == "zh" else "Task completed")
    assert not read_notifications(tmp_path, "run-test")["pending"]


def test_unknown_delivery_is_not_replayed_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    calls = []
    def unknown(*args: Any) -> dict[str, Any]:
        calls.append(1)
        return {"outcome": "unknown", "reason": "lost response"}
    monkeypatch.setattr("agent_run.notifications.send", unknown)
    sender = Notifications(tmp_path, state(language=language))
    sender.close()
    sender = Notifications(tmp_path, state(language=language))
    sender.close()
    assert calls == [1]
    assert read_notifications(tmp_path, "run-test")["pending"][0]["outcome"] == "unknown"


def test_explicit_recovery_retries_a_single_failed_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    def failed(*args: Any) -> dict[str, Any]:
        calls.append(1)
        return {"outcome": "failed", "reason": "offline"}
    monkeypatch.setattr("agent_run.notifications.send", failed)
    sender = Notifications(tmp_path, state())
    sender.close()
    assert len(calls) == 3
    sender = Notifications(tmp_path, state())
    sender.close()
    assert len(calls) == 6


def test_notification_receipts_are_bounded_without_replaying_visible_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def sent(config: Any, card: Any, identity: str, cancel: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    monkeypatch.setattr("agent_run.notifications.MAX_RECORDS", 3)
    current = state()
    for _ in range(5):
        sender = Notifications(tmp_path, current)
        sender.observe(state("ready_for_human"))
        sender.close()
    document = read_notifications(tmp_path, "run-test")
    assert len(document["records"]) == 3
    assert len(calls) == 6  # Run start once, plus each real re-entry.


def test_overlapping_control_sender_does_not_duplicate_inflight_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    def sent(config: Any, card: Any, identity: str, cancel: Any) -> dict[str, Any]:
        calls.append(identity)
        entered.set()
        assert release.wait(4)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    first = Notifications(tmp_path, state())
    second = None
    try:
        assert entered.wait(2)
        second = Notifications(tmp_path, state())
    finally:
        release.set()
        first.close()
        if second is not None:
            second.close()
    assert len(calls) == 1
    assert not read_notifications(tmp_path, "run-test")["pending"]


def test_repeated_unchanged_observations_do_not_retry_failed_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    calls: list[int] = []
    def failed(*args: Any) -> dict[str, Any]:
        calls.append(1)
        return {"outcome": "failed", "reason": "offline"}
    monkeypatch.setattr("agent_run.notifications.send", failed)
    sender = Notifications(tmp_path, state(language=language))
    sender.close()
    before = read_notifications(tmp_path, "run-test")["pending"]
    for _ in range(5):
        sender.observe(state(language=language))
    after = read_notifications(tmp_path, "run-test")["pending"]
    assert after == before
    assert len(calls) == 3


@pytest.mark.parametrize("mode", ["concise", "detailed"])
def test_recovered_ticket_todo_sends_the_ticket_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    current = state("ready_for_human")
    current["notifications"]["mode"] = mode
    current["ticket_graph"] = {"tickets": {"3": {"title": "子任务标题"}}}
    current["active_ticket_job"] = {
        "ticket_number": 3, "phase": "blocked", "human_blockers": ["测试账号不可用"],
    }
    monkeypatch.setattr("agent_run.notifications.send", lambda *args: {
        "outcome": "failed", "reason": "offline",
    })
    sender = Notifications(tmp_path, current)
    sender.close()
    assert read_notifications(tmp_path, "run-test")["pending"]

    delivered: list[dict[str, Any]] = []

    def sent(config: Any, card: dict[str, Any], identity: Any, cancel: Any) -> dict[str, Any]:
        delivered.append(card)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", sent)
    recovered = Notifications(tmp_path, current)
    recovered.close()
    assert len(delivered) == 1
    assert delivered[0]["header"]["subtitle"]["content"] == "example/project · #3"
    assert "子任务标题" in str(delivered[0])
    assert "https://github.com/example/project/issues/3" in str(delivered[0])
    assert "https://github.com/example/project/issues/1" not in str(delivered[0])
    assert not read_notifications(tmp_path, "run-test")["pending"]


@pytest.mark.parametrize("outcome,status,title", [
    ("failed", "execution_failed", "人工恢复失败"),
    ("human_action", "ready_for_human", "人工恢复后需要你处理"),
])
def test_resume_result_replaces_fault_and_survives_observer_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    outcome: str, status: str, title: str,
) -> None:
    calls: list[dict[str, Any]] = []
    def sent(config: Any, rendered: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state(status)
    current["notifications"]["mode"] = "concise"
    sender = Notifications(tmp_path, current)
    sender.close()
    baseline = len(calls)
    current["_manual_resume_result"] = {
        "id": "manual-action-1", "outcome": outcome,
        "evidence": "preflight", "reason": "original diagnostic",
    }
    sender = Notifications(tmp_path, current)
    sender.observe(current)
    sender.close()
    assert len(calls) == baseline + 1
    assert calls[-1]["header"]["title"]["content"] == title
    assert "original diagnostic" in str(calls[-1])
    current.pop("_manual_resume_result")
    restarted = Notifications(tmp_path, current)
    restarted.close()
    assert len(calls) == baseline + 1


def test_resume_request_and_unproven_success_never_send_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    def sent(config: Any, rendered: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state("running")
    current["notifications"]["mode"] = "concise"
    sender = Notifications(tmp_path, current)
    for evidence in ("request_received", "exit_zero", None):
        current["_manual_resume_result"] = {
            "id": "manual-action-1", "outcome": "started", "evidence": evidence,
        }
        sender.observe(current)
    sender.close()
    assert len(calls) == 1
    assert calls[0]["header"]["title"]["content"] == "任务已开始"


def test_first_ticket_start_is_not_replayed_after_history_rolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_notification_events import state_with_round

    calls: list[dict[str, Any]] = []
    def sent(config: Any, rendered: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state_with_round()
    current["notifications"] = {**state()["notifications"], "mode": "concise"}
    current["ticket_graph"] = {"tickets": {"3": {"title": "Feature"}}}
    attempt = current["semantic_agent_attempts"][0]
    attempt["work_subject"] = "ticket:3"
    invocation = current["agent_invocation_history"][0]
    invocation["semantic_attempt"] = attempt.copy()
    sender = Notifications(tmp_path, current)
    sender.close()
    first = len(calls)
    current.update(semantic_agent_attempts=[], agent_invocation_history=[])
    sender = Notifications(tmp_path, current)
    sender.close()
    attempt = {**attempt, "attempt_id": "new-thread-repair", "ordinal": 2}
    current.update(semantic_agent_attempts=[attempt],
                   agent_invocation_history=[{**invocation, "semantic_attempt": attempt}])
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == first


def test_resume_fault_does_not_reappear_as_detailed_round_result_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_notification_events import state_with_round

    calls: list[dict[str, Any]] = []
    def sent(config: Any, rendered: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state_with_round()
    current["status"] = "execution_failed"
    current["notifications"] = {**state()["notifications"], "mode": "detailed"}
    current["semantic_agent_attempts"][0].update(status="pending", outcome=None)
    invocation = current["agent_invocation_history"][0]
    invocation.update(status="execution_failed", error="failed to start")
    invocation["semantic_attempt"] = current["semantic_agent_attempts"][0].copy()
    current["_manual_resume_result"] = {"id": "manual-action-1", "outcome": "failed", "evidence": "execution"}
    sender = Notifications(tmp_path, current)
    sender.close()
    first = len(calls)
    assert any(item["header"]["title"]["content"] == "人工恢复失败" for item in calls)
    current.pop("_manual_resume_result")
    restarted = Notifications(tmp_path, current)
    restarted.close()
    assert len(calls) == first


def test_resume_result_identity_survives_absent_transient_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    def sent(config: Any, rendered: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state("running")
    result = {"id": "same-action", "outcome": "started", "evidence": "controller_check"}
    current["_manual_resume_result"] = result
    sender = Notifications(tmp_path, current)
    sender.close()
    current.pop("_manual_resume_result")
    sender = Notifications(tmp_path, current)
    sender.close()
    current["_manual_resume_result"] = result
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == 2  # Run start and the same actual Resume result once.
