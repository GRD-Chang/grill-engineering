"""Original Core worker and local-effect facts, without Git producers.

Process identity and local effect results are explicit test inputs.
Real Kernel methods own admission, history, receipts and state transitions;
this fixture is neither an OS witness nor proof of physical Git effects.
"""
from agent_run.core.operation_contract import Work
from agent_run.model import Commit, Delivery, DeliveryKind, EffectResult, Policy, Role, RunSpec, wire_json
from support.core_operations import CoreOperations, native_agent_fixture


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
    assert raw is not None or call.role == Role.DEVELOPMENT
    kernel.observe_worker_activity(call, "fixture-end", active=False,
        observed_at=kernel.clock(), evidence={"type": "turn.completed"})
    kernel.receive(call, raw if raw is not None else wire_json({"result_kind": "development", "summary": "Original work", "human_blockers": None}))
    kernel.agent_exited(call, process, 0)


def record_local_result(kernel, operations, subject, operation):
    """Record one explicit local result; production Core prepares its original intent."""
    assert operation in {"candidate", "validation_checkout", "validation_cleanup", "integrate"}
    world = operations.runner.observe(subject)
    execution = operations.runner.execution
    work = Work(execution, "pending", subject, False, True, None, None)
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, subject, execution)
        if operation == "candidate":
            assert job["phase"] == "candidate"
            request = kernel._mechanical(tx, run, job, work, world)
            commit = Commit("c" * 40, "d" * 40)
        elif operation == "validation_checkout":
            assert job["phase"] == "review"
            request = kernel._allocate(tx, run, job, work, world)
            commit = Commit(job["candidate_sha"], job["candidate_tree"])
        elif operation == "validation_cleanup":
            assert job["validation_cleanup"]
            request = kernel._advance(tx, work, world)
            commit = None
        else:
            assert job["phase"] == "integration"
            request = kernel._mechanical(tx, run, job, work, world)
            commit = Commit(job["candidate_sha"], job["candidate_tree"])
    assert request.effect.operation == operation
    assert kernel._dispatch(request.effect, execution, world, token=request.token)
    assert kernel._reconcile(request, EffectResult("target", commit=commit,
        expected_tree=commit.tree if operation == "integrate" else None)).kind == "progress"
    return request.effect, world


def consume_local_result(kernel, operations, subject, operation):
    effect, world = record_local_result(kernel, operations, subject, operation)
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, subject, effect.execution)
        row = tx.one("SELECT * FROM effects WHERE run_id=? AND id=?", (effect.execution.run_id, effect.effect_id))
        assert kernel._apply_effect(tx, run, job, row, world).kind == "progress"
    return effect


def worker_fixture(root, *, kind=DeliveryKind.TICKET, language="en", policy=None):
    """Fresh native Core and read-only external facts; no physical Git claim."""
    checkout = root / "work"
    checkout.mkdir(parents=True)
    issue = 2 if kind == DeliveryKind.TICKET else 1
    delivery = Delivery(f"{kind.value}:{issue}", kind, issue, "original", Commit("a" * 40, "b" * 40),
        "refs/heads/work", "refs/heads/target", checkout=str(checkout))
    kernel, ports = native_agent_fixture(root / "state" / "kernel.sqlite3", RunSpec(
        "worker-facts", "example/repository", 1, "parent", "graph", (delivery,),
        policy or Policy(ticket_rounds=2), language=language))
    kernel.clock = lambda: 1000.
    operations = CoreOperations(kernel, kernel.claim_executor("worker-consumer"), lambda _: ports,
                                prepare_agents=False)
    return kernel, operations
