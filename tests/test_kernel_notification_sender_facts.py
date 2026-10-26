"""The one sender consumes original Resume, close and delayed-progress facts."""
from dataclasses import replace
from pathlib import Path
from threading import Event
import time
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
from agent_run.notifications.envelope import event_copy, event_envelope
from support.core_operations import CoreOperations, scripted_return
from agent_run.model import DeliveryKind, Role
from agent_run.core.kernel import Kernel
from agent_run.model import wire_json
from agent_run.notifications.sender import NativeNotifications
from support.notification import recorded_agents as _recorded_agents
from test_kernel_notification_lifecycle import _enabled_scenario
from test_kernel_notifications import _kernel, _operations


def _state(scenario) -> dict[str, Any]:
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT id,repository,parent_issue,language FROM runs")
        return {"run_id": row["id"], "repository": row["repository"], "language": row["language"],
                "parent": {"number": row["parent_issue"]}}


def _active(root: Path, run: str) -> dict[str, Any]:
    sending = [item for item in outbox.read_notifications(root, run)["pending"] if item["outcome"] == "sending"]
    assert len(sending) == 1
    return dict(sending[0]["event"])


def _observe(scenario, root, captured):
    def transport(*_args, **_kwargs):
        event = _active(root, scenario.spec.run_id)
        event.update(_rendered_title=_args[1]["header"]["title"]["content"],
                     _rendered_color=_args[1]["header"]["template"])
        captured.append(event)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, root, transport=transport)
    observer.close()
    assert observer.failure_reason is None


@pytest.mark.parametrize("outcome", ["failed", "human_action"])
def test_sender_merges_actual_resume_result_into_the_original_gate_without_a_second_card(tmp_path, monkeypatch, outcome):
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY)
    blocked = wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Exact original decision"]})
    captured = []
    resumed = [False]

    def started(_call):
        if resumed[0]:
            _observe(scenario, tmp_path, captured)

    _recorded_agents(scenario, monkeypatch, lambda call: blocked, started)
    assert scenario.run().kind == "paused"
    assert scenario.kernel.control("resume", "resume-original-gate", subject=scenario.delivery.key,
                                   message="Continue the original target") == "applied"
    resumed[0] = True
    continued = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).once()
    assert continued.call is not None
    if outcome == "failed":
        assert scenario.kernel.dispatch_agent(continued.call)
        scenario.kernel.agent_unavailable(continued.call, "worker_preparation_failed", {"message": "Original launch failed"})
    else:
        scripted_return(scenario.kernel, scenario.runner(), continued.call, scenario.agent)
        actual_start, = [event for event in captured if event.get("resume_id") == "resume-original-gate"]
        assert actual_start["resume_outcome"] == "started"
        assert actual_start["color"] == actual_start["_rendered_color"] == "blue"
        assert scenario.step().kind == "paused"
    with scenario.kernel.storage.transaction(write=False) as tx:
        original = tx.one("SELECT position,material_id,generation,identity FROM history WHERE subject=? AND kind='paused' ORDER BY position DESC LIMIT 1",
                          (scenario.delivery.key,))
        position, material, generation = original["position"], original["material_id"], original["generation"]
    _observe(scenario, tmp_path, captured)
    gates = [event for event in captured if event["kind"] == "boundary"]
    assert len(gates) == 1
    gate = gates[0]
    assert gate["resume_id"] == "resume-original-gate" and gate["resume_outcome"] == outcome
    assert gate["source_position"] == position and gate["source_material_id"] == material
    assert gate["generation"] == generation and gate["work_subject"] == scenario.delivery.key
    assert gate["current"] is True and gate["next_step"]
    assert gate["title"] == gate["_rendered_title"] == event_copy(_state(scenario), "resume_" + outcome)
    assert gate["color"] == gate["_rendered_color"] == ("red" if outcome == "failed" else "yellow")
    assert not any(event["kind"] == "resume" and event.get("resume_outcome") in {"failed", "human_action"} for event in captured)
    first = outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"]
    _observe(scenario, tmp_path, captured)
    assert outbox.read_notifications(tmp_path, scenario.spec.run_id)["records"] == first


@pytest.mark.parametrize("publication_recorded", [True, False])
def test_sender_terminal_uses_original_close_whole_effort_and_keeps_unknown_publication_unknown(tmp_path, monkeypatch, publication_recorded):
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY)
    now = [time.time()]
    scenario.kernel.clock = lambda: now[0]

    def started(call):
        seconds = {Role.DEVELOPMENT: 2, Role.REVIEW: 3, Role.PUBLICATION: 5}[call.role]
        if publication_recorded or call.role != Role.PUBLICATION:
            scenario.kernel.observe_worker_activity(call, "start", active=True, observed_at=now[0] + 1, evidence={"type": "turn.started"})
            scenario.kernel.observe_worker_activity(call, "end", active=False, observed_at=now[0] + 1 + seconds, evidence={"type": "turn.completed"})
        now[0] += seconds + 2

    _recorded_agents(scenario, monkeypatch, started=started)
    assert scenario.run(approve=True).kind == "complete"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM history WHERE kind='worker_started'")[0] == 3
        close = tx.one("SELECT e.id,e.receipt_id FROM effects e JOIN history h ON h.identity=e.id AND h.material_id=e.receipt_id "
                       "WHERE e.operation='close' AND e.status='confirmed' AND h.kind='effect_consumed'")
        close_id, receipt_id = close["id"], close["receipt_id"]
    captured = []
    _observe(scenario, tmp_path, captured)
    terminal, = [event for event in captured if event.get("terminal") and event["status_code"] == "completed"]
    assert terminal["id"] == event_envelope(_state(scenario), "boundary", ["completed", close_id, receipt_id], "")["id"]
    assert terminal["execution_timing_complete"] is publication_recorded
    assert terminal["total_seconds"] == (10 if publication_recorded else None)
    assert terminal["recorded_execution_seconds"] == (10 if publication_recorded else 5)
    assert terminal["effort"]["development"]["execution_seconds"] == 2
    assert terminal["effort"]["review"]["execution_seconds"] == 3
    assert terminal["effort"]["development"]["rounds"] == terminal["effort"]["review"]["rounds"] == 1
    first = list(captured)
    _observe(scenario, tmp_path, captured)
    assert captured == first


def test_sender_compresses_real_development_to_review_and_preserves_gate_budget_and_return_order(tmp_path, monkeypatch):
    original_create = Kernel.create

    def detailed(_cls, path, spec, **kwargs):
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={**spec.inputs.notifications, "mode": "detailed"})
        return original_create(path, replace(spec, inputs=inputs), **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Kernel, "create", classmethod(detailed))
        scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY)
    entered, release = Event(), Event()
    sent = []

    def transport(*_args, **_kwargs):
        event = _active(tmp_path, scenario.spec.run_id)
        if not sent:
            sent.append(event)
            entered.set()
            assert release.wait(15)
        else:
            document = outbox.read_notifications(tmp_path, scenario.spec.run_id)
            assert any(record["id"] == sent[0]["id"] and record["outcome"] == "success" for record in document["records"])
            sent.append(event)
        return {"outcome": "success", "reason": None}

    observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
    observed = []

    def started(call):
        observer.observe_committed()
        document = outbox.read_notifications(tmp_path, scenario.spec.run_id)
        if call.role == Role.DEVELOPMENT:
            item, = [item for item in document["pending"] if item["event"]["kind"] == "parent_started"]
            observed.append((item["event"]["id"], item["deadline_at"]))
            with outbox.transaction(tmp_path, scenario.spec.run_id) as journal:
                outbox.enqueue(journal, event_envelope(_state(scenario), "stage_start", "unknown-origin-0", "Unknown recorded origin"), time.time())
                journal["unavailable_reason"] = "Original local delivery interruption"
        elif call.role == Role.REVIEW:
            old_id, deadline = observed[0]
            old, = [record for record in document["records"] if record["id"] == old_id]
            assert old["cancelled"] is True and old["attempts"] == 0 and old["deadline_at"] == deadline
            assert any(item["event"]["title"] == "Unknown recorded origin" for item in document["pending"])
            assert any(item["event"].get("attempt_id") == call.attempt_id for item in document["pending"])
            unknown = []
            with outbox.transaction(tmp_path, scenario.spec.run_id) as journal:
                while len(journal["pending"]) < outbox.MAX_PENDING:
                    event = event_envelope(_state(scenario), "stage_start", f"unknown-origin-{len(unknown) + 1}", "Capacity-only unknown")
                    unknown.append(event["id"])
                    outbox.enqueue(journal, event, time.time())
            assert scenario.kernel.control("stop", "protect-current-gate", subject=scenario.delivery.key) == "applied"
            observer.observe_committed()
            document = outbox.read_notifications(tmp_path, scenario.spec.run_id)
            assert len(document["pending"]) <= outbox.MAX_PENDING
            gates = [item for item in document["pending"] if item["event"]["kind"] == "boundary"]
            assert len(gates) == 1 and gates[0]["important"] is True and gates[0]["attempts"] == 0
            retained = {item["event"]["id"] for item in document["pending"]} | {record["id"] for record in document["records"]}
            assert set(unknown).issubset(retained)
            cancelled = [record for record in document["records"] if record["id"] in unknown]
            assert cancelled and all(record["cancel_reason"] == "pending capacity" and record["attempts"] == 0 for record in cancelled)
            observed.append(call.attempt_id)

    try:
        assert entered.wait(5)
        _recorded_agents(scenario, monkeypatch, started=started)
        for _ in range(40):
            scenario.step()
            if len(observed) == 2:
                break
        assert len(observed) == 2
    finally:
        release.set()
        observer.close()
    assert observer.failure_reason is None
    assert sent[1]["kind"] == "boundary"  # Important Gate precedes retained unknown ordinary cards.


def test_sender_one_poll_replaces_failed_live_start_with_the_newer_real_sibling_start(tmp_path):
    kernel = _kernel(tmp_path, count=2)
    execution = kernel.claim_executor("executor")
    operations = _operations(kernel, execution, count=2)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    process = {"pid": 21, "pgid": 21, "start_token": "first", "host_pid": 10, "host_start_token": "host"}
    kernel.agent_started(first, process)
    returned, newer_entered, release = Event(), Event(), Event()
    original = []

    def transport(*_args, **_kwargs):
        event = _active(tmp_path, "run-native")
        if event.get("attempt_id") == first.attempt_id:
            original.append(event["id"])
            returned.set()
            return {"outcome": "failed", "reason": "Original physical delivery failed"}
        if second[0] is not None and event.get("attempt_id") == second[0]:
            newer_entered.set()
            assert release.wait(10)
        return {"outcome": "success", "reason": None}

    second = [None]
    second_call = None
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport)
    try:
        assert returned.wait(5)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            items = outbox.read_notifications(tmp_path, "run-native")["pending"]
            failed = [item for item in items if item["event"]["id"] == original[0] and item["outcome"] == "failed"]
            if failed:
                break
            time.sleep(0.01)
        assert len(failed) == 1 and failed[0]["attempts"] == 1
        deadline = failed[0]["deadline_at"]
        call = operations.once().call
        assert call is not None and kernel.dispatch_agent(call)
        second_call = call
        second[0] = call.attempt_id
        kernel.agent_started(call, {**process, "pid": 22, "pgid": 22, "start_token": "second"})
        observer.observe_committed()
        assert newer_entered.wait(5)
        document = outbox.read_notifications(tmp_path, "run-native")
        cancelled, = [record for record in document["records"] if record["id"] == original[0]]
        assert cancelled["cancelled"] is True and cancelled["outcome"] == "failed"
        assert cancelled["attempts"] == 1 and cancelled["deadline_at"] == deadline
        assert cancelled["attempt_history"][0]["outcome"] == "failed"
        assert any(item["event"].get("attempt_id") == call.attempt_id for item in document["pending"])
        assert len(original) == 1
    finally:
        release.set()
        observer.close(drain=False)
        kernel.agent_exited(first, process, 1)
        if second_call is not None:
            kernel.agent_exited(second_call, {**process, "pid": 22, "pgid": 22, "start_token": "second"}, 1)
