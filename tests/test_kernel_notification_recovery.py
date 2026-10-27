"""Recover a paused native sender without a second queue or a fresh budget."""
from __future__ import annotations

import agent_run.queries.read_facts

from pathlib import Path
from threading import Event
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
import agent_run.notifications.sender as notifications
from test_kernel_notifications import _kernel


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
        assert observer.failure_reason is not None
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


def test_claim_commit_tail_failure_is_known_unsent_and_does_not_block_sender(tmp_path: Path, monkeypatch) -> None:
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
        # Equivalent to a directory fsync failure after atomic replacement:
        # the durable sending claim exists, while no transport ran yet.
        if wrote_claim:
            interrupted.set()
            failed.set()
            raise OSError("claim replacement committed; final durability report unavailable")

    def transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append("sent")
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr(outbox, "transaction", commit_tail)
    observer = notifications.NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert failed.wait(3)
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        before = outbox.read_notifications(tmp_path, "run-native")["pending"][0]
        assert before["outcome"] == "sending" and before["attempts"] == 1 and sent == []
        monkeypatch.setattr(outbox, "transaction", original)
        observer.observe_committed()
        observer.close()
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert record["outcome"] == "success" and record["attempts"] == 2
        assert record["attempt_history"][0]["outcome"] == "failed"
        assert record["attempt_history"][0]["not_sent"] is True
        assert record["attempt_history"][1]["outcome"] == "success"
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
        kernel.control("stop", "current-local-gate", subject="native-ticket")
        observer.observe_committed()
        monkeypatch.setattr(notifications, "read_boundaries", unavailable)
        observer.observe_committed()
        assert cancelled.wait(3)
        monkeypatch.setattr(notifications, "read_boundaries", original)
        observer.observe_committed()  # Request, not permission to clear old cancellation.
        assert observer.cancel.is_set() and len(calls) == 1
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
    try:
        assert observer.failure_reason is not None
        monkeypatch.setattr(notifications, "read_boundaries", healthy_but_delayed)
        observer.observe_committed()
        assert entered.wait(3)
        observer.close(drain=False)
        # close requests cancellation; a deliberately held callback still owns
        # its thread until released. Do not call that physical cleanup proved.
        assert not observer.closed and observer.cancel.is_set()
        release.set()
        assert observer.thread is not None
        observer.thread.join(timeout=3)
        observer.close(drain=False)
        assert observer.closed and observer.cancel.is_set()
        assert not observer.thread.is_alive() and calls == []
        assert observer.cancel.is_set() and observer.failure_reason is not None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert all(item["attempts"] == 0 for item in document["pending"])
    finally:
        release.set()
        observer.close(drain=False)
        if observer.thread is not None:
            observer.thread.join(timeout=3)
