"""The existing outbox consumes the same canonical before-Final timeout as queries."""
import json
import time

import agent_run.storage.notification_outbox as outbox
from agent_run.notifications.envelope import event_copy
from agent_run.model import DeliveryKind
from agent_run.notifications.sender import NativeNotifications
from agent_run.core.operation_contract import ReadUnavailable
from support.core_operations import CoreOperations
from support.notification import _enabled_scenario
from support.environment import durable_snapshot as _snapshot


def test_before_final_timeout_uses_one_original_global_notification_and_resume(tmp_path, monkeypatch):
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.TICKET)
    assert scenario.run().kind == "complete"
    assert scenario.kernel.source_status()["final_subject"] is None
    assert len(scenario.kernel.source_status()["completions"]) == 1
    started = time.time()
    scenario.kernel.clock = lambda: started
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    cause = "Original Run ref unavailable"
    def failed(parent):
        raise ReadUnavailable("final_source_missing", cause)
    monkeypatch.setattr(operations.runner.ports, "source", failed)
    assert operations.once().retry_at == started + 5
    scenario.kernel.clock = lambda: started + 600
    assert operations.once().reason == "supervision_timeout"
    assert scenario.kernel.source_status()["final_subject"] is None
    sent = []

    def transport(_config, rendered, _key, *_args, **_kwargs):
        sending = [item for item in outbox.read_notifications(tmp_path, scenario.spec.run_id)["pending"]
                   if item["outcome"] == "sending"]
        assert len(sending) == 1
        sent.append((dict(sending[0]["event"]), rendered))
        return {"outcome": "success", "reason": None}

    def observe():
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
        observer.close()
        assert observer.failure_reason is None
        return outbox.read_notifications(tmp_path, scenario.spec.run_id)

    before = {path: body for path, body in _snapshot(tmp_path).items() if "kernel.sqlite3" in path}
    journal = observe()
    assert {path: body for path, body in _snapshot(tmp_path).items() if "kernel.sqlite3" in path} == before
    boundary, = [(event, rendered) for event, rendered in sent if event["kind"] == "boundary"]
    event, rendered = boundary
    command = "agent-run resume --repo example/project -- offline-run"
    assert event["work_subject"] is None and event["scope"] == "global"
    assert event["title"] == event_copy({"language": "en"}, "timeout")
    assert event["next_step"] == command
    assert event["summary"] == "supervision_timeout; final_source_missing: " + cause
    assert rendered["header"]["template"] == "red"
    assert command in json.dumps(rendered) and cause in json.dumps(rendered)
    record, = [row for row in journal["records"] if row["kind"] == "boundary"]
    assert record["id"] == event["id"] and record["attempts"] == 1 and record["outcome"] == "success"
    assert journal["pending"] == []
    first_sent = list(sent)
    assert observe()["records"] == journal["records"]
    assert sent == first_sent
