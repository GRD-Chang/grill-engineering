"""Exit hooks share the native sender, original evidence and delivery budget."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
import agent_run.adapters.notification_transport as notification_transport
from agent_run.runtime.process_identity import _process_start_token
from agent_run.runtime.package_lease import runner_usage_lease
from agent_run.runtime.execution import _cgroup
from agent_run.queries.host_exit import host_exit_notice
from agent_run.model import HostObservation
from agent_run.runtime.handoff import prepare_handoff
from agent_run.notifications.sender import NativeNotifications
from support.notification import _kernel


@contextmanager
def _killed_host(tmp_path: Path):
    kernel = _kernel(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    lock = tmp_path / "runner.lock"
    command = (sys.executable, "-I", "-c", "import signal; print('ready',flush=True); signal.pause()")
    request = kernel.admit_host("original", kind="run", command=command,
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    handoff = None
    try:
        assert child.stdout is not None and select.select([child.stdout], [], [], 3)[0]
        assert child.stdout.readline() == b"ready\n"
        token = _process_start_token(child.pid)
        assert token
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {"PATH": os.defpath}, runner_lock=lock,
                                       terminal_lease=terminal, ttl_seconds=0.5)
            assert kernel.dispatch_host(request)
            encoded = json.dumps(handoff.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=encoded,
                                                        usage_pending=True))
            kernel.observe_host(request, HostObservation(
                "running", request.unit_name, invocation_id="1" * 32, cgroup=_cgroup(child.pid),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(), main_pid=child.pid,
                start_token=token, evidence=encoded, usage_ready=True))
            child.kill()
            assert child.wait(timeout=3) == -9
            notice = host_exit_notice(kernel.storage.path, 'original', handoff.receipt, service_result='signal', exit_code='killed', exit_status='KILL', invocation_id='1' * 32, storage=kernel.storage)
            assert notice is not None
            yield kernel, notice
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate()
            handoff.process.wait(timeout=3)


def test_exit_sender_keeps_first_evidence_budget_and_does_not_observe_business_history(tmp_path: Path):
    with _killed_host(tmp_path) as (kernel, notice):
        calls: list[str] = []
        def transport(_config: Any, _card: Any, identity: str, *_args: Any, **kwargs: Any):
            assert kwargs["is_current"]()
            calls.append(identity)
            return {"outcome": "unknown" if len(calls) == 1 else "success", "reason": None}
        before = kernel.storage.path.read_bytes()
        first = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, transport=transport, storage=kernel.storage)
        first.close()
        assert first.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert len(document["records"]) == 1 and document["pending"] == []
        record = document["records"][0]
        assert record["kind"] == "executor_exit" and record["attempts"] == 2
        assert record["exit_evidence"] == notice and document["executor_exit"] == notice
        assert record["attempt_history"][0]["outcome"] == "unknown"
        assert len(calls) == 2 and len(set(calls)) == 1
        assert "native_cursor" not in document and not document.get("native_run_start")
        second = NativeNotifications(kernel.storage.path, tmp_path, exit_notice={**notice, 'observed_at': 'later observation'}, transport=transport, storage=kernel.storage)
        second.close()
        assert second.failure_reason is None
        assert outbox.read_notifications(tmp_path, "run-native")["records"] == [record]
        assert outbox.read_notifications(tmp_path, "run-native")["executor_exit"] == notice
        assert len(calls) == 2 and kernel.storage.path.read_bytes() == before


def test_exit_sender_rechecks_after_transport_identity_before_physical_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with _killed_host(tmp_path) as (kernel, notice):
        commands: list[list[str]] = []
        def run(argv, _deadline, _cancel):
            commands.append(argv)
            assert "whoami" in argv
            kernel.stop_task("stop-during-identity-preflight")
            return notification_transport._CommandResult(0, json.dumps({
                "appId": "app_fixture", "profile": "fixture", "identity": "bot", "available": True,
            }).encode())
        monkeypatch.setattr(notification_transport, "_run", run)
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, storage=kernel.storage)
        observer.close()
        assert observer.failure_reason is None and len(commands) == 1
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert record["outcome"] == "failed" and record["attempts"] == 1 and record["cancelled"]
        assert record["attempt_history"][0]["outcome"] == "failed"
        NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, storage=kernel.storage).close()
        assert outbox.read_notifications(tmp_path, "run-native")["records"] == [record]
        assert len(commands) == 1


def test_full_exit_journal_records_first_unsent_failure_without_renewing_budget(tmp_path: Path):
    with _killed_host(tmp_path) as (kernel, notice):
        import time
        with outbox.transaction(tmp_path, "run-native") as document:
            document["native_binding"] = ["run-native", "example/project", 1]
            for number in range(outbox.MAX_PENDING):
                outbox.enqueue(document, {"id": f"existing-{number}", "kind": "stage_start"}, time.time())
        def forbidden(*_args, **_kwargs):
            raise AssertionError("capacity failure must not start a physical transport")
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, transport=forbidden, storage=kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert len(document["pending"]) == outbox.MAX_PENDING
        assert len(document["records"]) == 1
        original = document["records"][0]
        assert original["kind"] == "executor_exit" and original["exit_evidence"] == notice
        assert original["outcome"] == "failed" and original["attempts"] == 0
        assert original["exhaustion_reason"] == "pending_capacity"
        NativeNotifications(kernel.storage.path, tmp_path, exit_notice={**notice, 'observed_at': 'later'}, transport=forbidden, storage=kernel.storage).close()
        assert outbox.read_notifications(tmp_path, "run-native")["records"] == [original]


def test_exit_invalidated_after_claim_is_recorded_unsent_not_unknown(tmp_path: Path, monkeypatch):
    import agent_run.queries.host_exit as facts
    with _killed_host(tmp_path) as (kernel, notice):
        original_current = facts.host_exit_current
        stopped = False
        def current(database, evidence, *, storage):
            nonlocal stopped
            document = outbox.read_notifications(tmp_path, "run-native")
            if not stopped and any(item["outcome"] == "sending" for item in document.get("pending", [])):
                kernel.stop_task("stop-after-journal-claim")
                stopped = True
            return original_current(database, evidence, storage=storage)
        monkeypatch.setattr(facts, "host_exit_current", current)
        def forbidden(*_args, **_kwargs):
            raise AssertionError("the known obsolete claim must never enter transport")
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, transport=forbidden, storage=kernel.storage)
        observer.close()
        assert stopped and observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert record["outcome"] == "failed" and record["attempt_history"][0]["not_sent"] and record["attempts"] == 1
        assert record["attempt_history"][0]["outcome"] == "failed"


def test_temporary_unsent_claim_does_not_label_later_success_unsent(tmp_path: Path, monkeypatch):
    import agent_run.queries.host_exit as facts
    from threading import local
    with _killed_host(tmp_path) as (kernel, notice):
        original_current = facts.host_exit_current
        original_send = NativeNotifications._send_one
        sending = local()
        preflight_attempts = []
        interrupted = False

        def send(observer, event, deadline_at, cancel):
            # Owner-side validity scans can also see a sending journal entry.
            # Inject only the actual call's preflight, not a scheduling scan.
            sending.active = True
            preflight_attempts.append(event['id'])
            try:
                return original_send(observer, event, deadline_at, cancel)
            finally:
                sending.active = False

        def current(database, evidence, *, storage):
            nonlocal interrupted
            if not interrupted and getattr(sending, 'active', False):
                document = outbox.read_notifications(tmp_path, "run-native")
                assert any(item['event']['id'] == preflight_attempts[-1]
                           and item['outcome'] == 'sending' and item['attempts'] == 1
                           for item in document['pending'])
                interrupted = True
                return False  # This one physical preflight cannot verify its original Host.
            return original_current(database, evidence, storage=storage)
        monkeypatch.setattr(facts, "host_exit_current", current)
        monkeypatch.setattr(NativeNotifications, '_send_one', send)
        calls = []
        def transport(_config, _card, identity, *_args, **kwargs):
            assert kwargs["is_current"]()
            calls.append(identity)
            return {"outcome": "success", "reason": None}
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, transport=transport, storage=kernel.storage)
        observer.close()
        assert interrupted and observer.failure_reason is None and len(calls) == 1
        document = outbox.read_notifications(tmp_path, "run-native")
        assert document["pending"] == [] and len(document["records"]) == 1
        record = document["records"][0]
        assert preflight_attempts == [record['id'], record['id']]
        assert calls == [hashlib.sha256(record['id'].encode()).hexdigest()[:32]]
        assert record['kind'] == 'executor_exit' and record['exit_evidence'] == notice
        assert record["outcome"] == "success" and record["attempts"] == 2
        assert "not_sent" not in record
        assert record["attempt_history"][0]["not_sent"]
        assert record["attempt_history"][0]["outcome"] == "failed"
        assert record["attempt_history"][1]["outcome"] == "success"
        assert "not_sent" not in record["attempt_history"][1]


def test_unsent_attempt_then_success_archives_flat_identity_without_renewing_budget(tmp_path, monkeypatch):
    """Real journal/result accounting only; no Host, sender thread or transport proof."""
    observer = object.__new__(NativeNotifications)
    observer.state_root, observer.run_id, observer.owner = tmp_path, 'journal-run', {'fixture': 'claim-owner'}
    now = [100.0]
    monkeypatch.setattr('agent_run.notifications.sender.time.time', lambda: now[0])
    evidence = {'fixture': 'original-exit-payload'}
    event = {'id': 'original-exit-event', 'kind': 'executor_exit', 'exit_evidence': evidence}
    with outbox.transaction(tmp_path, observer.run_id) as document:
        outbox.enqueue(document, event, now[0])
    for number, result in enumerate(({'outcome': 'failed', 'not_sent': True}, {'outcome': 'success'}), 1):
        with outbox.transaction(tmp_path, observer.run_id) as document:
            item, = document['pending']
            assert item['event'] == event and item['deadline_at'] == 160.0
            item.update(owner=observer.owner, outcome='sending', attempts=number)
            item['attempt_history'].append({'number': number, 'started_at': now[0], 'outcome': 'sending'})
        before = outbox.read_notifications(tmp_path, observer.run_id)
        observer._finish_result((event['id'], number + 1), {'outcome': 'success'}, now[0])
        assert outbox.read_notifications(tmp_path, observer.run_id) == before
        observer._finish_result((event['id'], number), result, now[0])
        if number == 1:
            item, = outbox.read_notifications(tmp_path, observer.run_id)['pending']
            assert item['outcome'] == 'failed' and 'not_sent' not in item
            assert item['attempt_history'][0]['not_sent'] is True
            now[0] = item['next_attempt_at']
    document = outbox.read_notifications(tmp_path, observer.run_id)
    assert document['pending'] == []
    record, = document['records']
    assert record['id'] == event['id'] and 'event' not in record and 'owner' not in record
    assert record['kind'] == event['kind'] and record['exit_evidence'] == evidence
    assert record['deadline_at'] == 160.0 and record['outcome'] == 'success' and record['attempts'] == 2
    assert 'not_sent' not in record
    first, second = record['attempt_history']
    assert first['number'] == 1 and first['outcome'] == 'failed' and first['not_sent'] is True
    assert second['number'] == 2 and second['outcome'] == 'success' and 'not_sent' not in second


def test_exit_identity_read_fault_before_submit_keeps_original_unsent_attempt(tmp_path, monkeypatch):
    import agent_run.queries.host_exit as facts
    with _killed_host(tmp_path) as (kernel, notice):
        original_current = facts.host_exit_current
        identity_ready = False
        interrupted = False
        commands = []
        def current(database, evidence, *, storage):
            nonlocal interrupted
            if identity_ready and not interrupted:
                interrupted = True
                raise facts.HostExitUnavailable("temporary original Host read failure")
            return original_current(database, evidence, storage=storage)
        def run(argv, _deadline, _cancel):
            nonlocal identity_ready
            commands.append(argv)
            if "whoami" in argv:
                identity_ready = True
                return notification_transport._CommandResult(0, json.dumps({
                    "appId": "app_fixture", "profile": "fixture", "identity": "bot", "available": True,
                }).encode())
            assert interrupted and "+messages-send" in argv
            return notification_transport._CommandResult(0, b'{"data":{"message_id":"local-only-proof"}}')
        monkeypatch.setattr(facts, "host_exit_current", current)
        monkeypatch.setattr(notification_transport, "_run", run)
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, storage=kernel.storage)
        observer.close()
        assert observer.failure_reason is None and interrupted
        assert sum("whoami" in command for command in commands) == 2
        assert sum("+messages-send" in command for command in commands) == 1
        record, = outbox.read_notifications(tmp_path, "run-native")["records"]
        assert record["outcome"] == "success" and record["attempts"] == 2 and "not_sent" not in record
        assert record["attempt_history"][0]["outcome"] == "failed" and record["attempt_history"][0]["not_sent"]
        assert record["attempt_history"][1]["outcome"] == "success"


def test_late_exit_observation_does_not_downgrade_latest_exit_receipt(tmp_path, monkeypatch):
    with _killed_host(tmp_path) as (kernel, notice):
        later = {**notice, "epoch": notice["epoch"] + 1, "action_id": "newer-host-exit"}
        with outbox.transaction(tmp_path, "run-native") as document:
            document["native_binding"] = ["run-native", "example/project", 1]
            document["executor_exit"] = later
        # Isolate atomic receipt ordering from delivery/Host authorization.
        # A real sender must independently revalidate before claiming the old
        # event; this probe neither creates nor authorizes a successor Host.
        monkeypatch.setattr(NativeNotifications, "_work", lambda _self: None)
        observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice, storage=kernel.storage)
        # Projection-only receipt ordering, with the physical worker stopped.
        assert observer._observe_committed()
        observer.close(drain=False)
        assert observer.failure_reason is None
        assert outbox.read_notifications(tmp_path, "run-native")["executor_exit"] == later


def test_controlled_exit_first_postclaim_currentness_fault_is_paused_and_known_unsent(tmp_path, monkeypatch):
    """Prevalidated Host facts isolate sender ownership, not native Host proof."""
    from threading import Event
    from agent_run.queries.host_exit import HostExitUnavailable
    from agent_run.notifications.envelope import event_envelope
    from agent_run.notifications.event_projection import event_state

    kernel = _kernel(tmp_path)
    notice = {"run_id": "run-native", "epoch": 1, "action_id": "original-exit"}
    with kernel.storage.transaction(write=False) as tx:
        event = event_envelope(event_state(tx), "executor_exit", "original-exit", "Original Host exit",
                               important=True, exit_evidence=notice)
    monkeypatch.setattr('agent_run.notifications.event_projection.host_exit_event',
                        lambda *_args, **_kwargs: event)
    faulted, persisted = Event(), Event()
    calls = []
    original_finish = NativeNotifications._finish_result

    def current(self, event):
        pending = outbox.read_notifications(tmp_path, 'run-native').get('pending', [])
        if not faulted.is_set() and any(item['outcome'] == 'sending' for item in pending):
            faulted.set()
            raise HostExitUnavailable('original postclaim Host read unavailable')
        return True

    def finish(self, event_id, result, returned_at):
        original_finish(self, event_id, result, returned_at)
        persisted.set()

    def transport(*_args, **_kwargs):
        calls.append('physical request')
        return {'outcome': 'success', 'reason': None}

    monkeypatch.setattr(NativeNotifications, '_exit_current', current)
    monkeypatch.setattr(NativeNotifications, '_finish_result', finish)
    observer = NativeNotifications(kernel.storage.path, tmp_path, exit_notice=notice,
                                   transport=transport, storage=kernel.storage)
    try:
        assert persisted.wait(3)
        assert observer.failure_reason == 'original postclaim Host read unavailable'
        assert calls == []
        before, = outbox.read_notifications(tmp_path, 'run-native')['pending']
        assert before['attempts'] == 1 and before['outcome'] == 'failed'
        assert before['attempt_history'][0]['not_sent'] is True
        observer.observe_committed()
        observer.close(timeout_seconds=3)
        assert observer.closed and observer.failure_reason is None
        record, = outbox.read_notifications(tmp_path, 'run-native')['records']
        assert record['attempts'] == 2 and record['outcome'] == 'success'
        assert record['deadline_at'] == before['deadline_at']
        assert record['attempt_history'][0]['not_sent'] is True
        assert 'not_sent' not in record['attempt_history'][1]
        assert calls == ['physical request']
    finally:
        observer.close(drain=False)
        assert observer.closed
