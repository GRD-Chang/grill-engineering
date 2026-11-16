"""One cleanup allowance per normally returned notification close; no native proof."""
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent_run.notifications import sender


@pytest.mark.parametrize('with_call', [False, True])
def test_post_commit_normal_unproven_close_does_not_restart_cleanup_budget(monkeypatch, capsys, with_call):
    now, waits = [0.0], []

    def join(*, timeout):
        waits.append(timeout)
        now[0] += timeout

    observer = object.__new__(sender.NativeNotifications)
    observer.enabled = observer.status_enabled = True
    observer.condition = threading.Condition()
    observer.thread = SimpleNamespace(join=join, is_alive=lambda: True, ident=123)
    original_result = object()
    call = SimpleNamespace(cancel=threading.Event(), thread=observer.thread, result=original_result)
    observer._call = call if with_call else None
    observer._mode = 'running'
    observer._refresh_dirty = observer._status_dirty = False
    observer.status_failure_reason = None
    monkeypatch.setattr(sender, 'monotonic', lambda: now[0])
    monkeypatch.setattr(sender, 'NativeNotifications', lambda *_a, **_k: observer)
    sender.notify_committed(Path('/unused/database'), storage=object())
    grace = 2 * sender.WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS + 5
    assert waits == [1.0, grace] + ([0.0] if with_call else [])
    assert now[0] == 1.0 + grace
    assert not observer.closed and observer._mode == 'cancel_after_drain'
    assert observer.status_failure_reason == 'status_sender_cleanup_unproven'
    if with_call:
        assert call.cancel.is_set() and call.result is original_result
    assert capsys.readouterr().err.count('Notification sender cleanup unproven') == 1


@pytest.mark.parametrize('error', [OSError('ordinary close'), KeyboardInterrupt('interrupted close')])
def test_interrupted_first_close_still_gets_one_cancel_cleanup(monkeypatch, error):
    calls = []
    observer = SimpleNamespace(closed=False)

    def close(**options):
        calls.append(options)
        if len(calls) == 1:
            raise error
        observer.closed = True

    observer.close = close
    monkeypatch.setattr(sender, 'NativeNotifications', lambda *_a, **_k: observer)
    if isinstance(error, Exception):
        sender.notify_committed(Path('/unused/database'), storage=object())
    else:
        with pytest.raises(type(error)) as caught:
            sender.notify_committed(Path('/unused/database'), storage=object())
        assert caught.value is error
    assert calls == [{'timeout_seconds': 1.0}, {'drain': False}]
    assert observer.closed
