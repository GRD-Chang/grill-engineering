"""Original notification results on real Core relations, without Git producers."""
from pathlib import Path

import pytest

from support.core_operations import CoreOperations
from agent_run.model import wire_json
from support.notification import _project, _kernel, _operations
from support.worker_facts import start_worker, finish_worker


def test_resume_waits_for_its_own_invocation_and_ignores_a_sibling_start(tmp_path: Path):
    kernel = _kernel(tmp_path, count=2)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"), count=2)
    original = operations.once().call
    process = start_worker(kernel, operations, original)
    finish_worker(kernel, original, process, wire_json({"result_kind": "human_blocker", "summary": None,
                                                      "human_blockers": ["Choose the original target's behavior"]}))
    assert operations.once().kind == "paused"
    sibling = operations.once().call
    assert kernel.delivery(sibling.subject).issue == 3
    assert kernel.control("resume", "resume-original-target", subject=original.subject,
                          message="Use the original decision") == "applied"
    for mode in ("detailed", "concise"):
        assert not any(event["kind"] == "resume" for event in _project(kernel, mode))
    # A sibling's actual activity cannot satisfy the original action, even
    # while both logical workers have live synthetic process observations.
    start_worker(kernel, operations, sibling)
    for mode in ("detailed", "concise"):
        assert not any(event["kind"] == "resume" for event in _project(kernel, mode))
    resumed = operations.once().call
    assert resumed.subject == original.subject
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT action_id FROM invocations WHERE id=?", (resumed.invocation_id,))[0] == "resume-original-target"
    for mode in ("detailed", "concise"):
        assert not any(event["kind"] == "resume" for event in _project(kernel, mode))
    start_worker(kernel, operations, resumed)
    for mode in ("detailed", "concise"):
        event, = [event for event in _project(kernel, mode) if event["kind"] == "resume"]
        assert event["resume_id"] == "resume-original-target" and event["resume_outcome"] == "started"
        assert event["work_subject"] == original.subject and event["generation"] == 1
        assert event["attempt_id"] == resumed.attempt_id and event["evidence"] == "worker_active"


def test_effort_keeps_closed_partial_unknown_segments_and_original_thread_config_as_of_each_result(tmp_path):
    kernel = _kernel(tmp_path)
    now = [100.0]
    kernel.clock = lambda: now[0]
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    calls = []
    for continued in (False, True):
        call = operations.once().call
        calls.append(call)
        assert kernel.dispatch_agent(call)
        operations.runner.authorize_agent(call)
        process = {"pid": 21, "pgid": 21, "start_token": call.invocation_id,
                   "host_pid": 10, "host_start_token": "synthetic-host"}
        kernel.agent_started(call, process)
        start = now[0]
        kernel.observe_worker_activity(call, "turn-1-start", active=True, observed_at=start + 1, evidence={"type": "turn.started"})
        kernel.observe_worker_activity(call, "turn-1-end", active=False, observed_at=start + (4 if continued else 3), evidence={"type": "turn.completed"})
        if not continued:
            kernel.observe_worker_activity(call, "turn-2-start", active=True, observed_at=start + 4, evidence={"type": "turn.started"})
        now[0] += 5
        raw = ({"result_kind": "development", "summary": "Original work", "human_blockers": None} if continued else
               {"result_kind": "human_blocker", "summary": None, "human_blockers": ["Original decision"]})
        kernel.receive(call, wire_json(raw))
        kernel.agent_exited(call, process, 0)
        assert operations.once().kind == ("progress" if continued else "paused")
        if not continued:
            before = next(event for event in _project(kernel, "detailed") if event["kind"] == "stage_end")
            assert before["execution_timing_complete"] is False and before["duration_seconds"] is None
            assert before["recorded_execution_seconds"] == 2
            kernel.configure_profiles("change-profile", overrides={"development_model": "new-thread-model", "development_effort": "high"})
            assert kernel.control("resume", "new-thread-resume", subject=call.subject, new_thread=True,
                                  message="Original decision supplied") == "applied"
            now[0] = 110.0
    assert calls[0].attempt_id == calls[1].attempt_id and calls[0].thread_id != calls[1].thread_id
    ended = [event for event in _project(kernel, "detailed") if event["kind"] == "stage_end"]
    assert ended[0] == before
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
    query = read(kernel.storage.path, 'history', storage=kernel.storage).data
    first_output = query["history"]["attempts"][0]["invocations"][0]["outputs"][0]
    assert first_output["elapsed_seconds"] is None and first_output["recorded_execution_seconds"] == 2


def test_query_rejects_out_of_calendar_recorded_activity_with_public_diagnostic(tmp_path):
    from agent_run.queries.read_facts import QueryError
    from agent_run.queries.run import read
    from support.cli_fixtures import _kernel

    kernel, ports = _kernel(tmp_path)
    execution = kernel.claim_executor("executor")
    call = CoreOperations(kernel, execution, lambda binding: ports).once().call
    assert call is not None and kernel.dispatch_agent(call)
    process = {"pid": 21, "pgid": 21, "start_token": "synthetic-process", "host_pid": 10, "host_start_token": "host"}
    kernel.agent_started(call, process)
    kernel.observe_worker_activity(call, "outside-calendar", active=True, observed_at=1e100, evidence={"type": "turn.started"})
    with pytest.raises(QueryError) as caught:
        read(kernel.storage.path, 'status', storage=kernel.storage)
    assert caught.value.code == "incompatible_run_state"
