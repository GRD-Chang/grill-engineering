"""投递期限包含持久化和退避耗时；时钟只在受控边界推进。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

import agent_run.adapters.notification_transport as transport


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
