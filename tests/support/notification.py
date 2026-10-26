"""Notification fixtures and committed projections over caller-owned Core state."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_run.model import AgentCall, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Requirements, RunSpec, wire_json

from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.core.operation_contract import DeliveryFacts
from agent_run.notifications.event_projection import project_event
from support.core_operations import CoreOperations, ReadOnlyPorts
from support.offline import GraphScenario, Scenario
from support.requirements_scenarios import independent_source


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


def _kernel(root: Path, count: int = 1) -> Kernel:
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
    inputs = replace(spec.inputs, notifications={"enabled": True, "mode": "detailed",
                                                 "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    final = Delivery("notification-final", DeliveryKind.FINAL_RUN, 1, "unread", base,
                     delivery.target_ref, "refs/heads/main")
    return Kernel.create(root / "kernel.sqlite3", replace(spec, inputs=inputs, deliveries=(*deliveries, final)),
                         requirements=_source(count))


def _project(kernel: Kernel, mode: str) -> list[dict[str, Any]]:
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
                 "language": run["language"], "created_at": run["created_at"]}
        frozen = deepcopy(state)
        result = [projected.content for row in tx.all("SELECT * FROM history ORDER BY position")
                  for projected in project_event(tx, dict(row), state, mode) if projected.visible]
        assert state == frozen
        return result


def _enabled_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
                      *, reviews: tuple[str, ...] = ("pass",), mode: str = "concise", language: str = "en") -> Scenario:
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language=language, notifications={"enabled": False, "mode": mode})
    original_create = Kernel.create

    def create(_cls: type[Kernel], path: Path, spec: RunSpec, **kwargs: Any) -> Kernel:
        spec = freeze_creation(spec, defaults,
                               prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
        assert spec.inputs is not None
        inputs = replace(spec.inputs, notifications={"enabled": True, "mode": mode,
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
        return original_create(path, replace(spec, language=language, inputs=inputs), **kwargs)

    source = independent_source()
    if kind == DeliveryKind.PARENT_ONLY:
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=()), issues=())
    else:
        first = source.issues[0]
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=(first.number,)), issues=(first,))
    # Freeze only input configuration. Workspace and local marker ownership are
    # produced by the real Core/effect path, never by a synthetic receipt.
    with monkeypatch.context() as patch:
        patch.setattr(Kernel, "create", classmethod(create))
        if kind == DeliveryKind.FINAL_RUN:
            # d847 builds and retains actual historical Ticket Completion.
            scenario = Scenario(tmp_path, kind, reviews=reviews)
            assert len(scenario.kernel.source_status()["completions"]) == 1
        else:
            scenario = GraphScenario(tmp_path, source, reviews=reviews if kind == DeliveryKind.PARENT_ONLY else ("pass",))
    return scenario



class _NoNotifications:
    def __init__(self, *_arguments):
        pass

    def observe_committed(self):
        pass

    def close(self, **_arguments):
        pass
