from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications


def state(status: str = "starting") -> dict[str, Any]:
    return {"run_id": "run-test", "repository": "example/project", "parent": {"number": 1, "title": "任务"},
            "status": status, "notifications": {"enabled": True, "profile": "work", "open_id": "ou_test", "app_id": "cli_test"}}


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


def test_delivery_deduplicates_and_notifies_a_new_blocker_occurrence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    def sent(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    current = state("ready_for_human")
    sender = Notifications(tmp_path, current)
    sender.observe(current)
    sender.close()
    first = len(calls)
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == first
    sender = Notifications(tmp_path, current)
    sender.observe(state("running"))
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


def test_failure_retry_is_bounded_and_recovery_uses_current_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    def failed(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "failed", "reason": "offline"}
    monkeypatch.setattr("agent_run.notifications.send", failed)
    current = state("ready_for_human")
    current["human_blockers"] = ["OLD BLOCKER"]
    sender = Notifications(tmp_path, current)
    sender.close()
    assert len(calls) == 6
    assert all(item["attempts"] == 3 for item in read_notifications(tmp_path, "run-test")["pending"])
    def sent(config: Any, card: Any, identity: Any, cancel: Any) -> dict[str, Any]:
        calls.append(card)
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr("agent_run.notifications.send", sent)
    sender = Notifications(tmp_path, state("completed"))
    sender.close()
    assert "OLD BLOCKER" not in json.dumps(calls[-1])
    assert calls[-1]["header"]["title"]["content"] == "任务已完成"
    assert not read_notifications(tmp_path, "run-test")["pending"]


def test_unknown_delivery_is_not_replayed_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def unknown(*args: Any) -> dict[str, Any]:
        calls.append(1)
        return {"outcome": "unknown", "reason": "lost response"}
    monkeypatch.setattr("agent_run.notifications.send", unknown)
    sender = Notifications(tmp_path, state())
    sender.close()
    sender = Notifications(tmp_path, state())
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


def test_repeated_unchanged_observations_do_not_retry_failed_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    def failed(*args: Any) -> dict[str, Any]:
        calls.append(1)
        return {"outcome": "failed", "reason": "offline"}
    monkeypatch.setattr("agent_run.notifications.send", failed)
    sender = Notifications(tmp_path, state())
    sender.close()
    before = read_notifications(tmp_path, "run-test")["pending"]
    for _ in range(5):
        sender.observe(state())
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
