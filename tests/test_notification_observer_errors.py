"""独立错误通道不能被业务通知降级、恢复或取消清除。"""
from __future__ import annotations

from pathlib import Path
from threading import Event, current_thread, main_thread
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications
from support.observer_errors import ObserverErrors


@pytest.mark.parametrize("failure", [None, AssertionError("controlled observer assertion"), OSError("controlled state read")],
                         ids=["success", "assertion", "read-error"])
def test_observer_errors_reach_caller_after_business_catch_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException | None,
) -> None:
    state: dict[str, Any] = {
        "run_id": "exact-run", "repository": "example/project", "language": "en",
        "parent": {"number": 1, "title": "Original title"}, "status": "starting",
        "notifications": {"enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test"},
    }
    errors = ObserverErrors()
    handled, sent = Event(), Event()
    unavailable = Notifications._unavailable

    def load() -> dict[str, Any]:
        with errors.capture():
            if failure is not None and current_thread() is not main_thread():
                raise failure
            return state.copy()

    def record_unavailable(self: Notifications, error: Exception, **options: Any) -> None:
        unavailable(self, error, **options)
        handled.set()

    def send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.set()
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr(Notifications, "_unavailable", record_unavailable)
    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, state, load_state=load)
    try:
        assert (handled if failure is not None else sent).wait(5)
        if failure is not None:
            assert sender.failure_reason == str(failure)
    finally:
        sender.close()
    assert sender.thread is not None and not sender.thread.is_alive()
    if failure is None:
        errors.raise_if_any()
        assert read_notifications(tmp_path, "exact-run")["pending"] == []
    else:
        with pytest.raises(type(failure), match=str(failure)) as caught:
            errors.raise_if_any()
        assert caught.value is failure
        # A second close/cancellation cannot turn the independent check green.
        sender.close()
        with pytest.raises(type(failure)):
            errors.raise_if_any()
