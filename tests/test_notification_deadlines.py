"""投递期限包含持久化和退避耗时；时钟只在受控边界推进。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications
from agent_run import notification_transport as transport
from support.notification import parallel_state


def test_persistence_time_is_deducted_before_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.25]
    monkeypatch.setattr("agent_run.notifications.time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr("agent_run.notification_outbox.DELIVERY_LIFETIME_SECONDS", 5)
    fsync = os.fsync
    # Admission file/directory, then claim file/directory. Both are inside the
    # original five-second lifetime, including the first transport attempt.
    costs = iter([0.25, 0.25, 0.5, 0.5])

    def slow_fsync(fd: int) -> None:
        fsync(fd)
        now[0] += next(costs, 0)

    monkeypatch.setattr("agent_run.notification_outbox.os.fsync", slow_fsync)
    remaining: list[float] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, *, timeout_seconds: float) -> dict[str, Any]:
        remaining.append(timeout_seconds)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, parallel_state())
    sender.close()
    assert remaining == [3.5, 3.5]
    first = read_notifications(tmp_path, "r")["records"][0]
    assert first["created_at"] == 1000.25
    assert first["attempt_history"][0]["started_at"] == 1000.75
    assert first["deadline_at"] == 1005.25


def test_retry_wait_crossing_deadline_does_not_claim_another_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.25]
    waits: list[float] = []

    class AdvancingCondition(threading.Condition):
        def wait(self, timeout: float | None = None) -> bool:
            if timeout is None:
                return super().wait(timeout=2)
            waits.append(timeout)
            now[0] += timeout + 0.01
            return True

    monkeypatch.setattr("agent_run.notifications.time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr("agent_run.notifications.threading", SimpleNamespace(
        Condition=AdvancingCondition, Event=threading.Event, Thread=threading.Thread))
    monkeypatch.setattr("agent_run.notification_outbox.DELIVERY_LIFETIME_SECONDS", 0.5)
    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 1)
    calls: list[str] = []

    def unknown(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "unknown", "reason": "receipt lost"}

    monkeypatch.setattr("agent_run.notifications.send", unknown)
    sender = Notifications(tmp_path, parallel_state())
    sender.close()
    assert len(calls) == len(set(calls)) == 2
    assert waits == [0.5]
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert record["attempts"] == 1 and record["exhaustion_reason"] == "deadline"


@pytest.mark.parametrize("remaining,expected", [(0.25, 1000.5), (20, 1010.25)])
def test_transport_shares_remaining_deadline_across_identity_and_send(
    monkeypatch: pytest.MonkeyPatch, remaining: float, expected: float,
) -> None:
    now = [1000.25]
    deadlines: list[float] = []
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def command(argv: list[str], deadline: float, cancel: threading.Event) -> Any:
        deadlines.append(deadline)
        now[0] += 0.1
        payload = ({"profile": "test", "appId": "app", "identity": "bot", "available": True}
                   if "whoami" in argv else {"data": {"message_id": "message"}})
        return SimpleNamespace(code=0, output=json.dumps(payload).encode(), reason="")

    monkeypatch.setattr(transport, "_run", command)
    result = transport.send({"enabled": True, "profile": "test", "app_id": "app", "open_id": "ou_test"},
                            {}, "key", threading.Event(), timeout_seconds=remaining)
    assert result["outcome"] == "success"
    assert deadlines == [expected, expected]
