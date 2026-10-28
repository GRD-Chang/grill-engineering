"""Recover a paused native sender without a second queue or a fresh budget."""
from __future__ import annotations

import agent_run.queries.read_facts

from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
import agent_run.notifications.sender as notifications
from support.notification import _kernel, observe_and_wait


@pytest.mark.parametrize("recovery_request", ["business", "close"])
def test_healthy_observation_recovers_the_original_sender(tmp_path: Path, monkeypatch, recovery_request: str) -> None:
    kernel = _kernel(tmp_path)
    delivered = Event()
    sent: list[str] = []
    original = agent_run.queries.read_facts.read_boundaries

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("notification read temporarily unavailable")

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered["header"]["title"]["content"])
        delivered.set()
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr(notifications, "read_boundaries", unavailable)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        assert sent == []
        monkeypatch.setattr(notifications, "read_boundaries", original)
        if recovery_request == "business":
            observer.observe_committed()
            assert delivered.wait(3)
        observer.close()
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document.get("unavailable_reason") is None
        assert len(document["records"]) == 1 and document["records"][0]["attempts"] == 1
        assert document["records"][0]["outcome"] == "success"
        assert len(sent) == 1
    finally:
        observer.close(drain=False)


@pytest.mark.parametrize("outcome", ["success", "unknown"])
def test_returned_result_survives_journal_failure_with_original_end_time(tmp_path: Path, monkeypatch, outcome: str) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    kernel = _kernel(tmp_path)
    clock = [123.25]
    monkeypatch.setattr(notifications, "time", SimpleNamespace(time=lambda: clock[0]))
    original = outbox.transaction
    broken, failed = Event(), Event()
    sent: list[str] = []

    @contextmanager
    def interrupted(*args: Any, **kwargs: Any):
        with original(*args, **kwargs) as document:
            yield document
            if broken.is_set():
                failed.set()
                raise OSError("journal unavailable after physical return")

    def transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(outcome)
        clock[0] = 124.25
        broken.set()
        return {"outcome": outcome, "reason": "original physical result"}

    monkeypatch.setattr(outbox, "transaction", interrupted)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert failed.wait(3)
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        before = outbox.read_notifications(tmp_path, "run-native")["pending"][0]
        assert before["outcome"] == "sending" and before["attempts"] == 1
        deadline = before["deadline_at"]
        clock[0] = 134.75
        broken.clear()
        monkeypatch.setattr(outbox, "transaction", original)
        observer.observe_committed()
        observer.close()
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert record["outcome"] == outcome and record["attempts"] == 1
        assert record["deadline_at"] == deadline
        assert record["attempt_history"][0]["finished_at"] == 124.25
        assert sent == [outcome]
    finally:
        broken.clear()
        monkeypatch.setattr(outbox, "transaction", original)
        observer.close(drain=False)


@pytest.mark.parametrize("commit_point", ["after_replace", "rollback"])
def test_claim_commit_tail_failure_is_known_unsent_and_does_not_block_sender(tmp_path: Path, monkeypatch, commit_point) -> None:
    from contextlib import contextmanager

    kernel = _kernel(tmp_path)
    original = outbox.transaction
    interrupted, failed = Event(), Event()
    sent: list[str] = []

    @contextmanager
    def commit_tail(*args: Any, **kwargs: Any):
        wrote_claim = False
        with original(*args, **kwargs) as document:
            yield document
            wrote_claim = (not interrupted.is_set() and any(
                item["outcome"] == "sending" for item in document["pending"]))
            if wrote_claim and commit_point == "rollback":
                interrupted.set()
                failed.set()
                raise OSError("claim transaction rolled back before replacement")
        # Equivalent to a directory fsync failure after atomic replacement:
        # the durable sending claim exists, while no transport ran yet.
        if wrote_claim:
            interrupted.set()
            failed.set()
            raise OSError("claim replacement committed; final durability report unavailable")

    def transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append("sent")
        return {"outcome": "success", "reason": None}

    settled = Event()
    original_finish = notifications.NativeNotifications._finish_result

    def finish(self, claim, result, returned_at):
        settled.set()
        return original_finish(self, claim, result, returned_at)

    monkeypatch.setattr(notifications.NativeNotifications, "_finish_result", finish)
    monkeypatch.setattr(outbox, "transaction", commit_tail)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert failed.wait(3)
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        before = outbox.read_notifications(tmp_path, "run-native")["pending"][0]
        assert before["outcome"] == ("sending" if commit_point == "after_replace" else "pending")
        assert before["attempts"] == int(commit_point == "after_replace") and sent == []
        assert not settled.wait(0.1), "A failed claim retried settlement without a new observation"
        monkeypatch.setattr(outbox, "transaction", original)
        observer.observe_committed()
        observer.close()
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert record["outcome"] == "success" and record["attempts"] == (2 if commit_point == "after_replace" else 1)
        if commit_point == "after_replace":
            assert record["attempt_history"][0]["outcome"] == "failed"
            assert record["attempt_history"][0]["not_sent"] is True
        assert record["attempt_history"][-1]["outcome"] == "success"
        assert record["deadline_at"] == before["deadline_at"]
        assert sent == ["sent"]  # Existing failed-attempt retry, not a new budget.
    finally:
        monkeypatch.setattr(outbox, "transaction", original)
        observer.close(drain=False)


def test_inflight_cancel_finishes_before_the_requested_recovery(tmp_path: Path, monkeypatch) -> None:
    kernel = _kernel(tmp_path)
    started, cancelled, release, returned = Event(), Event(), Event(), Event()
    original = agent_run.queries.read_facts.read_boundaries
    calls: list[str] = []

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("temporary snapshot failure while a send is active")

    def transport(_config: Any, rendered: Any, _identity: Any, cancel: Event,
                  **_kwargs: Any) -> dict[str, Any]:
        title = rendered["header"]["title"]["content"]
        calls.append(title)
        if len(calls) == 1:
            started.set()
            assert cancel.wait(3)
            cancelled.set()
            assert release.wait(5)
            assert cancel.is_set(), "a recovery request uncancelled the old active call"
            returned.set()
            return {"outcome": "unknown", "reason": "original in-flight cancellation"}
        assert returned.is_set(), "new transport overlapped the unfinished original call"
        return {"outcome": "success", "reason": None}

    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert started.wait(3)
        original_cancel = observer._call.cancel
        kernel.control("stop", "current-local-gate", subject="native-ticket")
        observe_and_wait(observer)
        monkeypatch.setattr(notifications, "read_boundaries", unavailable)
        observer.observe_committed()
        assert cancelled.wait(3)
        monkeypatch.setattr(notifications, "read_boundaries", original)
        observer.observe_committed()  # Request, not permission to clear old cancellation.
        assert original_cancel.is_set() and len(calls) == 1
        release.set()
        observer.close()
        assert observer.failure_reason is None
        records = outbox.read_notifications(tmp_path, "run-native")["records"]
        original_send = next(row for row in records if row["kind"] == "run_start")
        boundary = next(row for row in records if row["kind"] == "boundary")
        assert original_send["outcome"] == "unknown" and original_send["attempts"] == 1
        assert boundary["outcome"] == "success" and boundary["attempts"] == 1
        assert len(calls) == 2
    finally:
        release.set()
        monkeypatch.setattr(notifications, "read_boundaries", original)
        observer.close(drain=False)


def test_fatal_close_wins_over_a_late_healthy_recovery_read(tmp_path: Path, monkeypatch) -> None:
    kernel = _kernel(tmp_path)
    entered, release = Event(), Event()
    original = agent_run.queries.read_facts.read_boundaries
    calls: list[str] = []

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("initial observer read unavailable")

    def healthy_but_delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append("sent")
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr(notifications, "read_boundaries", unavailable)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    close_returned = Event()
    close_errors: list[Exception] = []

    def close_sender() -> None:
        try:
            observer.close(drain=False)
        except Exception as error:
            close_errors.append(error)
        finally:
            close_returned.set()

    closing = Thread(target=close_sender, name='test-notification-close')
    try:
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        monkeypatch.setattr(notifications, "read_boundaries", healthy_but_delayed)
        observer.observe_committed()
        assert entered.wait(3)
        closing.start()
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer._mode == "cancel", timeout=3)
        assert observer._mode == "cancel"
        # close requests cancellation; a deliberately held callback still owns
        # its thread until released. Do not call that physical cleanup proved.
        assert not observer.closed and observer._mode == "cancel" and not close_returned.is_set()
        release.set()
        closing.join(timeout=3)
        assert close_returned.is_set() and not closing.is_alive() and close_errors == []
        assert observer.thread is not None
        observer.thread.join(timeout=3)
        observer.close(drain=False)
        assert observer.closed and observer._mode == "cancel"
        assert not observer.thread.is_alive() and calls == []
        assert observer._mode == "cancel" and observer.failure_reason is not None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document['unavailable_reason'] == observer.failure_reason
        assert all(item["attempts"] == 0 for item in document["pending"])
    finally:
        release.set()
        if closing.ident is not None:
            closing.join(timeout=3)
        observer.close(drain=False)
        if observer.thread is not None:
            observer.thread.join(timeout=3)


def test_fatal_close_mirrors_recovery_committed_before_cancellation(tmp_path, monkeypatch):
    """A successful durable recovery stays true when close races its commit tail."""
    from contextlib import contextmanager

    kernel = _kernel(tmp_path)
    committed, release = Event(), Event()
    original_transaction = outbox.transaction
    original_boundaries = notifications.read_boundaries
    calls = []

    def unavailable(*_args, **_kwargs):
        raise OSError('original observation unavailable')

    @contextmanager
    def recovery_commit_tail(*args, **kwargs):
        with original_transaction(*args, **kwargs) as document:
            paused = document.get('unavailable_reason') is not None
            yield document
            cleared = paused and document.get('unavailable_reason') is None
        if cleared:
            committed.set()
            assert release.wait(3)

    monkeypatch.setattr(notifications, 'read_boundaries', unavailable)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path,
        transport=lambda *_a, **_k: calls.append(True) or {'outcome': 'success', 'reason': None},
        storage=kernel.storage)
    closer = Thread(target=lambda: observer.close(drain=False))
    try:
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        monkeypatch.setattr(notifications, 'read_boundaries', original_boundaries)
        monkeypatch.setattr(outbox, 'transaction', recovery_commit_tail)
        observer.observe_committed()
        assert committed.wait(3)
        assert 'unavailable_reason' not in outbox.read_notifications(tmp_path, 'run-native')
        closer.start()
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer._mode == 'cancel', timeout=3)
        release.set()
        closer.join(3)
        assert not closer.is_alive() and observer.closed and calls == []
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, 'run-native')
        assert 'unavailable_reason' not in document
        assert document['records'] == []
        assert all(item['attempts'] == 0 for item in document['pending'])
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(3)
        observer.close(drain=False)
        assert observer.closed


def test_rolled_back_retry_claim_cannot_rewrite_the_previous_real_attempt(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    kernel = _kernel(tmp_path)
    clock = [100.25]
    monkeypatch.setattr(notifications, 'time', SimpleNamespace(time=lambda: clock[0]))
    original_transaction = outbox.transaction
    original_finish = notifications.NativeNotifications._finish_result
    first_finished, rolled_back = Event(), Event()
    original_history, calls, preserved = [], [], []

    @contextmanager
    def rollback_retry(*args, **kwargs):
        with original_transaction(*args, **kwargs) as document:
            yield document
            if not rolled_back.is_set() and any(item['attempts'] == 2 and item['outcome'] == 'sending'
                                                for item in document['pending']):
                rolled_back.set()
                raise OSError('second claim rolled back before replacement')

    def transport(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {'outcome': 'failed', 'reason': 'original proved failure'}
        clock[0] = 102.25
        return {'outcome': 'success', 'reason': None}

    def finish(self, claim, result, returned_at):
        original_finish(self, claim, result, returned_at)
        if len(calls) == 1:
            item, = outbox.read_notifications(tmp_path, 'run-native')['pending']
            if not first_finished.is_set():
                original_history.extend(item['attempt_history'])
                first_finished.set()
            elif rolled_back.is_set():
                assert item['attempt_history'] == original_history
                preserved.append(True)

    monkeypatch.setattr(notifications.NativeNotifications, '_finish_result', finish)
    monkeypatch.setattr(outbox, 'transaction', rollback_retry)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert first_finished.wait(3)
        clock[0] = 101.5
        observer.observe_committed()
        assert rolled_back.wait(3)
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        monkeypatch.setattr(outbox, 'transaction', original_transaction)
        observer.observe_committed()
        observer.close(timeout_seconds=3)
        assert observer.failure_reason is None and preserved and len(calls) == 2
        record, = outbox.read_notifications(tmp_path, 'run-native')['records']
        assert record['attempts'] == 2 and record['outcome'] == 'success'
        assert record['attempt_history'][0] == original_history[0]
        assert record['attempt_history'][0]['finished_at'] == 100.25
        assert record['deadline_at'] == 160.25
    finally:
        monkeypatch.setattr(outbox, 'transaction', original_transaction)
        observer.close(drain=False)
        assert observer.closed
