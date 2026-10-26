"""Small notification projection scenarios; no transport or persistent fixtures."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_run.model import AgentCall, Issue, ParentIssue, Requirements, wire_json

if TYPE_CHECKING:
    from support.offline import GraphScenario


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
