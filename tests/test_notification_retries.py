"""Persistent delivery budgets through the existing notification entrypoint."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.notifications import Notifications, read_notifications
from support.notification import parallel_state


def test_important_unknown_retries_keep_identity_and_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unknown(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "unknown", "reason": "receipt lost"}

    monkeypatch.setattr("agent_run.notifications.send", unknown)
    current = parallel_state()
    for _ in range(3):
        sender = Notifications(tmp_path, current)
        sender.observe(current)
        sender.close()
    # Ordinary start is never replayed; the human boundary has one fixed budget.
    assert sorted(calls.count(identity) for identity in set(calls)) == [1, 3]
    document = read_notifications(tmp_path, "r")
    record, = [item for item in document["records"] if item.get("work_subject") == "ticket:3"]
    assert record["outcome"] == "unknown" and record["exhausted"]
    assert record["exhaustion_reason"] == "max_attempts"
    assert [attempt["outcome"] for attempt in record["attempt_history"]] == ["unknown"] * 3
    assert not document["pending"]


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_run.notification_outbox.RETRY_DELAY_SECONDS", 0)


def test_unknown_then_success_records_each_actual_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "success" if len(calls) != 2 else "unknown", "reason": "receipt lost"}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, parallel_state())
    sender.close()
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert record["outcome"] == "success" and not record["exhausted"]
    assert record["attempts"] == 2
    assert [attempt["outcome"] for attempt in record["attempt_history"]] == ["unknown", "success"]
    assert calls[1] == calls[2]


@pytest.mark.parametrize("change", ["resolved", "generation", "wrong_run", "wrong_repository", "wrong_parent"])
def test_retry_rereads_authoritative_identity_and_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from copy import deepcopy

    current = parallel_state()
    initial = deepcopy(current)
    calls: list[str] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        if rendered["header"]["subtitle"]["content"].endswith("#3"):
            if change == "resolved":
                current["ticket_jobs"]["3"]["phase"] = "developing"
            elif change == "generation":
                current["ticket_jobs"]["3"]["generation"] += 1
                current["ticket_jobs"]["3"]["phase"] = "developing"
            elif change == "wrong_run":
                current["run_id"] = "other"
            elif change == "wrong_repository":
                current["repository"] = "other/repo"
            else:
                current["parent"]["number"] = 99
            return {"outcome": "unknown", "reason": "receipt lost"}
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, initial, load_state=lambda: current)
    sender.close()
    assert len(calls) == len(set(calls)) == 2
    document = read_notifications(tmp_path, "r")
    if change in {"resolved", "generation"}:
        record, = [item for item in document["records"] if item.get("work_subject")]
        assert record["outcome"] == "unknown" and record["cancelled"]
        assert record["cancel_reason"] == "no_longer_current"
    else:
        assert document["unavailable_reason"]


def test_cancelled_attempt_resumes_with_original_deadline_and_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def interrupted(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        if len(calls) == 2:
            cancel.set()
            return {"outcome": "unknown", "reason": "cancelled after submission"}
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", interrupted)
    state = parallel_state()
    sender = Notifications(tmp_path, state)
    sender.close()
    pending, = read_notifications(tmp_path, "r")["pending"]
    assert pending["attempts"] == 1 and pending["attempt_history"][0]["cancelled"]
    restarted = Notifications(tmp_path, state)
    restarted.close()
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert record["attempts"] == 2 and record["outcome"] == "success"
    assert record["deadline_at"] == pending["deadline_at"]
    assert calls[1] == calls[2]


def test_total_deadline_is_not_extended_on_restart_or_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    now = [1000.25]
    monkeypatch.setattr("agent_run.notifications.time", SimpleNamespace(time=lambda: now[0]))
    calls: list[str] = []

    def interrupted(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        if len(calls) == 2:
            cancel.set()
        return {"outcome": "unknown", "reason": "receipt lost"}

    monkeypatch.setattr("agent_run.notifications.send", interrupted)
    state = parallel_state()
    sender = Notifications(tmp_path, state)
    sender.close()
    pending, = read_notifications(tmp_path, "r")["pending"]
    assert pending["deadline_at"] == 1060.25
    now[0] = 1060.26
    for _ in range(2):
        sender = Notifications(tmp_path, state)
        sender.observe(state)
        sender.close()
    assert len(calls) == 2
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert record["exhaustion_reason"] == "deadline" and record["attempts"] == 1


def test_dead_process_claim_spends_an_attempt_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import os
    import subprocess
    import sys

    state = parallel_state()
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps(state))
    script = """import json, os, sys
from pathlib import Path
from agent_run.notifications import Notifications
import agent_run.notifications as module
root = Path(sys.argv[1])
def send(config, card, key, cancel, **kwargs):
    if card['header']['subtitle']['content'].endswith('#3'):
        (root / 'submitted-key').write_text(key)
        os._exit(0)
    return {'outcome': 'success', 'reason': None}
module.send = send
Notifications(root, json.loads((root / 'state.json').read_text())).close()
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=5,
                   env={**os.environ, "PYTHONPATH": source})
    pending, = read_notifications(tmp_path, "r")["pending"]
    assert pending["outcome"] == "sending" and pending["attempts"] == 1
    assert pending["attempt_history"][0]["outcome"] == "sending"
    calls: list[str] = []

    def unknown(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "unknown", "reason": "receipt lost"}

    monkeypatch.setattr("agent_run.notifications.send", unknown)
    sender = Notifications(tmp_path, state)
    sender.close()
    assert calls == [ (tmp_path / "submitted-key").read_text()] * 2
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert record["attempts"] == 3 and record["exhausted"]
    assert record["attempt_history"][0]["outcome"] == "unknown"
    assert "finished_at" not in record["attempt_history"][0]


def test_concurrent_senders_share_unknown_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        if rendered["header"]["subtitle"]["content"].endswith("#3"):
            entered.set()
            assert release.wait(3)
            return {"outcome": "unknown", "reason": "receipt lost"}
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    first = Notifications(tmp_path, parallel_state())
    second = None
    try:
        assert entered.wait(3)
        second = Notifications(tmp_path, parallel_state())
    finally:
        release.set()
        first.close()
        if second:
            second.close()
    assert sorted(calls.count(identity) for identity in set(calls)) == [1, 3]
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert [attempt["number"] for attempt in record["attempt_history"]] == [1, 2, 3]


def test_audit_trimming_does_not_reuse_occurrence_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from copy import deepcopy

    monkeypatch.setattr("agent_run.notification_outbox.MAX_RECORDS", 1)
    calls: list[str] = []

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    state = parallel_state()
    resolved = deepcopy(state)
    resolved["ticket_jobs"]["3"]["phase"] = "developing"
    for _ in range(4):
        sender = Notifications(tmp_path, resolved)
        sender.observe(state)
        sender.close()
    assert len(calls) == len(set(calls)) == 5
    assert len(read_notifications(tmp_path, "r")["records"]) == 1


def test_observation_failure_keeps_claim_until_transport_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    current = parallel_state()
    entered, release = threading.Event(), threading.Event()
    replacement_checked, replacement_sent = threading.Event(), threading.Event()
    broken = False
    keys: list[str] = []

    def load() -> dict[str, Any]:
        if broken:
            raise OSError("temporary state read failure")
        return current

    def replacement_load() -> dict[str, Any]:
        replacement_checked.set()
        return current

    def send(config: Any, rendered: Any, identity: str, cancel: Any, **kwargs: Any) -> dict[str, Any]:
        if not rendered["header"]["subtitle"]["content"].endswith("#3"):
            return {"outcome": "success", "reason": None}
        keys.append(identity)
        if len(keys) == 1:
            entered.set()
            assert cancel.wait(3)
            # Cancellation has been received, but the transport still owns
            # live resources until this controlled cleanup barrier is released.
            assert release.wait(3)
            return {"outcome": "unknown", "reason": "cancelled after submission"}
        replacement_sent.set()
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    first = Notifications(tmp_path, current, load_state=load)
    second = None
    try:
        assert entered.wait(3)
        broken = True
        first.observe(current)
        second = Notifications(tmp_path, current, load_state=replacement_load)
        assert replacement_checked.wait(3)
        pending, = read_notifications(tmp_path, "r")["pending"]
        assert pending["outcome"] == "sending" and pending["attempts"] == 1
        assert "finished_at" not in pending["attempt_history"][0]
        assert not replacement_sent.wait(0.05)
    finally:
        release.set()
        first.close()
        if second:
            second.close()
    assert replacement_sent.is_set() and len(keys) == 2 and keys[0] == keys[1]
    record, = [item for item in read_notifications(tmp_path, "r")["records"] if item.get("work_subject")]
    assert [attempt["outcome"] for attempt in record["attempt_history"]] == ["unknown", "success"]
