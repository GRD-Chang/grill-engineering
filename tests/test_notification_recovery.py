"""发送故障后的恢复和正常退出必须留下真实、可查询的结果。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications
from support.notification import parallel_state


def test_read_recovers_then_new_alert_is_delivered_on_normal_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = parallel_state()
    current["ticket_jobs"]["3"]["phase"] = "developing"
    read_failed = False
    sent: list[str] = []

    def load() -> dict[str, Any]:
        if read_failed:
            raise OSError("temporary state read failure")
        return deepcopy(current)

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        if cancel.is_set():
            return {"outcome": "failed", "reason": "cancelled before submission"}
        sent.append(rendered["header"]["subtitle"]["content"])
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, current, load_state=load)
    try:
        with sender.condition:
            assert sender.condition.wait_for(lambda: bool(sender.document.get("records")), timeout=3)
        read_failed = True
        sender.observe(current)
        assert read_notifications(tmp_path, "r")["unavailable_reason"]
        read_failed = False
        current["ticket_jobs"]["3"]["phase"] = "blocked"
        sender.observe(current)
    finally:
        sender.close()
    assert sent == ["example/project · #1", "example/project · #3"]
    assert sender.thread is not None and not sender.thread.is_alive()
    journal = read_notifications(tmp_path, "r")
    assert not journal["pending"] and not journal.get("unavailable_reason")
    alert, = [item for item in journal["records"] if item.get("work_subject") == "ticket:3"]
    assert alert["outcome"] == "success" and alert["attempts"] == 1


@pytest.mark.parametrize("recovery", ["observe", "close", "expired", "unavailable"])
def test_failed_read_preserves_existing_alert_budget_and_closes_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str,
) -> None:
    from types import SimpleNamespace

    now = [1000.25]
    current = parallel_state()
    failed = threading.Event()
    calls: list[str] = []
    broken = False

    def load() -> dict[str, Any]:
        if broken:
            failed.set()
            raise OSError("state read unavailable")
        return deepcopy(current)

    def unknown(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal broken
        if rendered["header"]["subtitle"]["content"].endswith("#3"):
            calls.append(identity)
            broken = True
            return {"outcome": "unknown", "reason": "receipt lost"}
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("agent_run.notifications.send", unknown)
    sender = Notifications(tmp_path, current, load_state=load)
    try:
        assert failed.wait(3)
        with sender.condition:
            assert sender.condition.wait_for(lambda: bool(sender.document.get("unavailable_reason")), timeout=3)
        before, = read_notifications(tmp_path, "r")["pending"]
        assert before["attempts"] == 1 and before["outcome"] == "unknown"
        assert before["deadline_at"] == 1060.25
        broken = recovery == "unavailable"
        if recovery == "expired":
            now[0] = 1060.26

        def recovered(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(identity)
            return {"outcome": "unknown", "reason": "receipt lost"}

        monkeypatch.setattr("agent_run.notifications.send", recovered)
        if recovery == "observe":
            sender.observe(current)
    finally:
        sender.close()
    assert sender.thread is not None and not sender.thread.is_alive()
    journal = read_notifications(tmp_path, "r")
    if recovery == "unavailable":
        alert, = journal["pending"]
        assert journal["unavailable_reason"] == "state read unavailable"
        assert alert["delivery_error"] == "state read unavailable"
        assert alert["outcome"] == "unknown" and alert["attempts"] == 1
        assert len(calls) == 1
    else:
        assert not journal["pending"] and not journal.get("unavailable_reason")
        alert, = [item for item in journal["records"] if item.get("work_subject") == "ticket:3"]
        assert alert["exhausted"]
        assert alert["exhaustion_reason"] == ("deadline" if recovery == "expired" else "max_attempts")
        assert alert["attempts"] == len(calls) == (1 if recovery == "expired" else 3)
    identity = alert.get("id") or alert["event"]["id"]
    assert identity == before["event"]["id"]
    assert alert["deadline_at"] == before["deadline_at"]
    assert len(set(calls)) == 1


def test_unrecovered_failure_keeps_unsent_alerts_explicit_and_query_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = parallel_state()
    entered, release = threading.Event(), threading.Event()
    broken = False

    def load() -> dict[str, Any]:
        if broken:
            raise OSError("state read unavailable")
        return deepcopy(current)

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(3)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, current, load_state=load)
    try:
        assert entered.wait(3)
        broken = True
        sender.observe(current)
    finally:
        release.set()
        sender.close()
    assert sender.thread is not None and not sender.thread.is_alive()
    journal = read_notifications(tmp_path, "r")
    alert, = journal["pending"]
    assert journal["unavailable_reason"] == "state read unavailable"
    assert alert["outcome"] == "failed" and alert["attempts"] == 0
    assert alert["delivery_error"] == "state read unavailable"
    assert alert["attempt_history"] == []
    before = {path: path.read_bytes() for path in (tmp_path / "notifications").iterdir()}
    assert read_notifications(tmp_path, "r") == journal
    assert {path: path.read_bytes() for path in before} == before


def test_initial_read_failure_recovers_at_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = parallel_state()
    broken = True
    sent: list[str] = []

    def load() -> dict[str, Any]:
        if broken:
            raise OSError("initial read failed")
        return current

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        assert not cancel.is_set()
        sent.append(identity)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, current, load_state=load)
    try:
        assert read_notifications(tmp_path, "r")["unavailable_reason"] == "initial read failed"
        broken = False
    finally:
        sender.close()
    journal = read_notifications(tmp_path, "r")
    assert not journal["pending"] and not journal.get("unavailable_reason")
    assert len(sent) == len(journal["records"]) == 2
    assert all(record["outcome"] == "success" and record["attempts"] == 1 for record in journal["records"])
    assert sender.thread is not None and not sender.thread.is_alive()


@pytest.mark.parametrize("use_loader", [False, True])
def test_transport_exception_recovers_with_accurate_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_loader: bool,
) -> None:
    current = parallel_state()
    keys: list[str] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        if rendered["header"]["subtitle"]["content"].endswith("#3"):
            keys.append(identity)
            if len(keys) == 1:
                raise OSError("transport unavailable")
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 0)
    sender = Notifications(tmp_path, current, load_state=(lambda: current) if use_loader else None)
    try:
        with sender.condition:
            assert sender.condition.wait_for(lambda: bool(sender.document.get("unavailable_reason")), timeout=3)
        before, = read_notifications(tmp_path, "r")["pending"]
        sender.observe(current)
    finally:
        sender.close()
    journal = read_notifications(tmp_path, "r")
    assert not journal["pending"] and not journal.get("unavailable_reason")
    alert, = [item for item in journal["records"] if item.get("work_subject") == "ticket:3"]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert alert["deadline_at"] == before["deadline_at"]
    assert [attempt["outcome"] for attempt in alert["attempt_history"]] == ["unknown", "success"]
    assert sender.thread is not None and not sender.thread.is_alive()


def test_unstarted_worker_does_not_clear_failure_on_successful_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_start(thread: threading.Thread) -> None:
        raise RuntimeError("cannot start notification thread")

    monkeypatch.setattr("agent_run.notifications.threading.Thread.start", fail_start)
    current = parallel_state()
    sender = Notifications(tmp_path, current, load_state=lambda: current)
    sender.observe(current)
    sender.close()
    journal = read_notifications(tmp_path, "r")
    assert journal["unavailable_reason"] == "cannot start notification thread"
    assert len(journal["pending"]) == 2
    assert all(item["outcome"] == "failed" and item["attempts"] == 0
               and item["attempt_history"] == [] for item in journal["pending"])
    assert sender.thread is None


def test_recovered_read_waits_for_cancelled_transport_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = parallel_state()
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    broken = False
    keys: list[str] = []

    def load() -> dict[str, Any]:
        if broken:
            raise OSError("temporary read failure")
        return current

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        if not rendered["header"]["subtitle"]["content"].endswith("#3"):
            return {"outcome": "success", "reason": None}
        keys.append(identity)
        if len(keys) == 1:
            entered.set()
            assert cancel.wait(3)
            cancelled.set()
            assert release.wait(3)
            assert cancel.is_set()
            return {"outcome": "unknown", "reason": "cancelled after submission"}
        assert not cancel.is_set()
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, current, load_state=load)
    try:
        assert entered.wait(3)
        broken = True
        sender.observe(current)
        assert cancelled.wait(3)
        broken = False
        sender.observe(current)
        before = read_notifications(tmp_path, "r")
        assert before["unavailable_reason"] == "temporary read failure"
        pending, = before["pending"]
        assert pending["outcome"] == "sending" and pending["attempts"] == 1
        assert "finished_at" not in pending["attempt_history"][0]
    finally:
        release.set()
        sender.close()
    journal = read_notifications(tmp_path, "r")
    assert not journal["pending"] and not journal.get("unavailable_reason")
    alert, = [item for item in journal["records"] if item.get("work_subject") == "ticket:3"]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert alert["deadline_at"] == pending["deadline_at"]
    assert [attempt["outcome"] for attempt in alert["attempt_history"]] == ["unknown", "success"]
    assert alert["attempt_history"][0]["cancelled"]
    assert sender.thread is not None and not sender.thread.is_alive()
