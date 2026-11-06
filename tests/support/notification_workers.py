"""Original Core worker facts for notification consumers, without Git producers.

Process identity and the two local effect results are explicit test inputs.
Real Kernel methods own admission, history, receipts and state transitions;
this fixture is neither an OS witness nor proof of physical Git effects.
"""
from agent_run.core.operation_contract import Work
from agent_run.model import Commit, EffectResult, Role, wire_json


def start_worker(kernel, operations, call):
    assert kernel.dispatch_agent(call)
    operations.runner.authorize_agent(call)
    process = {"pid": 21, "pgid": 21, "start_token": call.invocation_id,
               "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    kernel.observe_worker_activity(call, "fixture-start", active=True,
        observed_at=kernel.clock(), evidence={"type": "turn.started"})
    return process


def finish_worker(kernel, call, process, raw=None):
    assert call.role == Role.DEVELOPMENT
    kernel.observe_worker_activity(call, "fixture-end", active=False,
        observed_at=kernel.clock(), evidence={"type": "turn.completed"})
    kernel.receive(call, raw or wire_json({"result_kind": "development", "summary": "Original work", "human_blockers": None}))
    kernel.agent_exited(call, process, 0)


def record_local_result(kernel, operations, subject, operation):
    """Record an explicit candidate/checkout result, never execute a Git port."""
    assert operation in {"candidate", "validation_checkout"}
    world = operations.runner.observe(subject)
    execution = operations.runner.execution
    work = Work(execution, "pending", subject, False, True, None, None)
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, subject, execution)
        if operation == "candidate":
            assert job["phase"] == "candidate"
            request = kernel._mechanical(tx, run, job, work, world)
            commit = Commit("c" * 40, "d" * 40)
        else:
            assert job["phase"] == "review"
            request = kernel._allocate(tx, run, job, work, world)
            commit = Commit(job["candidate_sha"], job["candidate_tree"])
    assert request.effect.operation == operation
    assert kernel._dispatch(request.effect, execution, world, token=request.token)
    assert kernel._reconcile(request, EffectResult("target", commit=commit)).kind == "progress"
    return request.effect, world


def consume_local_result(kernel, operations, subject, operation):
    effect, world = record_local_result(kernel, operations, subject, operation)
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, subject, effect.execution)
        row = tx.one("SELECT * FROM effects WHERE run_id=? AND id=?", (effect.execution.run_id, effect.effect_id))
        assert kernel._apply_effect(tx, run, job, row, world).kind == "progress"
    return effect
