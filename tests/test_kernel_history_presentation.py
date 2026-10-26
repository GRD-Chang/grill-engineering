"""Human History consumes native Attempt/Invocation facts without another clock."""
from pathlib import Path

import pytest

from agent_run.model import Issue, ParentIssue, Requirements, wire_json
from agent_run.queries.run import read
from support.offline import GraphScenario
from test_kernel_notification_events import _recorded_agents
from test_kernel_query_cli import _snapshot


@pytest.fixture
def continued_round(tmp_path: Path, monkeypatch, request):
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

    _recorded_agents(scenario, monkeypatch, agent, started)
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


@pytest.mark.parametrize("continued_round", ["complete", "partial", "unknown"], indirect=True)
def test_history_timing_distinguishes_complete_partial_and_unknown_without_counting_human_wait(
    continued_round, tmp_path: Path,
) -> None:
    from agent_run.queries.execution import invocation_timing

    scenario, calls, timing = continued_round
    before = _snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "history")
    attempt, = document.data["history"]["attempts"]
    assert attempt["id"] == calls[0].attempt_id
    assert [(item["model"], item["effort"]) for item in attempt["invocations"]] == [
        ("original-model", "low"), ("new-model", "high")]
    facts = invocation_timing(attempt["invocations"])
    assert facts["execution_timing_complete"] is (timing == "complete")
    if timing == "complete":
        assert facts["execution_seconds"] == 5
    else:
        assert "execution_seconds" not in facts
    if timing != "unknown":
        assert facts["recorded_execution_seconds"] == 5
    else:
        assert "recorded_execution_seconds" not in facts
    assert _snapshot(tmp_path) == before


def test_closed_activity_without_worker_exit_does_not_prove_complete_execution(tmp_path: Path) -> None:
    from agent_run.queries.execution import invocation_timing
    from test_kernel_query_cli import _create, _operations

    kernel = _create(tmp_path)
    kernel.clock = lambda: 100.0
    operations = _operations(kernel, kernel.claim_executor("original-query-executor"))
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    operations.runner.authorize_agent(call)
    # Explicit Core process-observation seam, without launching or killing an
    # OS process. A closed Agent activity segment is not an exit observation.
    kernel.agent_started(call, {"pid": 23, "pgid": 23, "start_token": "synthetic-original",
                                "host_pid": 10, "host_start_token": "synthetic-host"})
    kernel.observe_worker_activity(call, "start", active=True, observed_at=101.0, evidence={"type": "turn.started"})
    kernel.observe_worker_activity(call, "end", active=False, observed_at=103.0, evidence={"type": "turn.completed"})
    before = _snapshot(tmp_path)
    attempt, = read(kernel.storage.path, "history").data["history"]["attempts"]
    assert invocation_timing(attempt["invocations"]) == {
        "execution_timing_complete": False, "recorded_execution_seconds": 2}
    assert _snapshot(tmp_path) == before
