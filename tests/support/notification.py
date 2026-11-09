"""Notification fixtures and committed projections over caller-owned Core state."""
from __future__ import annotations
from support.repository_fixture import create_kernel

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.model import AgentCall, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Policy, Requirements, RunSpec, wire_json

from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.core.operation_contract import DeliveryFacts
from agent_run.notifications.event_projection import event_state, project_event
from support.core_operations import CoreOperations, ReadOnlyPorts
from support.offline import GraphScenario, Scenario
from support.requirements_scenarios import independent_source


def observe_and_wait(observer: Any, *, timeout: float = 3) -> None:
    """Await one owned bounded projection, without production request counters."""
    from threading import Event
    from agent_run.storage import notification_outbox as outbox

    before = outbox.read_notifications(observer.state_root, observer.run_id)
    projected = Event()
    original = observer._observe_committed

    def observed() -> bool:
        healthy = original()
        if healthy:
            projected.set()
        return bool(healthy)

    # An already-caught-up cursor cannot prove a new material read. This test
    # barrier waits for the real owner; large history may still need more pages.
    observer._observe_committed = observed
    try:
        observer.observe_committed()
        assert projected.wait(timeout), "Notification owner did not complete a healthy projection"
        document = outbox.read_notifications(observer.state_root, observer.run_id)
        assert document.get("native_run_start") is True
        for cursor in ("native_cursor", "native_boundary_cursor"):
            assert document[cursor] >= before.get(cursor, 0)
    finally:
        observer._observe_committed = original


def sender_attempt_note(error: BaseException, root: Path, run_id: str, *,
                        first_id: str | None, gate_ids: list[str]) -> None:
    """Read the original first/Gate records after sender close; never replay."""
    try:
        from agent_run.storage import notification_outbox as outbox
        document = outbox.read_notifications(root, run_id)
        selected = {first_id, *gate_ids}
        entries = []
        fields = ('outcome', 'attempts', 'deadline_at', 'next_attempt_at', 'cancelled', 'cancel_reason')
        for section in ('pending', 'records'):
            for item in document[section]:
                identity = item['event']['id'] if section == 'pending' else item['id']
                if identity not in selected:
                    continue
                row = {'id': identity, 'section': section,
                       **{key: item[key] for key in fields if key in item}}
                history = item.get('attempt_history', [])
                kept = history if len(history) <= 4 else [history[0], *history[-3:]]
                row['attempt_history'] = [{key: attempt[key] for key in
                    ('number', 'started_at', 'finished_at', 'outcome', 'not_sent') if key in attempt}
                    for attempt in kept]
                entries.append(row)
        payload = json.dumps({'run_id': run_id, 'first_id': first_id, 'gate_ids': gate_ids,
                              'sender_attempts': entries}, separators=(',', ':'))
        error.add_note(payload if len(payload.encode()) <= 8192 else 'Sender attempt evidence exceeded 8192 bytes')
    except BaseException as unavailable:
        error.add_note('Sender attempt evidence unavailable: ' + type(unavailable).__name__)


def state_with_round(role: str = "development", outcome: str = "candidate") -> dict[str, Any]:
    attempt = {"attempt_id": "a1", "role": role, "work_subject": "run:r",
               "ordinal": 1, "generation": 1, "status": "completed", "outcome": outcome,
               "development_summary": "已实现输入校验"}
    return {"language": "zh", "run_id": "r", "repository": "o/repo", "parent": {"number": 241, "title": "飞书通知"},
            "status": "run_review_pending", "semantic_agent_attempts": [attempt],
            "agent_invocation_history": [{"semantic_attempt": attempt.copy(), "status": "completed", "reported_thread_id": "thread-1",
                                          "started_at": "2026-01-01T00:00:00+00:00",
                                          "execution_segments": [{"started_at": "2026-01-01T00:00:00+00:00",
                                                                  "ended_at": "2026-01-01T00:00:09+00:00"}],
                                          "ended_at": "2026-01-01T00:00:09+00:00"}]}


def parallel_state(reason: str = "agent_requires_human", mode: str = "concise") -> dict[str, Any]:
    running = {"ticket_number": 2, "phase": "developing", "generation": 1}
    return {
        "run_id": "r", "language": "zh", "repository": "example/project",
        "parent": {"number": 1, "title": "Parent"}, "status": "active",
        "notifications": {"enabled": True, "mode": mode},
        "ticket_graph": {"tickets": {"2": {"title": "Running"}, "3": {"title": "原始标题"}}},
        "active_ticket_job": running,
        "ticket_jobs": {"2": running, "3": {
            "ticket_number": 3, "generation": 7, "phase": "blocked", "blocked_reason": reason,
            "human_blockers": ["External account required."], "execution_resume_phase": "developing",
        }},
    }


def recorded_agents(scenario: GraphScenario, monkeypatch: pytest.MonkeyPatch,
                     result: Callable[[AgentCall], bytes] | None = None,
                     started: Callable[[AgentCall], None] | None = None, *, activity: bool = True) -> None:
    original_agent = scenario.agent
    original_receive = scenario.kernel.receive
    completed: dict[tuple[str, int], tuple[dict[str, Any], bytes]] = {}

    def agent(call: AgentCall) -> bytes:
        # These are explicit synthetic observations, never an OS process claim.
        # Normal Scenario.step and direct fixture consumers share this leaf.
        process = {"pid": 21, "pgid": 21, "start_token": "synthetic-process",
                   "host_pid": 10, "host_start_token": "synthetic-host"}
        assert scenario.kernel.dispatch_agent(call)
        scenario.runner().authorize_agent(call)
        scenario.kernel.agent_started(call, process)
        if activity:
            scenario.kernel.agent_source_ready(call)
            scenario.kernel.observe_worker_activity(call, "fixture-start", active=True,
                observed_at=scenario.kernel.clock(), evidence={"type": "turn.started", "source": "synthetic-agent"})
        if started is not None:
            started(call)
        raw = (result or original_agent)(call)
        if activity:
            scenario.kernel.observe_worker_activity(call, "fixture-end", active=False,
                observed_at=scenario.kernel.clock(), evidence={"type": "turn.completed", "source": "synthetic-agent"})
        # A throwing/interrupted callback supplies no known exit. Only this
        # exact synchronously returned output may settle its synthetic process.
        completed[(call.invocation_id, call.output_ordinal)] = (process, raw)
        return raw

    def receive(call: AgentCall, raw: bytes) -> None:
        original_receive(call, raw)
        known = completed.pop((call.invocation_id, call.output_ordinal), None)
        if known is not None and known[1] == raw:
            scenario.kernel.agent_exited(call, known[0], 0)

    monkeypatch.setattr(scenario, "agent", agent)
    monkeypatch.setattr(scenario.kernel, "receive", receive)

@pytest.fixture
def continued_round(tmp_path: Path, monkeypatch, request):
    from support.offline import GraphScenario

    timing = request.param
    scenario = GraphScenario(tmp_path / "scenario", Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    scenario.kernel.configure_profiles("original-profile", overrides={
        "development_model": "original-model", "development_effort": "low"})
    now = [100.0]
    scenario.kernel.clock = lambda: now[0]
    calls = []
    original_agent = scenario.agent

    def started(call):
        start = now[0]
        if timing != "unknown":
            scenario.kernel.observe_worker_activity(call, "turn-start", active=True,
                observed_at=start + 1, evidence={"type": "turn.started"})
            scenario.kernel.observe_worker_activity(call, "turn-end", active=False,
                observed_at=start + (3 if not calls else 4), evidence={"type": "turn.completed"})
            if timing == "partial" and not calls:
                scenario.kernel.observe_worker_activity(call, "unfinished-turn", active=True,
                    observed_at=start + 4, evidence={"type": "turn.started"})
        now[0] += 5

    def agent(call):
        calls.append(call)
        if len(calls) == 1:
            return wire_json({"result_kind": "human_blocker", "summary": None,
                              "human_blockers": ["Keep the original decision / 保留原文"]})
        return original_agent(call)

    recorded_agents(scenario, monkeypatch, agent, started, activity=False)
    assert scenario.run().kind == "paused"
    scenario.kernel.configure_profiles("new-profile", overrides={
        "development_model": "new-model", "development_effort": "high"})
    now[0] = 200.0  # This human wait must never become Agent execution time.
    assert scenario.kernel.control("resume", "original-answer", subject="ticket:2", new_thread=True,
                                   message="原始回复\r\nKeep exactly this.") == "applied"
    assert scenario.step().call is not None
    assert scenario.step().kind == "progress"
    assert len(calls) == 2 and calls[0].attempt_id == calls[1].attempt_id
    assert calls[0].thread_id != calls[1].thread_id
    return scenario, calls, timing


def _source(count: int = 1) -> Requirements:
    return Requirements(ParentIssue(1, "Parent", "Original notification requirement", tuple(range(2, count + 2))),
        tuple(Issue(number, "Ticket", "ticket-v1", "OPEN", frozenset({"ready-for-agent"}))
              for number in range(2, count + 2)))


def _operations(kernel, execution, *, count: int = 1) -> CoreOperations:
    ports = ReadOnlyPorts(_source(count), "refs/heads/source", "refs/heads/target",
                          DeliveryFacts(Commit("base", "tree"), None))
    return CoreOperations(kernel, execution, lambda binding: ports)


def _kernel(root: Path, count: int = 1, *, notifications: bool = True) -> Kernel:
    defaults = UserDefaultsStore(root / "defaults.json")
    defaults.configure(language="en", notifications={"enabled": False, "mode": "detailed"})
    base = Commit("base", "tree")
    delivery = Delivery("native-ticket", DeliveryKind.TICKET, 2, "ticket-v1", base,
                        "refs/heads/source", "refs/heads/target")
    deliveries = ((delivery,) if count == 1 else tuple(
        replace(delivery, key=f"native-ticket-{index}", issue=index + 2) for index in range(count)))
    spec = freeze_creation(RunSpec("run-native", "example/project", 1, "parent-v1", "graph-v1",
                                   deliveries, repository_root=str(root)), defaults,
                           prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
    assert spec.inputs is not None
    inputs = replace(spec.inputs, notifications={"enabled": notifications, "mode": "detailed",
                                                 "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    final = Delivery("notification-final", DeliveryKind.FINAL_RUN, 1, "unread", base,
                     delivery.target_ref, "refs/heads/main")
    return create_kernel(root / "kernel.sqlite3", replace(spec, inputs=inputs, deliveries=(*deliveries, final)),
                         requirements=_source(count))


def _completions(kernel: Kernel) -> list[dict[str, Any]]:
    """Only the selected Run's real Completion projection, never seeded facts."""
    return [item for item in _project(kernel, "concise") if item["kind"] == "ticket_completed"]


def _project(kernel: Kernel, mode: str) -> list[dict[str, Any]]:
    with kernel.storage.transaction(write=False) as tx:
        state = event_state(tx)
        frozen = deepcopy(state)
        result = [projected.content for row in tx.all("SELECT * FROM history WHERE run_id=? ORDER BY position", (state['run_id'],))
                  for projected in project_event(tx, dict(row), state, mode) if projected.visible]
        assert state == frozen
        return result


def _enabled_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
                      *, reviews: tuple[str, ...] = ("pass",), mode: str = "concise", language: str = "en",
                      policy: Policy | None = None) -> Scenario:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language=language, notifications={"enabled": False, "mode": mode})
    from support import offline
    original_create = offline.create_kernel

    def create(path: Path, spec: RunSpec, **kwargs: Any) -> Kernel:
        spec = freeze_creation(spec, defaults,
                               prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={"enabled": True, "mode": mode,
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
        # These are declared fixture inputs, not a defaults-resolution test.
        return original_create(path, replace(spec, language=language, inputs=inputs,
            policy=spec.policy if policy is None else policy), **kwargs)

    source = independent_source()
    if kind == DeliveryKind.PARENT_ONLY:
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=()), issues=())
    else:
        first = source.issues[0]
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=(first.number,)), issues=(first,))
    # Freeze only input configuration. Workspace and local marker ownership are
    # produced by the real Core/effect path, never by a synthetic receipt.
    with monkeypatch.context() as patch:
        patch.setattr(offline, "create_kernel", create)
        if kind == DeliveryKind.FINAL_RUN:
            # d847 builds and retains actual historical Ticket Completion.
            scenario = Scenario(tmp_path, kind, reviews=reviews, policy=policy)
            assert len(scenario.kernel.source_status()["completions"]) == 1
        else:
            scenario = GraphScenario(tmp_path, source, policy=policy,
                                     reviews=reviews if kind == DeliveryKind.PARENT_ONLY else ("pass",))
    return scenario



class _NoNotifications:
    def __init__(self, *_arguments, storage):
        self.storage = storage

    def observe_committed(self):
        pass

    def close(self, **_arguments):
        pass


def assert_confirmed_status_comment(projection, journal, comments, writes):
    """Account for one exact display receipt, never a marker-prefix exception."""
    from agent_run.notifications.status_comment import StatusComments

    target = projection.target
    assert journal.get('native_binding') == [target.run_id, target.repository, target.parent_issue]
    assert set(journal['remote_status']['bindings']) == {target.key}
    entry = StatusComments._entry(deepcopy(journal), projection)
    receipt = entry['receipt']
    comment, = comments
    assert type(comment['id']) is int and type(receipt['comment_id']) is int
    assert comment['id'] == entry['comment_id'] == receipt['comment_id']
    assert comment['body'] == entry['confirmed_body']
    assert comment['body'].startswith(entry['marker'] + '\n')
    assert comment['user']['login'] == target.actor
    assert comment['issue_url'] == f'https://api.github.com/repos/{target.repository}/issues/{target.pr_number}'
    post = f'repos/{target.repository}/issues/{target.pr_number}/comments'
    patch = f'repos/{target.repository}/issues/comments/{comment["id"]}'
    completion = f'repos/{target.repository}/issues/{target.issue}/comments'
    status_writes = [(row[1], row[row.index('--method') + 1]) for row in writes
                     if '/comments' in row[1] and row[1] != completion]
    assert status_writes.count((post, 'POST')) == 1
    assert all(item in {(post, 'POST'), (patch, 'PATCH')} for item in status_writes)
