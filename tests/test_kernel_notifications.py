"""Native notifications consume committed facts and retain the old outbox budget."""
from __future__ import annotations

from support.repository_fixture import create_kernel

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
import agent_run.runtime.process_identity as process_binding
from agent_run.runtime.process_identity import _process_start_token
from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec, World
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.notifications.sender import NativeNotifications
from support.offline import OfflinePort, Scenario
from support.notification import _kernel, _operations


def test_native_run_start_is_sent_once_from_the_frozen_run_and_query_does_not_send(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    sent: list[tuple[str, str]] = []

    def fake(_config: Any, rendered: dict[str, Any], key: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append((rendered["header"]["title"]["content"], key))
        return {"outcome": "success", "reason": None}

    from agent_run.queries.run import read
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data["run_id"] == "run-native"
    assert sent == [] and not (tmp_path / "notifications").exists()
    NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage).close()
    first = list(sent)
    assert len(first) == 1
    NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage).close()
    assert sent == first
    document = outbox.read_notifications(tmp_path, "run-native")
    assert document["pending"] == []
    assert len(document["records"]) == 1 and document["records"][0]["attempts"] == 1


def test_native_reopens_dead_sender_claim_as_unknown_with_original_budget(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        token = _process_start_token(child.pid)
        assert token is not None
        now = time.time()
        event = {"id": "committed-important-event", "kind": "ticket_completed", "title": "Done",
                 "repository": "example/project", "language": "en", "task_number": 2,
                 "important": True}
        with outbox.transaction(tmp_path, "run-native") as document:
            document["native_run_start"] = True
            outbox.enqueue(document, event, now)
            item = document["pending"][0]
            item.update(owner={"pid": child.pid, "token": token, "sender": "original"},
                        outcome="sending", attempts=1)
            item["attempt_history"].append({"number": 1, "started_at": now, "outcome": "sending"})
            deadline = item["deadline_at"]
    finally:
        child.terminate()
        child.wait(timeout=5)
    calls: list[str] = []

    def fake(_config: Any, _rendered: Any, identity: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "success", "reason": None}

    NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage).close()
    document = outbox.read_notifications(tmp_path, "run-native")
    record = next(item for item in document["records"] if item["id"] == event["id"])
    assert len(calls) == 1 and record["attempts"] == 2
    assert record["deadline_at"] == deadline
    assert record["attempt_history"][0]["outcome"] == "unknown"
    assert document["pending"] == []


def test_native_unknown_ack_is_not_sent_again_after_reopen(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    calls: list[str] = []

    def uncertain(_config: Any, _rendered: Any, identity: str, *_args: Any,
                  **_kwargs: Any) -> dict[str, Any]:
        calls.append(identity)
        return {"outcome": "unknown", "reason": "ACK not observed"}

    NativeNotifications(kernel.storage.path, tmp_path, transport=uncertain, storage=kernel.storage).close()
    NativeNotifications(kernel.storage.path, tmp_path, transport=uncertain, storage=kernel.storage).close()
    record = outbox.read_notifications(tmp_path, "run-native")["records"][0]
    assert len(calls) == 1 and record["outcome"] == "unknown" and record["attempts"] == 1


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_native_pr_creation_requires_consumed_receipt_and_does_not_repeat_for_edits(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", notifications={"enabled": False, "mode": mode})
    original_prepare = OfflinePort.prepare

    def prepare(port: OfflinePort, *args: Any, **kwargs: Any) -> tuple[RunSpec, Delivery]:
        # Only freeze the test's notification inputs; Scenario still runs the
        # existing real Kernel, local Git and simulated remote operations.
        spec, delivery = original_prepare(port, *args, **kwargs)
        spec = freeze_creation(spec, defaults,
                               prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={"enabled": True, "mode": mode,
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
        return replace(spec, inputs=inputs), delivery

    with monkeypatch.context() as patch:
        patch.setattr(OfflinePort, "prepare", prepare)
        scenario = Scenario(tmp_path, DeliveryKind.TICKET, checks=("fail", "none"))
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("pr_write")
    issued = []
    execute = scenario.port.execute

    def recorded_execute(effect, authorize=None):
        if effect.operation == "pr_write":
            issued.append(effect)
        return execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", recorded_execute)
    sent: list[tuple[dict[str, Any], str]] = []

    def transport(_config: Any, rendered: dict[str, Any], key: str,
                  *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append((rendered, key))
        return {"outcome": "success", "reason": None}

    def observe() -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        return outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]

    for _ in range(80):
        step = scenario.step()
        if issued:
            assert issued[0].payload["prior"] is None
            break
        assert step.kind not in {"paused", "waiting", "complete"}
    else:
        pytest.fail("Scenario did not reach its first PR write")
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None  # External write succeeded, but its ACK was lost.
    assert not any(record["kind"] == "pr_created" for record in observe())
    assert step.kind == "waiting" and step.retry_at is not None
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["id"] == issued[0].effect_id and pending["status"] == "sent"
    assert len(issued) == 1
    # Advance to Core's actual bounded readback wake, without polling or renewing
    # its deadline. The same original write must be observed before consumption.
    now[0] = step.retry_at

    step = scenario.step()
    assert step.kind == "progress"
    with scenario.kernel.storage.transaction(write=False) as tx:
        receipt = tx.one("SELECT operation,status,receipt_id FROM effects WHERE id=?", (issued[0].effect_id,))
        assert receipt["operation"] == "pr_write" and receipt["status"] == "observed"
        assert receipt["receipt_id"] is not None
    assert not any(record["kind"] == "pr_created" for record in observe())
    # Reconciliation persists the receipt; only the next real Core advance
    # consumes that original effect and commits the notification's producer fact.
    assert scenario.step().kind == "progress"
    records = observe()
    created = [record for record in records if record["kind"] == "pr_created"]
    assert len(created) == (1 if mode == "detailed" else 0)
    if created:
        from agent_run.notifications.envelope import _identity
        assert created[0]["id"] == _identity(scenario.spec.run_id, "pr_created", pr.number)
        rendered = next(card for card, _key in sent
                        if f"#{pr.number}" in card["header"]["title"]["content"])
        assert f"https://github.com/example/repository/pull/{pr.number}" in str(rendered)

    # CI repair updates the same PR through another confirmed real effect.
    for _ in range(100):
        step = scenario.step()
        if len(issued) == 2:
            assert issued[1].payload["prior"] is not None
            break
        assert step.kind not in {"paused", "waiting", "complete"}
    else:
        pytest.fail("Scenario did not reach the CI repair PR edit")
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref).number == pr.number
    assert len([write for write in scenario.port.writes() if write["operation"] == "pr_write"]) == 2
    assert scenario.step().kind == "progress"  # Consume the edit receipt as well.
    assert [record for record in observe() if record["kind"] == "pr_created"] == created
    sent_before = list(sent)
    observe()
    assert sent == sent_before


def test_native_does_not_adopt_an_unmigrated_existing_journal(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    with outbox.transaction(tmp_path, "run-native") as document:
        document["seen"].append("old-identity")
    before = outbox.read_notifications(tmp_path, "run-native")
    calls: list[str] = []

    def fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append("sent")
        return {"outcome": "success", "reason": None}

    sender = NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage)
    assert sender.failure_reason == "existing notification journal has no native receipt"
    sender.close()
    assert calls == [] and outbox.read_notifications(tmp_path, "run-native") == before


def test_native_first_worker_event_uses_committed_start_once(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    execution = kernel.claim_executor("owner")
    call = _operations(kernel, execution).once().call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, "fixture-thread")
    process = {"pid": 21, "pgid": 21, "start_token": "fixture", "host_pid": 10,
               "host_start_token": "fixture-host"}
    kernel.agent_started(call, process)
    sent: list[str] = []

    def fake(_config: Any, rendered: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered["header"]["title"]["content"])
        return {"outcome": "success", "reason": None}

    NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage).close()
    records = outbox.read_notifications(tmp_path, "run-native")["records"]
    assert {record["kind"] for record in records} == {"run_start", "ticket_started"}
    from agent_run.notifications.envelope import _identity
    assert next(record for record in records if record["kind"] == "ticket_started")["id"] == (
        _identity("run-native", "ticket_started", "ticket:2"))
    assert len(sent) == 2
    NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage).close()
    assert len(sent) == 2


def test_native_observer_consumes_multiple_committed_history_pages(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    for number in range(9):
        assert kernel.control("stop", f"stop-{number}", subject="native-ticket") == "applied"
        assert kernel.control("resume", f"resume-{number}", subject="native-ticket") == "applied"
    with kernel.storage.transaction(write=False) as tx:
        end = tx.one("SELECT max(position) FROM history")[0]
    assert end > 16

    sent: list[str] = []

    def fake(_config: Any, rendered: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered["header"]["title"]["content"])
        return {"outcome": "success", "reason": None}

    sender = NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage)
    sender.close()
    assert sender.failure_reason is None
    document = outbox.read_notifications(tmp_path, "run-native")
    assert document["native_cursor"] == end
    assert len(sent) == 1 and len(document["records"]) == 1


def test_parent_requirement_refresh_keeps_notification_delivery_available(tmp_path: Path) -> None:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", notifications={"enabled": False, "mode": "detailed"})
    source = Requirements(ParentIssue(1, "Original Parent", "Original scope"), ())
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "unread", Commit("base", "tree"),
                        "refs/heads/source", "refs/heads/target")
    spec = freeze_creation(RunSpec("run-native", "example/project", 1, "unread", "graph-v1",
                                   (delivery,), repository_root=str(tmp_path)), defaults,
                           prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
    assert spec.inputs is not None
    inputs = replace(spec.inputs, notifications={"enabled": True, "mode": "detailed",
                                                 "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    kernel = create_kernel(tmp_path / "kernel.sqlite3", replace(spec, inputs=inputs), requirements=source)
    delivered = Event()

    def fake(_config: Any, _rendered: Any, _identity: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        delivered.set()
        return {"outcome": "success", "reason": None}

    sender = NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage)
    try:
        assert delivered.wait(3)
        changed = replace(source, parent=replace(source.parent, title="Revised Parent"))
        kernel.refresh(lambda: changed)
        sender.observe_committed()
        with kernel.storage.transaction(write=False) as tx:
            end = tx.one("SELECT max(position) FROM history")[0]
        document = outbox.read_notifications(tmp_path, "run-native")
        assert sender.failure_reason is None
        assert document["native_cursor"] == end
    finally:
        sender.close(drain=False)


def test_invalid_owner_pid_cannot_be_probed_as_a_process_group(monkeypatch) -> None:
    def forbidden(_pid: int, _signal: int) -> None:
        raise AssertionError("invalid owner PID reached kill(0)")

    monkeypatch.setattr(process_binding.os, "kill", forbidden)
    for pid in (-1, 0, 1):
        assert process_binding._process_start_token(pid) is None
        assert process_binding._process_binding_status(pid, None) == "unknown"


def test_fatal_close_cancels_real_local_send_child_without_drain(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    started = Event()
    children: list[subprocess.Popen[bytes]] = []

    def fake(_config: Any, _rendered: Any, _identity: str, cancel: Event,
             **_kwargs: Any) -> dict[str, Any]:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        started.set()
        try:
            assert cancel.wait(5)
            return {"outcome": "unknown", "reason": "cancelled before ACK"}
        finally:
            child.terminate()
            child.wait(timeout=5)

    sender = NativeNotifications(kernel.storage.path, tmp_path, transport=fake, storage=kernel.storage)
    try:
        assert started.wait(3)
        before = time.monotonic()
        sender.close(drain=False)
        assert time.monotonic() - before < 3
        assert all(child.poll() is not None for child in children)
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == []
        assert document["records"][0]["outcome"] == "unknown"
    finally:
        sender.close(drain=False)
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_native_gate_leave_reentry_between_observations_is_a_new_occurrence(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    gate_colors: list[str] = []

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if rendered["header"]["title"]["content"] == "Task paused":
            gate_colors.append(rendered["header"]["template"])
        return {"outcome": "success", "reason": None}

    def boundaries() -> list[dict[str, Any]]:
        observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        return [row for row in outbox.read_notifications(tmp_path, "run-native")["records"]
                if row["kind"] == "boundary"]

    kernel.control("stop", "first-stop", subject="native-ticket")
    first = boundaries()
    assert len(first) == 1 and first[0]["outcome"] == "success"
    # Both transitions happen while there is no observer. Comparing only the
    # latest gate snapshot would mistake this for the first occurrence.
    kernel.control("resume", "between-observations", subject="native-ticket")
    kernel.control("stop", "second-stop", subject="native-ticket")
    second = boundaries()
    assert len(second) == 2
    assert second[0]["id"] != second[1]["id"]
    assert second[0]["slot_id"] == second[1]["slot_id"]
    assert [row["attempts"] for row in second] == [1, 1]
    assert boundaries() == second
    assert gate_colors == ["yellow", "yellow"]


def test_leaving_a_gate_cancels_its_unsent_card_without_claiming_delivery(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    started, release = Event(), Event()
    delivered: list[str] = []

    def transport(_config: Any, _card: Any, identity: str, cancel: Event,
                  **_kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(5)
        delivered.append(identity)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert started.wait(3)
        kernel.control("stop", "first-stop", subject="native-ticket")
        observer.observe_committed()
        pending = outbox.read_notifications(tmp_path, "run-native")["pending"]
        gate = [item for item in pending if item["event"]["kind"] == "boundary"]
        assert len(gate) == 1 and gate[0]["attempts"] == 0
        deadline = gate[0]["deadline_at"]
        kernel.control("resume", "leave-before-send", subject="native-ticket")
        observer.observe_committed()
        document = outbox.read_notifications(tmp_path, "run-native")
        cancelled = [item for item in document["records"] if item["kind"] == "boundary"]
        assert len(cancelled) == 1
        assert cancelled[0]["outcome"] == "cancelled" and cancelled[0]["attempts"] == 0
        assert cancelled[0]["deadline_at"] == deadline
        assert cancelled[0]["id"] not in delivered
    finally:
        release.set()
        observer.close()


def test_gate_batch_wakes_a_parked_sender_without_close(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, count=16)
    started, boundary = Event(), Event()

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if rendered["header"]["title"]["content"] == "Task paused":
            boundary.set()
        started.set()
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert started.wait(3)
        deadline = time.monotonic() + 3
        while outbox.read_notifications(tmp_path, "run-native")["pending"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        for index in range(16):
            kernel.control("stop", f"batch-{index}", subject=f"native-ticket-{index}")
        observer.observe_committed()
        assert boundary.wait(3), "durable current cards did not wake the idle sender"
        assert observer.failure_reason is None
    finally:
        observer.close(drain=False)


def test_accumulated_gate_history_continues_within_each_read_budget(tmp_path: Path, monkeypatch) -> None:
    import agent_run.storage.sqlite as storage

    kernel = _kernel(tmp_path)
    for index in range(200):
        kernel.control("stop", f"old-stop-{index}", subject="native-ticket")
        kernel.control("resume", f"old-resume-{index}", subject="native-ticket")
    # A constrained but valid read budget exposes cumulative unpaged reads
    # without creating a huge Run or weakening production capacity limits.
    monkeypatch.setattr(storage, "QUERY_LIMIT", 32 * 1024)
    sent: list[str] = []

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        sent.append(rendered["header"]["title"]["content"])
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    observer.close()
    assert observer.failure_reason is None
    document = outbox.read_notifications(tmp_path, "run-native")
    assert document["native_boundary_cursor"] == document["native_cursor"]
    assert len(sent) == 1  # Historical, already-cleared gates are not current cards.


def test_gate_reentry_during_paged_catchup_never_claims_the_old_card(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    started, release = Event(), Event()
    delivered: list[str] = []

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(15)
        delivered.append(rendered["header"]["title"]["content"])
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert started.wait(3)
        kernel.control("stop", "original-stop", subject="native-ticket")
        observer.observe_committed()
        old = next(item["event"]["id"] for item in outbox.read_notifications(tmp_path, "run-native")["pending"]
                   if item["event"]["kind"] == "boundary")
        # Keep the same gate for more than two replay pages before leaving it.
        # The sender must catch up before claiming any old current boundary.
        for index in range(300):
            kernel.control("stop", f"still-stopped-{index}", subject="native-ticket")
        kernel.control("resume", "leave-old", subject="native-ticket")
        kernel.control("stop", "new-occurrence", subject="native-ticket")
        observer.observe_committed()
        release.set()
        observer.close()
        assert observer.failure_reason is None
        records = [row for row in outbox.read_notifications(tmp_path, "run-native")["records"]
                   if row["kind"] == "boundary"]
        previous = next(row for row in records if row["id"] == old)
        assert previous["outcome"] == "cancelled" and previous["attempts"] == 0
        assert len([row for row in records if row["outcome"] == "success"]) == 1
        assert delivered.count("Task paused") == 1
    finally:
        release.set()
        observer.close(drain=False)


def test_gate_retry_rereads_committed_subject_without_a_callback(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    kernel.control("stop", "original-gate", subject="native-ticket")
    attempted = Event()
    gate_calls: list[str] = []

    def transport(_config: Any, rendered: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if rendered["header"]["title"]["content"] == "Task paused":
            gate_calls.append("attempt")
            # The business commit survives even if its observer callback did
            # not run. A delivery retry cannot use an old gate as authority.
            kernel.control("resume", "resume-without-observer", subject="native-ticket")
            attempted.set()
            return {"outcome": "failed", "reason": "definitive rejected send"}
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert attempted.wait(3)
        observer.close()
        assert observer.failure_reason is None
        records = [row for row in outbox.read_notifications(tmp_path, "run-native")["records"]
                   if row["kind"] == "boundary"]
        assert len(records) == 1 and records[0]["cancelled"] is True
        assert records[0]["attempts"] == 1 and records[0]["outcome"] == "failed"
        assert gate_calls == ["attempt"]
    finally:
        observer.close(drain=False)
