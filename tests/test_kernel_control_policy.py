"""Explicit checkpoint policy and stopped-Run feedback keep their real owners."""

from pathlib import Path
from dataclasses import replace
import json

import pytest

from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import EffectResult, Execution, HostLaunch, HostObservation, wire_json
from support.core_operations import CoreOperations, ReadOnlyPorts, native_agent_fixture


def managed(kernel: Kernel, root: Path) -> tuple[HostLaunch, Execution]:
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=root / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    return request, Execution(request.run_id, request.owner, request.epoch)


def tickets(root: Path, *, new_thread_per_development: bool = False) -> tuple[Kernel, ReadOnlyPorts]:
    base = Commit("base", "tree")
    jobs = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "v1", base,
                          f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    kernel, ports = native_agent_fixture(root / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", jobs,
                           Policy(ticket_rounds=1, concurrency=2, development_deadline_seconds=7.0,
                                  new_thread_per_development=new_thread_per_development),
                           language="en", repository_root=str(root / "repository")))
    class TicketPorts(ReadOnlyPorts):
        def delivery(self, request):
            assert request.source_ref in {item.source_ref for item in jobs}
            assert request.target_ref == 'refs/heads/run'
            return replace(self.facts, checks=self.facts.checks if request.read_checks else None,
                           checks_requested=request.read_checks)

        def execute(self, effect, authorize):
            assert effect.operation == 'candidate'
            authorize.prepare()()
            return EffectResult('failure', base, raw={'head': 'base', 'reason': 'candidate_integrity'})

    return kernel, TicketPorts(ports.requirements, ports.source_ref, ports.target_ref, ports.facts)


def test_ticket_policy_window_does_not_change_a_sibling_call_or_its_allowances(tmp_path: Path) -> None:
    # #229 freezes rotation at creation; Resume changes only the legal window.
    kernel, ports = tickets(tmp_path, new_thread_per_development=True)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    previous = None
    sibling = None
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            if step.call.subject == "ticket:3":
                assert sibling is None
                sibling = step.call  # Keep the real FIFO lease; do not run or settle it.
            else:
                assert step.call.subject == "ticket:2"
                previous = step.call
                kernel.bind_thread(previous, previous.reported_thread or "ticket-two-thread-" + previous.thread_id)
                kernel.receive(previous, b'{"result_kind":"development","summary":"Investigated candidate integrity","human_blockers":null}')
        if "modification_budget_exhausted" in kernel.status("ticket:2")["gates"]:
            break
    else:
        raise AssertionError("The actual candidate failures did not exhaust Ticket 2")
    assert "modification_budget_exhausted" in kernel.status("ticket:2")["gates"]
    if sibling is None:
        sibling = operations.once().call
    assert sibling is not None and sibling.subject == "ticket:3" and sibling.deadline_seconds == 7.0
    sibling_budget = kernel.status("ticket:3")["budgets"]
    with kernel.storage.transaction(write=False) as tx:
        original_policy = tx.one("SELECT policy FROM runs")[0]
    assert kernel.control_host(request, "resume", "ticket-window", subject="ticket:2", policy_overrides={
        "ticket_review_rounds": 3, "development_deadline": "13s",
    }) == "applied"
    resumed = operations.once().call
    assert resumed is not None and resumed.deadline_seconds == 13.0
    assert previous is not None and resumed.attempt_id != previous.attempt_id and resumed.thread_id != previous.thread_id
    # Release this Worker in the fixture's controlled Host domain; keep its sibling idle.
    operations.runner.authorize_agent(resumed)
    assert kernel.dispatch_agent(resumed)
    kernel.agent_started(resumed, {"pid": 23, "pgid": 23, "start_token": "resumed-ticket-two",
        "host_pid": 42, "host_start_token": "start", "boot_id": "boot",
        "host_cgroup": "/host", "cgroup": "/host/worker"})
    kernel.agent_source_ready(resumed)
    assert kernel.status("ticket:3")["budgets"] == sibling_budget
    assert kernel.dispatch_agent(sibling)
    kernel.guard_agent(sibling)
    budget = kernel.status("ticket:2")["budgets"]["requirements"]
    assert (budget["window"], budget["development_limit"], budget["review_limit"], budget["developments_used"]) == (2, 4, 3, 1)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT policy FROM runs")[0] == original_policy
        old = tx.one("SELECT * FROM budgets WHERE subject='ticket:2' AND purpose='requirements' AND window=1")
        assert (old["developments_used"], old["development_limit"], old["review_limit"], old["current"]) == (2, 2, 1, 0)


def test_ordinary_resume_cannot_change_policy_or_consume_its_action(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.agent_unavailable(call, "worker_preparation_failed", "No child process was created")
    before = kernel.status("ticket:2")
    with pytest.raises(KernelError, match="policy_override_boundary"):
        kernel.control_host(request, "resume", "ordinary", subject="ticket:2", policy_overrides={"development_deadline": "13s"})
    assert kernel.status("ticket:2") == before
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE id='ordinary'")
    assert kernel.control_host(request, "resume", "ordinary", subject="ticket:2", message="Fixed the worker setup") == "applied"
    continued = operations.once().call
    assert continued is not None and continued.attempt_id == call.attempt_id and continued.deadline_seconds == 7.0


def test_whole_run_resume_forwards_feedback_only_to_the_stopped_work(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    execution = kernel.claim_executor("original-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    blocked = operations.once().call
    assert blocked is not None
    kernel.receive(blocked, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["An independent product decision"]}))
    assert operations.once(agent_capacity=0).kind == "paused"
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, "original-thread")
    process = {"pid": 23, "start_token": "original-start", "pgid": 23, "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(first, process)
    # This work actually passed final source admission before the operator Stop.
    kernel.agent_source_ready(first)
    kernel.control("stop", "whole-stop")
    kernel.agent_exited(first, process, 1, failure="Stopped by the operator")
    kernel.confirm_executor_exit(execution)
    message = "已检查停止原因；继续原工作。\r\n"
    request = kernel.admit_host("whole-resume", kind="resume", command=("native-host",), environment_carrier=tmp_path / "carrier",
                                preflight=lambda: None, message=message, new_thread=True)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "resumed", "/host", "boot", 42, "start",
                                                       usage_ready=True)) == "running"
    resumed_execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, resumed_execution, lambda binding: ports)
    assert kernel.status("ticket:2")["pending_attempt"] == blocked.attempt_id
    assert kernel.status("ticket:2")["gates"]
    resumed = operations.once().call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.thread_id != first.thread_id and resumed.context["human_responses"] == [message]
    assert resumed.context["semantic_continuation"] is True and resumed.deadline_seconds == 7.0
    assert kernel.status("ticket:3")["budgets"]["requirements"]["developments_used"] == 1
    assert not [item for item in kernel.history("ticket:2") if item["kind"] == "human_response"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT resume_action FROM attempts WHERE id=?", (blocked.attempt_id,))[0] is None


def test_local_human_resume_cannot_take_pending_whole_run_feedback(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    execution = kernel.claim_executor("original-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    blocked = operations.once().call
    assert blocked is not None
    kernel.receive(blocked, wire_json({"result_kind": "human_blocker", "summary": None,
                                      "human_blockers": ["An independent product decision"]}))
    assert operations.once(agent_capacity=0).kind == "paused"
    first = operations.once().call
    assert first is not None
    kernel.control("stop", "whole-stop")
    kernel.confirm_executor_exit(execution, (first.invocation_id,))
    request = kernel.admit_host("whole-resume", kind="resume", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                message="Continue the stopped work", new_thread=True)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "resumed", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    resumed_execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, resumed_execution, lambda binding: ports)
    assert kernel.control_host(request, "resume", "local-response", subject="ticket:2",
                               message="Independent product decision is now settled") == "applied"
    allocated = [operations.once().call, operations.once().call]
    assert all(call is not None for call in allocated)
    by_subject = {call.subject: call for call in allocated}
    assert set(by_subject) == {"ticket:2", "ticket:3"}
    local = by_subject["ticket:2"]
    assert local is not None and local.attempt_id == blocked.attempt_id
    assert local.thread_id == blocked.thread_id
    assert local.context["human_responses"] == ["Independent product decision is now settled"]
    stopped = by_subject["ticket:3"]
    assert stopped is not None and stopped.context["human_responses"] == ["Continue the stopped work"]
    assert stopped.attempt_id == first.attempt_id and stopped.thread_id != first.thread_id


def test_two_accepted_whole_run_responses_reach_the_original_work_in_order(tmp_path: Path) -> None:
    kernel, ports = tickets(tmp_path)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    first = operations.once().call
    assert first is not None
    # The original Worker reached final release before either accepted Stop.
    operations.runner.authorize_agent(first)
    assert kernel.dispatch_agent(first)
    kernel.agent_started(first, {"pid": 23, "pgid": 23, "start_token": "original-ticket-two",
        "host_pid": 42, "host_start_token": "start", "boot_id": "boot",
        "host_cgroup": "/host", "cgroup": "/host/worker"})
    kernel.agent_source_ready(first)
    for number in (1, 2):
        assert kernel.control_host(request, "stop", f"stop-{number}") == "applied"
        assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
                                                           42, "start", exit_status=0, ended_at=kernel.clock(),
                                                           usage_released=True)) == "exited"
        request = kernel.admit_host(f"resume-{number}", kind="resume", command=("native-host",),
                                    environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                    message=f"Accepted response {number}\r\n", new_thread=number == 1)
        assert kernel.dispatch_host(request)
        assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                           42, "start", usage_ready=True)) == "running"
    repeated = kernel.admit_host("resume-2", kind="resume", command=("native-host",),
                                 environment_carrier=tmp_path / "carrier", preflight=lambda: None,
                                 message="Accepted response 2\r\n")
    assert repeated == request and not kernel.dispatch_host(repeated)
    resumed = CoreOperations(kernel, Execution(request.run_id, request.owner, request.epoch), lambda binding: ports).once().call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.context["human_responses"] == ["Accepted response 1\r\n", "Accepted response 2\r\n"]
    assert resumed.thread_id != first.thread_id
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1
    responses = [item["identity"] for item in kernel.history("ticket:2") if item["kind"] == "human_response"]
    assert responses == ["resume-1", "resume-2"]


@pytest.mark.parametrize('initial,forbidden', [(False, 'new-per-attempt'), (True, 'reuse')])
@pytest.mark.parametrize('entry', ['control', 'control_host'])
def test_checkpoint_thread_policy_is_creation_frozen_and_legal_exceptions_stay_independent(
    tmp_path: Path, initial: bool, forbidden: str, entry: str,
) -> None:
    """Real Core/SQLite producers; no Worker, Host, Git or clock substitute."""
    kernel, ports = tickets(tmp_path, new_thread_per_development=initial)
    request, execution = managed(kernel, tmp_path)
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    previous = sibling = None
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            if step.call.subject == 'ticket:3':
                assert sibling is None
                sibling = step.call  # Keep the sibling's original idle lease.
            else:
                assert step.call.subject == 'ticket:2'
                previous = step.call
                kernel.bind_thread(previous, previous.reported_thread or 'checkpoint-ticket-two-' + previous.thread_id)
                kernel.receive(previous, wire_json({'result_kind': 'development',
                    'summary': 'Investigated candidate integrity', 'human_blockers': None}))
        if 'modification_budget_exhausted' in kernel.status('ticket:2')['gates']:
            break
    else:
        raise AssertionError('Normal candidate failures did not reach the original checkpoint')
    assert previous is not None
    assert 'modification_budget_exhausted' in kernel.status('ticket:2')['gates']
    if sibling is None:
        sibling = operations.once().call
    assert sibling is not None and sibling.subject == 'ticket:3'
    sibling_budget = kernel.status('ticket:3')['budgets']
    original_policy = kernel.status('ticket:2')['policy']
    assert original_policy['new_thread_per_development'] is initial
    with kernel.storage.transaction(write=False) as tx:
        original_run_policy = tx.one('SELECT policy FROM runs WHERE id=?', (execution.run_id,))[0]
        original_threads = [dict(row) for row in tx.all('SELECT * FROM threads WHERE run_id=? ORDER BY id', (execution.run_id,))]
        before = tuple(tx.connection.iterdump())

    def resume(action, **options):
        if entry == 'control_host':
            return kernel.control_host(request, 'resume', action, subject='ticket:2', **options)
        return kernel.control('resume', action, subject='ticket:2', **options)

    # Presence itself is invalid, including a same-value or unset override.
    # Mixing a legal budget/new-thread/message cannot apply any partial intent.
    frozen = 'new-per-attempt' if initial else 'reuse'
    for name, value in (('flip', forbidden), ('same', frozen), ('unset', None)):
        for _ in range(2):
            with pytest.raises(KernelError, match='policy_override_boundary'):
                resume('rejected-' + name, policy_overrides={'ticket_review_rounds': 3,
                    'development_deadline': '13s', 'development_thread_policy': value},
                    message='Do not persist rejected feedback', new_thread=True)
            with kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.connection.iterdump()) == before
                assert not tx.rows('SELECT id FROM actions WHERE run_id=? AND id=?',
                    (execution.run_id, 'rejected-' + name))

    # Profile Revision is a distinct valid boundary; old Threads remain frozen.
    revision = kernel.configure_task_profiles('valid-profile', overrides={
        'development_model': 'checkpoint-development-model'})
    assert revision == 2
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.all('SELECT * FROM threads WHERE run_id=? ORDER BY id', (execution.run_id,))] == original_threads
    overrides = {'ticket_review_rounds': 3, 'development_deadline': '13s'}
    assert resume('rejected-flip', policy_overrides=overrides) == 'applied'
    assert resume('rejected-flip', policy_overrides=overrides) == 'applied'
    renewed = kernel.status('ticket:2')['budgets']['requirements']
    assert (renewed['window'], renewed['development_limit'], renewed['review_limit']) == (2, 4, 3)
    resumed = operations.once().call
    assert resumed is not None and resumed.subject == 'ticket:2'
    assert resumed.attempt_id != previous.attempt_id and resumed.deadline_seconds == 13.0
    assert (resumed.thread_id != previous.thread_id) is initial
    assert resumed.profile_revision == (2 if initial else previous.profile_revision)
    if initial:
        assert resumed.profile.model == 'checkpoint-development-model'
    assert kernel.status('ticket:3')['budgets'] == sibling_budget
    assert kernel.status('ticket:2')['policy']['new_thread_per_development'] is initial

    # A one-off new Thread resumes this same Attempt and uses the current Profile
    # without changing the creation-frozen rotation policy or sibling budget.
    assert kernel.dispatch_agent(resumed)
    kernel.bind_thread(resumed, resumed.reported_thread or 'resumed-checkpoint-thread')
    kernel.agent_unavailable(resumed, 'worker_preparation_failed', 'Controlled pre-process failure')
    assert resume('one-off-thread', new_thread=True) == 'applied'
    successor = operations.once().call
    assert successor is not None and successor.attempt_id == resumed.attempt_id
    assert successor.thread_id != resumed.thread_id and successor.deadline_seconds == 13.0
    assert successor.profile_revision == revision and successor.profile.model == 'checkpoint-development-model'
    assert kernel.status('ticket:2')['policy']['new_thread_per_development'] is initial
    assert kernel.status('ticket:3')['budgets'] == sibling_budget
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT policy FROM runs WHERE id=?', (execution.run_id,))[0] == original_run_policy
        job = tx.one('SELECT policy_id FROM subjects WHERE run_id=? AND id=?', (execution.run_id, 'ticket:2'))
        assert json.loads(tx.get(job['policy_id'], 'ticket:2'))['new_thread_per_development'] is initial
        assert tx.one('SELECT new_thread FROM attempts WHERE run_id=? AND id=?',
            (execution.run_id, successor.attempt_id))[0] == 0
        assert tx.one('SELECT count(*) FROM actions WHERE run_id=? AND id=?',
            (execution.run_id, 'rejected-flip'))[0] == 1
