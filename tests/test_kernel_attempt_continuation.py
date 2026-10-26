"""A semantic continuation owns its input and binding before native execution."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from agent_run.model import Step
from agent_run.core.kernel import Kernel
from agent_run.core import kernel as core_bindings
from agent_run.errors import KernelError
from agent_run.core.attempt_continuation import load_continuation, validate_native_opening
from agent_run.model import wire_json
from agent_run.model import Issue, ParentIssue, Requirements
from support.offline import GraphScenario
from support.core_operations import CoreOperations
from agent_run.storage.preflight import PROTOCOL
from support.agent_continuation import _native_spec, _read_operations, _native_boundary


def test_human_resume_uses_attempt_input_and_binding_without_charging_again(tmp_path: Path, monkeypatch) -> None:
    bind = core_bindings.bind_attempt_continuation
    bound = []

    def retry_native_binding(tx, attempt_id, basis):
        bind(tx, attempt_id, basis)
        bind(tx, attempt_id, basis)  # Exact native retry, before Invocation allocation.
        bound.append(attempt_id)

    monkeypatch.setattr(core_bindings, "bind_attempt_continuation", retry_native_binding)
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)), (
        Issue(2, "Ticket", "Implement the original requirement", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "scenario", source)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
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
    assert bound == [first.attempt_id]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT protocol FROM meta")[0] == PROTOCOL == "independent-delivery-kernel-v21"
        references = {row["from"]: (row["table"], row["to"])
                      for row in tx.execute("PRAGMA foreign_key_list(attempts)")}
        assert references["thread_id"] == ("threads", "id")
        assert references["opening_id"] == ("material", "id")
        assert tx.execute("PRAGMA foreign_key_check").fetchone() is None
        facts = load_continuation(tx, first.attempt_id)
        attempt = tx.one("SELECT thread_id,opening_id,context_id FROM attempts WHERE id=?", (first.attempt_id,))
        assert tx.get(attempt["opening_id"], first.subject) == wire_json({
            "provenance_id": None, "ordinary_used": 0, "capacity_count": 0, "prior": None})
    assert facts.thread_id == attempt["thread_id"] == second.thread_id
    assert facts.context["human_responses"] == ["Keep it.\r\n"]
    assert facts.context["prior_human_blockers"] == ["Choose the original formatting."]
    assert facts.context["base_sha"] == first.context["base_sha"]
    assert facts.ordinary_used == 0 and facts.capacity_count == 0
    assert attempt["opening_id"] is not None
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1








@pytest.mark.parametrize('has_raw', [False, True])
def test_native_invocation_with_unproven_exit_cannot_consume_or_dispatch(tmp_path: Path, has_raw: bool) -> None:
    raw = wire_json({'result_kind': 'development', 'summary': 'Durable before exit.', 'human_blockers': None}) if has_raw else None
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path, exit_proven=False, return_body=raw)
    step = operations.once()
    # The shared selector already sees the native Invocation exit barrier; it need not
    # enter a private subject transition to rediscover the same waiting reason.
    assert isinstance(step, Step)
    assert (step.kind, step.call) == ('waiting', None)
    state = operations.runner.state()
    assert (state.disposition, state.active, state.wake_at) == ('waiting', (previous.lease_id,), previous.deadline_at)
    assert kernel.status('ticket:2')['phase'] == 'development'
    assert kernel.status('ticket:2')['pending_return'] is has_raw
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM effects')
        assert tx.one('SELECT count(*) FROM invocations')[0] == 1
        assert tx.one('SELECT count(*) FROM leases')[0] == 1
        prior = load_continuation(tx, attempt_id).prior
        assert prior is not None and prior.dispatch_state == 'started'
        assert prior.invocation_id == previous.invocation_id
        assert (prior.raw_id is not None) is has_raw


def test_native_explicit_continuation_does_not_refund_ordinary_recovery(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path, ordinary_used=True)
    assert operations.once().reason == 'execution_recovery_unavailable'
    kernel.control('resume', 'continue-exact-attempt', subject='ticket:2')
    call = operations.once().call
    assert call is not None and call.attempt_id == attempt_id and call.mode == 'resume'
    with kernel.storage.transaction(write=False) as tx:
        invocation = tx.one('SELECT * FROM invocations WHERE id=?', (call.invocation_id,))
        facts = load_continuation(tx, attempt_id)
    assert invocation['predecessor'] == previous.invocation_id and invocation['recovery_kind'] == 'explicit'
    assert facts.ordinary_used == 1
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


def test_first_native_capacity_continuation_preserves_existing_not_before(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path, failure_kind='capacity')
    kernel.clock = lambda: 129.0
    waiting = operations.once()
    assert isinstance(waiting, Step)
    assert (waiting.kind, waiting.retry_at, waiting.call) == ('waiting', 130.0, None)
    assert operations.runner.state().wake_at == 130.0
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM effects')
        assert tx.one('SELECT count(*) FROM invocations')[0] == 1
    kernel.clock = lambda: 130.0
    call = operations.once().call
    assert call is not None and call.attempt_id == attempt_id and call.deadline_at == 137.0
    with kernel.storage.transaction(write=False) as tx:
        assert load_continuation(tx, attempt_id).capacity_count == 1


def test_format_period_crossing_executor_exit_uses_a_real_new_invocation(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path, malformed=True)
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
    assert isinstance(waiting, Step)
    assert (waiting.kind, waiting.call) == ('waiting', None)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM effects')
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
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path, malformed=True)
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
        assert tx.one('SELECT predecessor FROM invocations WHERE id=?', (call.invocation_id,))[0] == (previous.invocation_id if expired else None)


def test_concurrent_first_native_allocation_has_one_lease_and_stop_only_settles_raw(tmp_path: Path) -> None:
    kernel, execution, operations, attempt_id, previous = _native_boundary(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        steps = list(pool.map(lambda _: operations.once(), range(2)))
    calls = [step.call for step in steps if step.kind == 'agent']
    assert len(calls) == 1 and calls[0] is not None
    call = calls[0]
    assert call.attempt_id == attempt_id
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 2
        assert tx.one('SELECT count(*) FROM leases')[0] == 1
    kernel.control('stop', 'stop-new-native')
    kernel.receive(call, wire_json({'result_kind': 'development', 'summary': 'Durable late result.', 'human_blockers': None}))
    before = kernel.status('ticket:2')
    stopped = operations.once()
    assert isinstance(stopped, Step)
    assert (stopped.kind, stopped.reason, stopped.call) == ('paused', 'executor_fenced', None)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM effects')
    assert kernel.status('ticket:2') == before
    assert kernel.status('ticket:2')['pending_return'] is True
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


@pytest.mark.parametrize("changed", [
    {"provenance_id": "certified-source"}, {"ordinary_used": 1}, {"capacity_count": 1},
    {"prior": {}}, {"ordinary_used": False}, {"capacity_count": 0.0}, {"extra": "ignored"},
])
def test_non_native_opening_is_rejected_without_installation(changed) -> None:
    body = wire_json({"provenance_id": None, "ordinary_used": 0, "capacity_count": 0, "prior": None} | changed)
    with pytest.raises(KernelError, match="continuation_opening"):
        validate_native_opening(body)


def test_opening_does_not_discard_duplicate_imported_fields() -> None:
    body = b'{"capacity_count":0,"ordinary_used":0,"prior":{},"prior":null,"provenance_id":null}'
    with pytest.raises(KernelError, match="continuation_opening"):
        validate_native_opening(body)
