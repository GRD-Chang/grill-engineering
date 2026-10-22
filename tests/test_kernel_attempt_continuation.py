"""A semantic continuation owns its input and binding before native execution."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError
from agent_run_kernel.attempt_continuation import (
    AttemptContinuationBasis, ContinuationOpening, bind_attempt_continuation, load_continuation,
)
from agent_run_kernel.model import wire_json
from agent_run_kernel.model import Issue, ParentIssue, Requirements
from agent_run_kernel.offline import GraphScenario, fixture_inputs
from support.core_operations import CoreOperations, ReadOnlyPorts
from agent_run_kernel.operation_runner import DeliveryFacts
from test_kernel_exploration import ticket_spec
from test_kernel_operation_runner import Ports


def test_human_resume_uses_attempt_input_and_binding_without_charging_again(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)), (
        Issue(2, "Ticket", "Implement the original requirement", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "scenario", source)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, lambda binding: Ports(scenario))
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None
    assert first.subject == "ticket:2" and first.role == "development"
    assert kernel.dispatch_agent(first)
    operations.runner.authorize_agent(first)
    kernel.bind_thread(first, "backend-first")
    process = {"pid": 23, "pgid": 23, "start_token": "first", "host_pid": 11, "host_start_token": "host"}
    kernel.agent_started(first, process)
    kernel.receive(first, wire_json({"result_kind": "human_blocker", "summary": None,
        "human_blockers": ["Choose the original formatting."]}))
    assert not operations.runner.worker_settled(first)
    kernel.agent_exited(first, process, 0)
    assert operations.runner.worker_settled(first)
    assert operations.once().reason == "human_blocker"
    kernel.control("resume", "answer", subject="ticket:2", message="Keep it.\r\n", new_thread=True)
    second = operations.until(lambda step: step.call is not None).call
    assert second is not None and second.attempt_id == first.attempt_id
    with kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, first.attempt_id)
        attempt = tx.one("SELECT thread_id,opening_id,context_id FROM attempts WHERE id=?", (first.attempt_id,))
    assert facts.thread_id == attempt["thread_id"] == second.thread_id
    assert facts.context["human_responses"] == ["Keep it.\r\n"]
    assert facts.context["prior_human_blockers"] == ["Choose the original formatting."]
    assert facts.context["base_sha"] == first.context["base_sha"]
    assert facts.ordinary_used == 0 and facts.capacity_count == 0
    assert attempt["opening_id"] is not None
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


def _native_spec(root: Path):
    """Explicit creation inputs and source facts, with no physical write adapter."""
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)), (
        Issue(2, "Ticket", "Implement original requirement", "OPEN", frozenset({"ready-for-agent"})),
    ))
    spec = ticket_spec()
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unread', spec.deliveries[0].base,
                     'refs/heads/run', 'refs/heads/main')
    spec = replace(spec, language='zh', repository_root=str(root.resolve()), inputs=fixture_inputs(),
                   deliveries=(*spec.deliveries, final))
    return spec, source


def _read_operations(kernel, execution, source):
    ports = ReadOnlyPorts(source, 'refs/heads/ticket-2', 'refs/heads/run',
                          DeliveryFacts(Commit('base', 'base-tree'), None))
    return CoreOperations(kernel, execution, lambda binding: ports)


def _canonical_boundary(tmp_path: Path, *, ordinary_used: bool = False, malformed: bool = False,
                        failure_kind: str = 'ordinary', exit_proven: bool = True, return_body: bytes | None = None):
    """Install actual native-produced facts in an empty unpublished Core DB.

    Only immutable materials and semantic identity cross the seam. No source
    Invocation, lease, action, gate, or prepared database is manufactured.
    """
    spec, requirements = _native_spec(tmp_path)
    spec = replace(spec, policy=replace(spec.policy, development_deadline_seconds=7))
    source = Kernel.create(tmp_path / "source.sqlite3", spec, requirements=requirements)
    source.clock = lambda: 100.0
    owner = source.claim_executor("source-executor")
    source_operations = _read_operations(source, owner, requirements)
    call = source_operations.once().call
    assert call is not None
    source.bind_thread(call, "retained-backend")
    for index in range(2 if ordinary_used else 1):
        assert source.dispatch_agent(call)
        process = {"pid": 23 + index, "start_token": "start-" + str(index), "pgid": 23 + index,
                   "host_pid": 11, "host_start_token": "host-start"}
        source.agent_started(call, process)
        body = b'{"summary":"incomplete return"}' if malformed else return_body
        if body is not None:
            source.receive(call, body)
        if exit_proven:
            source.agent_exited(call, process, 0 if body is not None else 1, failure_kind=failure_kind)
        if index == 0 and ordinary_used:
            call = source_operations.once().call
            assert call is not None
    with source.storage.transaction(write=False) as tx:
        attempt = dict(tx.one("SELECT * FROM attempts WHERE id=?", (call.attempt_id,)))
        thread = dict(tx.one("SELECT * FROM threads WHERE id=?", (call.thread_id,)))
        materials = [dict(row) for row in tx.all("SELECT * FROM material")]
        facts = load_continuation(tx, call.attempt_id)
        budget = tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements'")
    target = Kernel.create(tmp_path / "target.sqlite3", spec, requirements=requirements)
    target.clock = lambda: 100.0
    with target.storage.transaction() as tx:
        for material in materials:
            assert tx.put(material['subject'], material['purpose'], material['body']) == material['id']
        tx.execute("INSERT INTO threads(id,subject,role,profile_revision,backend_id) VALUES (?,?,?,?,?)",
                   tuple(thread[key] for key in ('id', 'subject', 'role', 'profile_revision', 'backend_id')))
        # Initial installation of a semantic Attempt, never rewriting an old DB.
        attempt.update(current_invocation=None, thread_id=None, opening_id=None)
        tx.execute("INSERT INTO attempts (" + ','.join(attempt) + ") VALUES (" + ','.join('?' for _ in attempt) + ")", tuple(attempt.values()))
        tx.execute("UPDATE subjects SET current_attempt=? WHERE id='ticket:2'", (call.attempt_id,))
        tx.execute("UPDATE budgets SET developments_used=?,reviews_used=? WHERE subject='ticket:2' AND purpose='requirements'",
                   tuple(budget))
        provenance = tx.put('ticket:2', 'certified_continuation', wire_json({
            'attempt': call.attempt_id, **{key: attempt[key] for key in ('subject', 'generation', 'role', 'purpose', 'window')},
            'source_execution': call.invocation_id}))
        assert facts.prior is not None
        prior = replace(facts.prior, invocation_id=None, lease_id=None)
        if not exit_proven:
            prior = replace(prior, status='interrupted', dispatch_state='unknown')
        opening = ContinuationOpening(provenance, facts.ordinary_used, facts.capacity_count, prior)
        basis = AttemptContinuationBasis(call.thread_id, attempt['context_id'], opening)
        bind_attempt_continuation(tx, call.attempt_id, basis)
        bind_attempt_continuation(tx, call.attempt_id, basis)  # Exact retry is idempotent.
    execution = target.claim_executor('target-executor')
    return target, execution, _read_operations(target, execution, requirements), call.attempt_id


@pytest.mark.parametrize('has_raw', [False, True])
def test_opening_with_unproven_exit_cannot_consume_or_dispatch(tmp_path: Path, has_raw: bool) -> None:
    raw = wire_json({'result_kind': 'development', 'summary': 'Durable before exit.', 'human_blockers': None}) if has_raw else None
    kernel, execution, operations, _ = _canonical_boundary(tmp_path, exit_proven=False, return_body=raw)
    step = operations.once()
    assert (step.kind, step.reason, step.call, step.effect) == ('waiting', 'previous_invocation_exit', None, None)
    assert kernel.status('ticket:2')['phase'] == 'development'
    assert kernel.status('ticket:2')['pending_return'] is has_raw
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0


def test_first_native_explicit_continuation_does_not_refund_opening_recovery(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id = _canonical_boundary(tmp_path, ordinary_used=True)
    assert operations.once().reason == 'execution_recovery_unavailable'
    kernel.control('resume', 'continue-exact-attempt', subject='ticket:2')
    call = operations.once().call
    assert call is not None and call.attempt_id == attempt_id and call.mode == 'resume'
    with kernel.storage.transaction(write=False) as tx:
        invocation = tx.one('SELECT * FROM invocations')
        facts = load_continuation(tx, attempt_id)
    assert invocation['predecessor'] is None and invocation['recovery_kind'] == 'explicit'
    assert facts.ordinary_used == 1
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


def test_first_native_capacity_continuation_preserves_existing_not_before(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id = _canonical_boundary(tmp_path, failure_kind='capacity')
    kernel.clock = lambda: 129.0
    assert operations.once().reason == 'capacity_recovery_wait'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    kernel.clock = lambda: 130.0
    call = operations.once().call
    assert call is not None and call.attempt_id == attempt_id and call.deadline_at == 137.0
    with kernel.storage.transaction(write=False) as tx:
        assert load_continuation(tx, attempt_id).capacity_count == 1


def test_format_period_crossing_executor_exit_uses_a_real_new_invocation(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id = _canonical_boundary(tmp_path, malformed=True)
    first = operations.once().call
    assert first is not None
    assert kernel.dispatch_agent(first)
    process = {'pid': 80, 'pgid': 80, 'start_token': 'format-start', 'host_pid': 11, 'host_start_token': 'host-start'}
    kernel.agent_started(first, process)
    kernel.receive(first, b'{"summary":"still incomplete"}')
    kernel.agent_exited(first, process, 0)
    kernel.control('stop', 'replace-executor')
    kernel.confirm_executor_exit(execution)
    kernel.control('resume', 'resume-executor')
    replacement = kernel.claim_executor('replacement-executor')
    kernel.clock = lambda: 106.0
    call = CoreOperations(kernel, replacement, lambda binding: operations.runner.ports).once().call
    assert call is not None and call.attempt_id == attempt_id
    assert call.invocation_id != first.invocation_id
    assert call.deadline_at == 107.0 and call.output_ordinal == 2
    assert kernel.dispatch_agent(call)


def test_local_resume_new_thread_keeps_format_period_and_old_invocation_binding(tmp_path: Path) -> None:
    spec, requirements = _native_spec(tmp_path)
    spec = replace(spec, policy=replace(spec.policy, development_deadline_seconds=7))
    kernel = Kernel.create(tmp_path / 'native.sqlite3', spec, requirements=requirements)
    now = [100.0]
    kernel.clock = lambda: now[0]
    execution = kernel.claim_executor('executor')
    operations = _read_operations(kernel, execution, requirements)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, 'original-backend')
    process = {'pid': 90, 'pgid': 90, 'start_token': 'original-start', 'host_pid': 11, 'host_start_token': 'host-start'}
    kernel.agent_started(first, process)
    kernel.receive(first, b'{"summary":"format incomplete"}')
    kernel.agent_exited(first, process, 0)
    kernel.control('stop', 'local-stop', subject='ticket:2')
    kernel.control('resume', 'local-new-thread', subject='ticket:2', new_thread=True)
    now[0] = 106.0
    call = operations.once().call
    assert call is not None and call.thread_id != first.thread_id
    assert kernel.dispatch_agent(call)
    assert call.invocation_id != first.invocation_id and call.attempt_id == first.attempt_id
    assert call.deadline_at == first.deadline_at == 107.0 and call.output_ordinal == 1
    assert call.context['readonly'] is True and call.context['contract_error']
    with kernel.storage.transaction(write=False) as tx:
        old = tx.one('SELECT thread,status FROM invocations WHERE id=?', (first.invocation_id,))
        current = tx.one('SELECT thread,predecessor,action_id FROM invocations WHERE id=?', (call.invocation_id,))
    assert old['thread'] == first.thread_id and old['status'] == 'returned'
    assert tuple(current) == (call.thread_id, first.invocation_id, 'local-new-thread')
    waiting = operations.once()
    # Global selection waits on the durable lease rather than advancing this
    # selected subject merely to rediscover its private agent_running reason.
    assert (waiting.kind, waiting.call, waiting.effect) == ('waiting', None, None)
    assert operations.runner.state().active == (call.lease_id,)
    kernel.bind_thread(call, 'replacement-backend')
    next_process = {**process, 'pid': 91, 'pgid': 91, 'start_token': 'replacement-start'}
    kernel.agent_started(call, next_process)
    kernel.receive(call, wire_json({'result_kind': 'development', 'summary': 'Retained output.', 'human_blockers': None}))
    kernel.agent_exited(call, next_process, 0)
    assert operations.once().kind == 'progress'
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


@pytest.mark.parametrize('expired', [False, True])
def test_first_native_format_step_keeps_its_period_unless_explicitly_resumed(tmp_path: Path, expired: bool) -> None:
    kernel, execution, operations, attempt_id = _canonical_boundary(tmp_path, malformed=True)
    kernel.clock = lambda: 108.0 if expired else 106.0
    if expired:
        assert operations.once().reason == 'invocation_deadline_exceeded'
        kernel.control('resume', 'new-physical-period', subject='ticket:2')
    call = operations.once().call
    assert call is not None and call.attempt_id == attempt_id
    assert call.output_ordinal == 1 and call.mode == 'output-repair'
    assert call.deadline_at == (115.0 if expired else 107.0)
    assert call.context['readonly'] is True and call.context['contract_error']
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT predecessor FROM invocations')[0] is None


def test_concurrent_first_native_allocation_has_one_lease_and_stop_only_settles_raw(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id = _canonical_boundary(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        steps = list(pool.map(lambda _: operations.once(), range(2)))
    calls = [step.call for step in steps if step.kind == 'agent']
    assert len(calls) == 1 and calls[0] is not None
    call = calls[0]
    assert call.attempt_id == attempt_id
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 1
        assert tx.one('SELECT count(*) FROM leases')[0] == 1
    kernel.control('stop', 'stop-new-native')
    kernel.receive(call, wire_json({'result_kind': 'development', 'summary': 'Durable late result.', 'human_blockers': None}))
    with pytest.raises(KernelError, match='executor_fenced'):
        operations.once()
    assert kernel.status('ticket:2')['pending_return'] is True
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1
