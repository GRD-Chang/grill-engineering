"""通知投影的未读历史不能取消已有投递事实或重置其预算。"""
from pathlib import Path
from copy import deepcopy
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_run import notification_outbox as outbox
from agent_run.notification_event_facts import event_envelope
from agent_run.notifications import Notifications, read_notifications
from support.notification import state_with_round


def test_unloaded_important_attempt_is_delivered_with_its_existing_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_round()
    state["notifications"] = {"enabled": True, "mode": "detailed"}
    state["_notification_scope"] = {"attempt_ids": ["a1"], "resume_ids": [], "receipt": {"history_position": 1}}
    retained = event_envelope(state, "stage_end", "old-attempt", "Retained original evidence",
                              important=True, attempt_id="old-attempt")
    with outbox.transaction(tmp_path, state["run_id"]) as document:
        outbox.enqueue(document, retained, time.time())
        original_deadline = document["pending"][0]["deadline_at"]
    sent = []

    def send(_config, card, _identity, _cancel, **_kwargs):
        sent.append(card["header"]["title"]["content"])
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, state)
    sender.close()
    assert "Retained original evidence" in sent
    record = next(record for record in read_notifications(tmp_path, state["run_id"])["records"] if record["id"] == retained["id"])
    assert record["outcome"] == "success"
    assert record["deadline_at"] == original_deadline
    assert "cancel_reason" not in record


def test_cold_and_steady_notification_queries_do_not_open_all_old_contexts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import sqlite_work_evidence, sqlite_work_execution_attempts, sqlite_work_execution_invocations
    from agent_run.notification_events import events
    from agent_run.sqlite_state import SQLiteStateRepository
    from support.work_records import attempt, invocation

    repository = SQLiteStateRepository(tmp_path)
    archived, invocations = [], []
    started = datetime(2026, 1, 1, tzinfo=UTC)
    for number in range(500):
        context = {"position": number, "original_report": "retained evidence " * 32}
        archived.append(attempt("ticket:2", role="development", context=context))
        invocations.append({**invocation("ticket:2", (started + timedelta(seconds=number)).isoformat(),
                                        context=context, role="development", phase="developing"),
                            "reported_thread_id": f"thread-{number}"})
    repository.save("run-1", {
        "run_id": "run-1", "repository": "example/project", "language": "zh", "status": "active",
        "delivery_type": "ticket_run", "parent": {"number": 1, "title": "Parent"},
        "ticket_graph": {"tickets": {"2": {"number": 2, "title": "Ticket 2"}}},
        "ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing",
                                "development_summary": "Only the current Candidate owns this summary",
                                "semantic_attempt_history": archived}},
        "agent_invocation_history": invocations,
    })
    opened = []
    real = sqlite_work_evidence.get_evidence

    def read(*args, **kwargs):
        if kwargs.get("kind") == "review_context":
            opened.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite_work_execution_attempts, "get_evidence", read)
    monkeypatch.setattr(sqlite_work_execution_invocations, "get_evidence", read)
    cold = repository.load_notifications("run-1", {})
    first = next(event for event in events(cold) if event["kind"] == "ticket_started")
    assert first["attempt_id"] == archived[0]["attempt_id"]
    assert first["started_at"] == invocations[0]["started_at"]
    assert len(opened) < 80
    receipt = {"observation": cold["_notification_scope"]["receipt"], "milestones": [first["id"]]}
    opened.clear()
    steady = repository.load_notifications("run-1", receipt)
    assert not steady["agent_invocation_history"]
    assert len(opened) <= 2
    complete = repository.load_history("run-1")
    assert len(complete["agent_invocation_history"]) == 500
    original_events = {event["id"]: event for event in events(complete)}
    for event in events(cold):
        if event["kind"] == "stage_end":
            # Unloaded owner history cannot make an old Attempt borrow the
            # current Candidate's summary or Artifact.
            assert event == original_events[event["id"]]


def _current_executor_query(tmp_path: Path, history: int = 0):
    from agent_run.executor_host import _process_start_token
    from agent_run.run_lifecycle import prepare_action_application_receipt
    from agent_run.sqlite_state import SQLiteStateRepository
    from agent_run.task_control import TaskControlStore, TaskKey
    from support.work_records import attempt, invocation

    repository = SQLiteStateRepository(tmp_path)
    archived, invocations = [], []
    started = datetime(2026, 1, 1, tzinfo=UTC)
    for ordinal in range(1, history + 1):
        record = attempt(role="development", ordinal=ordinal)
        archived.append(record)
        current = invocation(started=(started + timedelta(seconds=ordinal)).isoformat(),
                             role="development", phase="developing")
        current.update(semantic_attempt={**record, "status": "pending"}, reported_thread_id=f"old-{ordinal}",
                       currentness_boundary={"fixture": {}, "ordinal": ordinal})
        current["semantic_attempt"].pop("outcome")
        invocations.append(current)
    pending = attempt(role="development", ordinal=history + 1, completed=False)
    active = invocation(started=(started + timedelta(seconds=history + 1)).isoformat(),
                        status="running", role="development", phase="developing")
    active.update(semantic_attempt=deepcopy(pending), reported_thread_id="current-developer",
                  currentness_boundary={"fixture": {}, "ordinal": history + 1})
    job = {"ticket_number": 2, "ticket_branch_generation": 1, "phase": "developing",
           "development_thread_id": "current-developer", "pending_semantic_attempt": pending,
           "semantic_attempt_history": archived}
    state = {
        "run_id": "run-1", "repository": "example/project", "parent": {"number": 1, "title": "Parent"},
        "language": "zh", "status": "active", "delivery_type": "ticket_run",
        "notifications": {"enabled": True, "mode": "detailed"},
        "ticket_graph": {"tickets": {"2": {"number": 2, "title": "Ticket 2"}}},
        "ticket_jobs": {"2": job}, "active_ticket_job": deepcopy(job),
        "agent_invocation_history": [*invocations, active], "active_agent_invocation": active,
        "ticket_agent_invocations": {"2": deepcopy(active)},
    }
    control = TaskControlStore(tmp_path)
    task = TaskKey(tmp_path / "workspace", state["repository"], 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=state["run_id"]).generation
    control.bind_run(task, claim.action_id, state["run_id"], generation=generation)
    action = control.load(task)["action"]
    prepare_action_application_receipt(state, action)
    repository.save(state["run_id"], state)
    control.mark_handshake(task, action_id=claim.action_id, generation=generation,
                           pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    control.complete_action(task, action_id=claim.action_id, generation=generation, result_status="active")
    return repository, control, task, pending["attempt_id"]


def test_cold_queue_retains_the_proven_live_attempt_and_really_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.notification_events import events

    repository, control, task, identity = _current_executor_query(tmp_path, history=40)
    before = control.load(task)
    state = repository.load_notifications("run-1", {})
    projected = events(state)
    live = next(event for event in projected if event.get("attempt_id") == identity)
    assert len(projected) > outbox.MAX_PENDING
    assert live["kind"] == "stage_start" and live["live"] is True
    sent = []

    def send(_config, _card, event_id, _cancel, **_kwargs):
        sent.append(event_id)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, state, query_state=lambda observation: repository.load_notifications("run-1", observation))
    sender.close()
    assert len(sent) == 1
    records = read_notifications(tmp_path, "run-1")["records"]
    assert next(record for record in records if record["id"] == live["id"])["outcome"] == "success"
    assert sum(record.get("cancel_reason") == "obsolete_progress" for record in records) > outbox.MAX_PENDING
    assert control.load(task) == before
    complete = repository.load_history("run-1")
    assert len(complete["agent_invocation_history"]) == 41
    assert "_executor_control" not in complete


@pytest.mark.parametrize("mutation", [
    "DELETE FROM control_tasks",
    "UPDATE control_tasks SET protocol=2",
    "UPDATE control_tasks SET repository='other/project'",
    "UPDATE control_tasks SET parent_number=99",
    "UPDATE control_tasks SET workspace='/another/workspace'",
    "UPDATE control_tasks SET run_state_dir='/another/state'",
    "UPDATE control_tasks SET run_id='another-run'",
    "UPDATE control_tasks SET current_action_id='stale-action'",
    "UPDATE control_tasks SET current_executor_generation=99",
    "UPDATE control_actions SET status='failed'",
    "UPDATE control_actions SET application_observed=0",
    "UPDATE control_actions SET kind='stop'; UPDATE run_application_receipt SET kind='stop'",
    "UPDATE control_actions SET kind='abandon'; UPDATE run_application_receipt SET kind='abandon'",
    "UPDATE control_actions SET payload_digest='different'",
    "UPDATE run_application_receipt SET executor_generation=99",
    "UPDATE control_executors SET status='starting'",
    "UPDATE control_executors SET status='exited'",
    "UPDATE control_executors SET run_id='another-run'",
    "UPDATE control_executors SET pid=pid+1",
    "UPDATE control_executors SET process_start_token='old-process'",
    "UPDATE control_executors SET handshake_at=NULL",
    "UPDATE control_executors SET handshake_at='invalid'",
    "UPDATE control_executors SET exited_at='2026-01-01T00:00:00+00:00'",
])
def test_unproven_executor_cannot_make_an_attempt_live(tmp_path: Path, mutation: str) -> None:
    from agent_run.notification_events import events

    repository, _control, _task, identity = _current_executor_query(tmp_path)
    # Deliberately damage one narrow ownership fact without changing the Run
    # Attempt; a notification query must not turn that into liveness evidence.
    with sqlite3.connect(repository.path) as connection:
        connection.executescript(mutation)
    state = repository.load_notifications("run-1", {})
    assert state["_executor_control"]["activity"] == "unknown"
    event = next(event for event in events(state) if event.get("attempt_id") == identity)
    assert event["kind"] == "ticket_started" and event["live"] is False


@pytest.mark.parametrize(("kind", "status"), [
    ("run", "accepted"), ("resume", "applying"), ("requeue", "completed"),
    ("approve", "completed"), ("revise", "completed"),
])
def test_handshaken_executor_liveness_covers_legal_advancement_actions(
    tmp_path: Path, kind: str, status: str,
) -> None:
    from agent_run.notification_events import events

    repository, _control, _task, identity = _current_executor_query(tmp_path)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE control_actions SET kind=?,status=?", (kind, status))
        connection.execute("UPDATE run_application_receipt SET kind=?", (kind,))
    state = repository.load_notifications("run-1", {})
    assert state["_executor_control"]["activity"] == "running"
    event = next(event for event in events(state) if event.get("attempt_id") == identity)
    assert event["kind"] == "ticket_started" and event["live"] is True


def test_live_executor_query_allows_the_optional_unpinned_state_directory(tmp_path: Path) -> None:
    repository, _control, _task, _identity = _current_executor_query(tmp_path)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE control_tasks SET run_state_dir=NULL")
    state = repository.load_notifications("run-1", {})
    assert state["_executor_control"]["activity"] == "running"


def test_live_executor_query_accepts_a_symlink_to_the_same_database(tmp_path: Path) -> None:
    from agent_run.sqlite_state import SQLiteStateRepository

    _repository, _control, _task, _identity = _current_executor_query(tmp_path / "physical")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "physical", target_is_directory=True)
    repository = SQLiteStateRepository(alias)
    with repository.atomic():
        state = repository.load_notifications("run-1", {})
    assert state["_executor_control"]["activity"] == "running"
