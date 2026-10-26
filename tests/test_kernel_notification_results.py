"""Manual Resume notifications require progress by the original target."""

import agent_run
from pathlib import Path

import pytest

from support.core_operations import CoreOperations, scripted_return
from agent_run.model import Issue, ParentIssue, Requirements
from agent_run.model import wire_json
from support.offline import GraphScenario
from support.notification import _project
from support.notification import recorded_agents as _recorded_agents


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_resume_waits_for_its_own_invocation_and_ignores_a_sibling_start(tmp_path: Path, monkeypatch, mode):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2, 3)),
        tuple(Issue(number, f"Ticket {number}", f"Original task {number}", "OPEN", labels=("ready-for-agent",))
              for number in (2, 3))))
    original = scenario.agent
    first = True

    def agent(call):
        nonlocal first
        raw = original(call)
        if call.subject == "ticket:2" and first:
            first = False
            return wire_json({"result_kind": "human_blocker", "summary": None,
                              "human_blockers": ["Choose the original target's behavior"]})
        return raw

    _recorded_agents(scenario, monkeypatch, agent)
    assert scenario.run().kind == "paused"
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    sibling = operations.until(lambda step: step.call is not None)
    assert sibling.call.subject == "ticket:3"
    scenario.select("ticket:2")
    assert scenario.kernel.control("resume", "resume-original-target", subject="ticket:2",
                                   message="Use the original decision") == "applied"
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, mode))
    scripted_return(scenario.kernel, operations.runner, sibling.call, scenario.agent)
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, mode))
    resumed = operations.until(lambda step: step.call is not None)
    assert resumed.call is not None
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT action_id FROM invocations WHERE id=?", (resumed.call.invocation_id,))[0] == "resume-original-target"
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, mode))
    scripted_return(scenario.kernel, operations.runner, resumed.call, scenario.agent)
    results = [event for event in _project(scenario.kernel, mode) if event["kind"] == "resume"]
    assert len(results) == 1
    assert results[0]["resume_id"] == "resume-original-target" and results[0]["resume_outcome"] == "started"
    assert results[0]["work_subject"] == "ticket:2" and results[0]["generation"] == 1
    assert results[0]["attempt_id"] == resumed.call.attempt_id and results[0]["evidence"] == "worker_started"


def test_approval_update_is_the_original_invalidation_not_a_second_approval_request(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    assert scenario.run().kind == "complete"
    for _ in range(40):
        scenario.step()
        if scenario.kernel.source_status()["final_subject"] is not None:
            break
    else:
        raise AssertionError("Real Ticket Completion did not admit Final")
    scenario.select("final:1")
    assert scenario.run().kind == "approval"
    assert not any(event["kind"] == "approval_update" for event in _project(scenario.kernel, "concise"))
    scenario.port.external_remote_append(scenario.delivery.target_ref, {"external.txt": "New remote work\n"}, "approval-update")
    assert scenario.step().reason == "updating"
    with scenario.kernel.storage.transaction(write=False) as tx:
        invalidated = tx.one("SELECT * FROM history WHERE subject=? AND kind='boundary_update' ORDER BY position DESC LIMIT 1",
                             (scenario.delivery.key,))
        position, material = invalidated["position"], invalidated["material_id"]
    updates = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "approval_update"]
    assert len(updates) == 1
    assert updates[0]["source_position"] == position and updates[0]["source_material_id"] == material
    assert updates[0]["work_subject"] == scenario.delivery.key and updates[0]["generation"] == 1
    assert scenario.run().kind == "approval"
    assert [event for event in _project(scenario.kernel, "concise") if event["kind"] == "approval_update"] == updates
    assert not any(event["kind"] == "approval_ready" for event in _project(scenario.kernel, "concise"))


def test_effort_keeps_closed_partial_unknown_segments_and_original_thread_config_as_of_each_result(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    now = [100.0]
    scenario.kernel.clock = lambda: now[0]
    original = scenario.agent
    calls = []

    def agent(call):
        calls.append(call)
        if len(calls) == 1:
            return wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Original decision"]})
        return original(call)

    def started(call):
        start = now[0]
        scenario.kernel.observe_worker_activity(call, "turn-1-start", active=True, observed_at=start + 1, evidence={"type": "turn.started"})
        scenario.kernel.observe_worker_activity(call, "turn-1-end", active=False, observed_at=start + (3 if not calls else 4), evidence={"type": "turn.completed"})
        if not calls:
            scenario.kernel.observe_worker_activity(call, "turn-2-start", active=True, observed_at=start + 4, evidence={"type": "turn.started"})
        now[0] += 5

    _recorded_agents(scenario, monkeypatch, agent, started)
    assert scenario.run().kind == "paused"
    before = next(event for event in _project(scenario.kernel, "detailed") if event["kind"] == "stage_end")
    assert before["execution_timing_complete"] is False and before["duration_seconds"] is None
    assert before["recorded_execution_seconds"] == 2
    scenario.kernel.configure_profiles("change-profile", overrides={"development_model": "new-thread-model", "development_effort": "high"})
    assert scenario.kernel.control("resume", "new-thread-resume", subject="ticket:2", new_thread=True,
                                   message="Original decision supplied") == "applied"
    now[0] = 110.0
    step = scenario.step()
    assert step.call is not None and step.call.attempt_id == calls[0].attempt_id
    assert step.call.thread_id != calls[0].thread_id
    assert scenario.step().kind == "progress"
    ended = [event for event in _project(scenario.kernel, "detailed") if event["kind"] == "stage_end"]
    assert ended[0] == before  # Later continuation must not rewrite the earlier human result.
    result = ended[-1]
    assert result["execution_timing_complete"] is False and result["duration_seconds"] is None
    assert result["recorded_execution_seconds"] == 5
    development = result["effort"]["development"]
    assert development["rounds"] == 1 and development["shared_rounds"] is True
    assert development["execution_timing_complete"] is False and "execution_seconds" not in development
    assert development["recorded_execution_seconds"] == 5
    configs = {config["model"]: config for config in development["configurations"]}
    assert set(configs) == {calls[0].profile.model, "new-thread-model"}
    assert configs["new-thread-model"]["reasoning_effort"] == "high"
    assert all(config["rounds"] == 1 for config in configs.values())
    from agent_run.queries.run import read
    query = read(scenario.kernel.storage.path, "history").data
    first_output = query["history"]["attempts"][0]["invocations"][0]["outputs"][0]
    assert first_output["elapsed_seconds"] is None and first_output["recorded_execution_seconds"] == 2


def test_controller_only_resume_waits_for_consumption_of_the_original_target_effect(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    for _ in range(16):
        if scenario.step().call is not None:
            break
    else:
        raise AssertionError("Original Development did not start")
    assert scenario.step().kind == "progress"
    assert scenario.kernel.status("ticket:2")["phase"] == "candidate"
    assert scenario.kernel.control("stop", "stop-controller", subject="ticket:2") == "applied"
    assert scenario.kernel.control("resume", "resume-controller", subject="ticket:2") == "applied"
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, "concise"))
    candidates = []
    execute = scenario.port.execute
    def capture(effect, authorize=None):
        if effect.operation == "candidate":
            candidates.append(effect)
        return execute(effect, authorize)
    monkeypatch.setattr(scenario.port, "execute", capture)
    scenario.step()
    assert len(candidates) == 1 and candidates[0].operation == "candidate"
    prepared = candidates[0]
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, "concise"))
    assert scenario.step().kind == "progress"
    results = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "resume"]
    assert len(results) == 1
    assert results[0]["resume_id"] == "resume-controller" and results[0]["resume_outcome"] == "started"
    assert results[0]["evidence"] == "effect_consumed" and results[0]["effect_id"] == prepared.effect_id
    assert results[0]["work_subject"] == "ticket:2" and results[0]["generation"] == 1


def test_whole_resume_without_an_invocation_fk_requires_original_stop_and_actual_run_progress(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    for _ in range(16):
        if scenario.step().call is not None:
            break
    else:
        raise AssertionError("Original Development did not start")
    assert scenario.step().kind == "progress"
    assert scenario.kernel.control("stop", "stop-whole-run") == "applied"
    scenario.kernel.confirm_executor_exit(scenario.execution)
    assert scenario.kernel.control("resume", "resume-whole-run") == "applied"
    scenario.execution = scenario.kernel.claim_executor("continued-executor")
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, "concise"))
    candidates = []
    execute = scenario.port.execute
    def capture(effect, authorize=None):
        if effect.operation == "candidate":
            candidates.append(effect)
        return execute(effect, authorize)
    monkeypatch.setattr(scenario.port, "execute", capture)
    scenario.step()
    assert len(candidates) == 1
    effect = candidates[0]
    assert effect.operation == "candidate"
    assert not any(event["kind"] == "resume" for event in _project(scenario.kernel, "concise"))
    assert scenario.step().kind == "progress"
    results = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "resume"]
    assert len(results) == 1 and results[0]["resume_id"] == "resume-whole-run"
    assert results[0]["resume_scope"] == "run" and results[0]["resume_subject"] is None
    assert results[0]["resume_generation"] is None and results[0]["task_number"] == 1
    assert results[0]["effect_id"] == effect.effect_id and results[0]["resumed_from_action"] == "stop-whole-run"


@pytest.mark.parametrize("outcome", ["failed", "human_action"])
def test_resume_failure_and_human_result_bind_the_original_gate_without_a_second_card(tmp_path, monkeypatch, outcome):
    import agent_run.notifications.event_projection as projection

    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    blocked = wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Exact original decision"]})
    _recorded_agents(scenario, monkeypatch, lambda call: blocked)
    assert scenario.run().kind == "paused"
    assert scenario.kernel.control("resume", "resume-gate-result", subject="ticket:2",
                                   message="Continue this exact target") == "applied"
    continued = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).once()
    assert continued.call is not None
    if outcome == "failed":
        assert scenario.kernel.dispatch_agent(continued.call)
        scenario.kernel.agent_unavailable(continued.call, "worker_preparation_failed", {"message": "Original launch failed"})
    else:
        scripted_return(scenario.kernel, scenario.runner(), continued.call, scenario.agent)
        assert scenario.step().kind == "paused"
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = dict(tx.one("SELECT * FROM history WHERE subject='ticket:2' AND kind='paused' ORDER BY position DESC LIMIT 1"))
        facts = agent_run.queries.resume_facts.resume_facts(tx, row)
        assert facts["resume_id"] == "resume-gate-result" and facts["resume_outcome"] == outcome
        assert facts["resume_scope"] == "subject" and facts["resume_subject"] == "ticket:2"
        assert facts["resume_generation"] == 1 and facts["source_position"] == row["position"]
        assert facts["source_material_id"] == row["material_id"] and facts["reason_code"] == row["identity"]
        state = {"run_id": "offline-run", "repository": "example/project", "parent": {"number": 1, "title": "Parent"}, "language": "zh"}
        assert projection.project_event(tx, row, state, "detailed") == []  # Root's exact existing Gate owns this result.


def test_query_rejects_out_of_calendar_recorded_activity_with_public_diagnostic(tmp_path):
    from agent_run.queries.read_facts import QueryError
    from agent_run.queries.run import read
    from test_kernel_public_v9_facts import _kernel

    kernel, ports = _kernel(tmp_path)
    execution = kernel.claim_executor("executor")
    call = CoreOperations(kernel, execution, lambda binding: ports).once().call
    assert call is not None and kernel.dispatch_agent(call)
    process = {"pid": 21, "pgid": 21, "start_token": "synthetic-process", "host_pid": 10, "host_start_token": "host"}
    kernel.agent_started(call, process)
    kernel.observe_worker_activity(call, "outside-calendar", active=True, observed_at=1e100, evidence={"type": "turn.started"})
    with pytest.raises(QueryError) as caught:
        read(kernel.storage.path, "status")
    assert caught.value.code == "incompatible_run_state"
