"""A semantic continuation owns its input and binding before native execution."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest
from agent_run_kernel import Commit, Kernel, KernelError, World
from agent_run_kernel.attempt_continuation import (
    AttemptContinuationBasis, ContinuationOpening, bind_attempt_continuation, load_continuation,
)
from agent_run_kernel.model import wire_json
from test_kernel_exploration import ticket_spec


def test_human_resume_uses_attempt_input_and_binding_without_charging_again(tmp_path: Path) -> None:
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", ticket_spec())
    execution = kernel.claim_executor("executor")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    first = kernel.advance("ticket:2", execution, world).call
    assert first is not None
    kernel.bind_thread(first, "backend-first")
    kernel.receive(first, wire_json({"result_kind": "human_blocker", "summary": None,
        "human_blockers": ["Choose the original formatting."]}))
    assert kernel.advance("ticket:2", execution, world).reason == "human_blocker"
    kernel.control("resume", "answer", subject="ticket:2", message="Keep it.\r\n", new_thread=True)
    second = kernel.advance("ticket:2", execution, world).call
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


def _canonical_boundary(tmp_path: Path, *, ordinary_used: bool = False, malformed: bool = False,
                        failure_kind: str = 'ordinary', exit_proven: bool = True, return_body: bytes | None = None):
    """Install actual native-produced facts in an empty unpublished Core DB.

    Only immutable materials and semantic identity cross the seam. No source
    Invocation, lease, action, gate, or prepared database is manufactured.
    """
    spec = ticket_spec()
    spec = replace(spec, policy=replace(spec.policy, development_deadline_seconds=7))
    source = Kernel.create(tmp_path / "source.sqlite3", spec)
    source.clock = lambda: 100.0
    owner = source.claim_executor("source-executor")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    call = source.advance("ticket:2", owner, world).call
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
            call = source.advance("ticket:2", owner, world).call
            assert call is not None
    with source.storage.transaction(write=False) as tx:
        attempt = dict(tx.one("SELECT * FROM attempts WHERE id=?", (call.attempt_id,)))
        thread = dict(tx.one("SELECT * FROM threads WHERE id=?", (call.thread_id,)))
        materials = [dict(row) for row in tx.all("SELECT * FROM material")]
        facts = load_continuation(tx, call.attempt_id)
        budget = tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements'")
    target = Kernel.create(tmp_path / "target.sqlite3", spec)
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
    return target, target.claim_executor('target-executor'), world, call.attempt_id


@pytest.mark.parametrize('has_raw', [False, True])
def test_opening_with_unproven_exit_cannot_consume_or_dispatch(tmp_path: Path, has_raw: bool) -> None:
    raw = wire_json({'result_kind': 'development', 'summary': 'Durable before exit.', 'human_blockers': None}) if has_raw else None
    kernel, execution, world, _ = _canonical_boundary(tmp_path, exit_proven=False, return_body=raw)
    step = kernel.advance('ticket:2', execution, world)
    assert (step.kind, step.reason, step.call, step.effect) == ('waiting', 'previous_invocation_exit', None, None)
    assert kernel.status('ticket:2')['phase'] == 'development'
    assert kernel.status('ticket:2')['pending_return'] is has_raw
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0


def test_first_native_explicit_continuation_does_not_refund_opening_recovery(tmp_path: Path) -> None:
    kernel, execution, world, attempt_id = _canonical_boundary(tmp_path, ordinary_used=True)
    assert kernel.advance('ticket:2', execution, world).reason == 'execution_recovery_unavailable'
    kernel.control('resume', 'continue-exact-attempt', subject='ticket:2')
    call = kernel.advance('ticket:2', execution, world).call
    assert call is not None and call.attempt_id == attempt_id and call.mode == 'resume'
    with kernel.storage.transaction(write=False) as tx:
        invocation = tx.one('SELECT * FROM invocations')
        facts = load_continuation(tx, attempt_id)
    assert invocation['predecessor'] is None and invocation['recovery_kind'] == 'explicit'
    assert facts.ordinary_used == 1
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


def test_first_native_capacity_continuation_preserves_existing_not_before(tmp_path: Path) -> None:
    kernel, execution, world, attempt_id = _canonical_boundary(tmp_path, failure_kind='capacity')
    kernel.clock = lambda: 129.0
    assert kernel.advance('ticket:2', execution, world).reason == 'capacity_recovery_wait'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    kernel.clock = lambda: 130.0
    call = kernel.advance('ticket:2', execution, world).call
    assert call is not None and call.attempt_id == attempt_id and call.deadline_at == 137.0
    with kernel.storage.transaction(write=False) as tx:
        assert load_continuation(tx, attempt_id).capacity_count == 1


def test_format_period_crossing_executor_exit_uses_a_real_new_invocation(tmp_path: Path) -> None:
    kernel, execution, world, attempt_id = _canonical_boundary(tmp_path, malformed=True)
    first = kernel.advance('ticket:2', execution, world).call
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
    call = kernel.advance('ticket:2', replacement, world).call
    assert call is not None and call.attempt_id == attempt_id
    assert call.invocation_id != first.invocation_id
    assert call.deadline_at == 107.0 and call.output_ordinal == 2
    assert kernel.dispatch_agent(call)


@pytest.mark.parametrize('expired', [False, True])
def test_first_native_format_step_keeps_its_period_unless_explicitly_resumed(tmp_path: Path, expired: bool) -> None:
    kernel, execution, world, attempt_id = _canonical_boundary(tmp_path, malformed=True)
    kernel.clock = lambda: 108.0 if expired else 106.0
    if expired:
        assert kernel.advance('ticket:2', execution, world).reason == 'invocation_deadline_exceeded'
        kernel.control('resume', 'new-physical-period', subject='ticket:2')
    call = kernel.advance('ticket:2', execution, world).call
    assert call is not None and call.attempt_id == attempt_id
    assert call.output_ordinal == 1 and call.mode == 'output-repair'
    assert call.deadline_at == (115.0 if expired else 107.0)
    assert call.context['readonly'] is True and call.context['contract_error']
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT predecessor FROM invocations')[0] is None


def test_concurrent_first_native_allocation_has_one_lease_and_stop_only_settles_raw(tmp_path: Path) -> None:
    kernel, execution, world, attempt_id = _canonical_boundary(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        steps = list(pool.map(lambda _: kernel.advance('ticket:2', execution, world), range(2)))
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
        kernel.advance('ticket:2', execution, world)
    assert kernel.status('ticket:2')['pending_return'] is True
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1
