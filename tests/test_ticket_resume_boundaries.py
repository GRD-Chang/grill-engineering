"""Small boundary tests complement the public concurrent Resume flow."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller, apply_resume_to_state
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.resume_intent import ResumeIntentError, bind_resume_intent
from agent_run.state import ConcurrentRunStateError, StateStore
from agent_run.task_control import ActionBusyError
from agent_run.ticket_pause import request_ticket_pause
from agent_run.ticket_resume import admit_ticket_resume
from support.cli import issue, stdout_json
from support.control import _bind_running_executor
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state

from support.state import snapshot_run


@pytest.fixture
def paused_ticket(git_repo: Path):
    fixture = write_fixture(git_repo / 'github.json', issues={str(n): issue(n) for n in (2, 3, 4)})
    seeded = seed_run(git_repo, fixture, '1')
    run_id = stdout_json(seeded)['run_id']
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))

    def fail(_number: int) -> None:
        raise RuntimeError('controlled development failure')

    agents = TicketAgents(git_repo, (2, 3), fail)
    ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
    control, task, worker = _bind_running_executor(git_repo, run_id)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    current = states.load_current_run(run_id)
    payload = {
        'parent': 1, 'run_id': run_id, 'ticket_number': 2, 'new_thread': False,
        'message': None, 'resume_budget_checkpoint': False,
        'policy': current['policy_snapshot'],
        'resume_intent': bind_resume_intent(current, ticket_number=2),
    }
    try:
        yield controller, control, task, control.load(task)['executor'], payload, fixture
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_resume_stop_and_stale_supervision_cannot_replay_authority(paused_ticket) -> None:
    controller, control, task, executor, payload, _ = paused_ticket
    run_id = payload['run_id']
    stale = controller.states.load_current_run(run_id)
    admitted, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert not repeated and admitted['ticket_work_queue'][0]['ticket_number'] == 2
    with pytest.raises(ConcurrentRunStateError, match='Resume superseded'):
        controller.states.save_run(run_id, stale)
    before_stop = controller.states.load_current_run(run_id)
    assert before_stop['ticket_jobs']['2']['last_ticket_resume'] == admitted['ticket_jobs']['2']['last_ticket_resume']
    request_ticket_pause(controller.states, run_id, 2)
    assert controller.states.load_current_run(run_id)['ticket_work_queue'] == []
    with pytest.raises(ResumeIntentError, match='Stop'):
        admit_ticket_resume(controller, control, task, executor, payload)
    assert controller.states.load_current_run(run_id)['ticket_jobs']['2']['review_budget'] == stale['ticket_jobs']['2']['review_budget']


@pytest.mark.parametrize('drift', ['requirements', 'global_stop', 'pause', 'ownership'])
def test_active_resume_revalidates_before_commit(paused_ticket, monkeypatch, drift: str) -> None:
    controller, control, task, executor, payload, fixture = paused_ticket
    original = controller._refresh
    before = controller.states.load_current_run(payload['run_id'])

    def refresh(state, parent):
        result = original(state, parent)
        if drift == 'requirements':
            result['ticket_graph']['tickets']['2']['body'] = 'changed requirement'
        elif drift == 'ownership':
            control.finish_executor(task, action_id=executor['action_id'], generation=executor['generation'])
        else:
            current = controller.states.load_current_run(payload['run_id'])
            if drift == 'global_stop':
                current['status'] = 'operator_stopped'
            else:
                job = current['ticket_jobs']['2']
                job['blocked_reason'] = 'ticket_paused'
                job['pause_resume_phase'] = job.pop('execution_resume_phase')
                if current.get('active_ticket_job', {}).get('ticket_number') == 2:
                    current['active_ticket_job'] = job
            controller.states.save_run(payload['run_id'], current)
        return result

    monkeypatch.setattr(controller, '_refresh', refresh)
    with pytest.raises(ValueError):
        admit_ticket_resume(controller, control, task, executor, payload)
    after = controller.states.load_current_run(payload['run_id'])
    assert after['ticket_work_queue'] == []
    assert after['resume_audit'] == before['resume_audit']
    assert after['ticket_jobs']['2']['review_budget'] == before['ticket_jobs']['2']['review_budget']
    assert after['ticket_jobs'].get('3') == before['ticket_jobs'].get('3')


def test_executor_exit_closes_admission_or_yields_to_new_resume(paused_ticket) -> None:
    controller, control, task, executor, payload, _ = paused_ticket
    arguments = dict(action_id=executor['action_id'], generation=executor['generation'], run_id=payload['run_id'])
    total = controller.states.load_current_run(payload['run_id'])['resume_audit']['total']
    admit_ticket_resume(controller, control, task, executor, payload)
    assert not control.close_ticket_resume_admission(
        task, **arguments,
        can_exit=lambda: controller.states.load_current_run(payload['run_id'])['resume_audit']['total'] == total,
    )
    assert control.close_ticket_resume_admission(task, **arguments, can_exit=lambda: True)
    with pytest.raises(ActionBusyError):
        with control.ticket_resume_transaction(task, **arguments):
            pytest.fail('closed Executor accepted new work')


@pytest.mark.parametrize("new_thread", [False, True])
def test_shared_resume_renews_only_target_exhausted_demand_window(new_thread: bool) -> None:
    policy = DeliveryPolicy(ticket_review_rounds=1)
    job: dict[str, Any] = {
        'ticket_number': 2, 'phase': 'blocked', 'blocked_reason': 'modification_budget_exhausted',
        'policy_snapshot': policy.snapshot(), 'repair_source': 'acceptance',
        'candidate_sha': 'candidate', 'ticket_branch_generation': 1,
        'review_budget': {
            'window': 1, 'development_attempts': 2, 'reviewer_invocations': 1,
            'final_ci_fix_used': False, 'review_artifacts': [],
            'checkpoint_reason': 'modification_budget_exhausted',
        },
        'review_budget_history': [], 'ci_recovery': {'unchanged': True},
        'integration_resync': {'unchanged': True},
    }
    sibling = deepcopy(job) | {'ticket_number': 3}
    before_sibling = deepcopy(sibling)
    old_budget = deepcopy(job['review_budget'])
    state = {
        'ticket_jobs': {'2': job, '3': sibling}, 'active_ticket_job': sibling,
        'ticket_agent_invocations': {}, 'policy_snapshot': DeliveryPolicy(ticket_development_workers=2).snapshot(),
        'ticket_integration_holder': None, 'ticket_integration_queue': [],
    }
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=False, new_thread=new_thread,
        human_response=None, resume_budget_checkpoint=True, budget_policy=policy,
        ticket_number=2, source_status='blocked',
    )
    assert job.get('development_new_thread', False) is new_thread
    assert job['review_budget']['window'] == 2
    assert job['review_budget_history'][0]['review_budget'] == old_budget
    assert job['ci_recovery'] == job['integration_resync'] == {'unchanged': True}
    assert job['candidate_sha'] == 'candidate' and job['ticket_branch_generation'] == 1
    assert sibling == before_sibling
    assert state['policy_snapshot'] == DeliveryPolicy(ticket_development_workers=2).snapshot()


def test_queued_resume_rechecks_remote_requirements_before_agent(paused_ticket) -> None:
    import json

    controller, control, task, executor, payload, fixture = paused_ticket
    admit_ticket_resume(controller, control, task, executor, payload)
    data = json.loads(fixture.read_text())
    data['issues']['2']['body'] = 'New requirements need a new generation.'
    fixture.write_text(json.dumps(data))
    calls = []
    git = controller.publisher.git
    agents = TicketAgents(fixture.parent, (2, 3), lambda n: calls.append(n))
    result = ticket_engine(git, controller.states, fixture, agents, development_only=True).deliver_ticket(payload['run_id'], 2)
    assert calls == []
    assert result['status'] == 'blocked'
    assert result['ticket_jobs']['2']['blocked_reason'] == 'effective_revision_mismatch'
    assert result['ticket_jobs']['2']['ticket_branch_generation'] == 1


def test_lifecycle_consumes_resume_accepted_at_driver_exit(tmp_path: Path) -> None:
    from agent_run.executor_host import ExecutorSpec, FakeExecutorHost
    from agent_run.run_lifecycle import LifecycleRequest, RunLifecycle
    from agent_run.task_control import TaskControlStore
    from support.lifecycle import _InMemoryRunState, _task

    task = _task(tmp_path)
    states = _InMemoryRunState()
    states.value['resume_audit'] = {'total': 0}
    control = TaskControlStore(tmp_path / 'control')
    host = FakeExecutorHost()
    executions = []

    def execute(run_id):
        result = deepcopy(states.load_current_run(run_id))
        executions.append(result['resume_audit']['total'])
        if len(executions) == 1:
            # A local Resume commits after the driver formed its exit result.
            states.value['resume_audit'] = {'total': 1}
        return result

    lifecycle = RunLifecycle(
        states=states, control=control, host=host, task=task,
        preflight=lambda: None, select_run=lambda action: (dict(states.value), False),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, action_id=action_id, run_id=run_id, generation=generation,
        ),
        execute=execute,
    )
    result, _, _ = lifecycle.submit(LifecycleRequest(task=task, kind='run', payload={'parent': 156}))
    assert executions == [0, 1]
    assert host.start_count == 1
    assert result['resume_audit']['total'] == 1
    assert control.load(task)['executor']['ticket_resume_closed'] is True
    assert control.load(task)['executor']['status'] == 'exited'


@pytest.mark.parametrize('override', ['ticket_development_workers', 'parent_ci_development_limit'])
def test_active_resume_cannot_change_other_work_budgets(paused_ticket, override: str) -> None:
    from agent_run.ticket_work import TicketWorkAdmission, generation

    controller, control, task, executor, payload, _ = paused_ticket
    state = controller.states.load_current_run(payload['run_id'])
    admission = TicketWorkAdmission(controller.states, payload['run_id'])
    for number in (3, 4):
        assert admission.acquire(number, generation(state, number), 'development') is not None
    before = controller.states.load_current_run(payload['run_id'])
    changed = deepcopy(payload)
    changed['policy'][override] = 1
    with pytest.raises(ResumeIntentError, match='并发或其他有效预算'):
        admit_ticket_resume(controller, control, task, executor, changed)
    assert controller.states.load_current_run(payload['run_id']) == before


def test_resume_accepts_repaired_remote_qualification(paused_ticket) -> None:
    controller, control, task, executor, payload, _ = paused_ticket
    stale = controller.states.load_current_run(payload['run_id'])
    ticket = stale['ticket_graph']['tickets']['2']
    ticket['labels'] = ['ready-for-human']
    ticket['eligibility'] = {'eligible': False, 'reason': 'ready-for-human'}
    controller.states.save_run(payload['run_id'], stale)
    # Remote fixture is now ready-for-agent; only the paused local projection is old.
    resumed, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert not repeated
    assert resumed['ticket_graph']['tickets']['2']['labels'] == ['ready-for-agent']
    assert resumed['ticket_graph']['tickets']['2']['eligibility']['eligible'] is True
    assert 2 in resumed['frontier']
    assert [item['ticket_number'] for item in resumed['ticket_work_queue']] == [2]
    duplicate, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert repeated and duplicate['ticket_work_queue'] == resumed['ticket_work_queue']


def test_exit_handshake_retries_concurrent_resume_transaction(paused_ticket, monkeypatch) -> None:
    from threading import Event, Thread
    from agent_run import task_control as control_module
    from agent_run.task_control import TaskControlStore

    _, control, task, executor, payload, _ = paused_ticket
    arguments = dict(action_id=executor['action_id'], generation=executor['generation'], run_id=payload['run_id'])
    contended = Event()
    finished = Event()
    results, errors = [], []
    original = control_module.fcntl.flock

    def observe_contention(fd, flags):
        try:
            return original(fd, flags)
        except BlockingIOError:
            contended.set()
            raise

    monkeypatch.setattr(control_module.fcntl, 'flock', observe_contention)

    def close():
        try:
            results.append(control.close_ticket_resume_admission(task, **arguments, can_exit=lambda: False))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    other_terminal = TaskControlStore(control.root)
    thread = Thread(target=close)
    try:
        with other_terminal.ticket_resume_transaction(task, **arguments):
            thread.start()
            assert contended.wait(5)
            assert not finished.is_set()
        assert finished.wait(5)
    finally:
        if thread.ident is not None:
            thread.join(5)
    assert not errors and results == [False]
    assert control.load(task)['executor'].get('ticket_resume_closed') is not True


@pytest.mark.parametrize('closed', [False, True])
def test_duplicate_resume_rechecks_executor_admission(paused_ticket, closed: bool) -> None:
    controller, control, task, executor, payload, _ = paused_ticket
    admitted, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert not repeated
    if closed:
        assert control.close_ticket_resume_admission(
            task, action_id=executor['action_id'], generation=executor['generation'],
            run_id=payload['run_id'], can_exit=lambda: True, failure='scheduler stopped',
        )
    state_before = snapshot_run((controller.states.root, str(payload['run_id'])))
    control_before = control.path_for(task).read_bytes()
    if closed:
        with pytest.raises(ActionBusyError, match='收口'):
            admit_ticket_resume(controller, control, task, executor, payload)
    else:
        duplicate, repeated = admit_ticket_resume(controller, control, task, executor, payload)
        assert repeated and duplicate == admitted
    assert snapshot_run((controller.states.root, str(payload['run_id']))) == state_before
    assert control.path_for(task).read_bytes() == control_before


def test_unknown_ticket_pause_continues_original_attempt_without_sibling_unlock(paused_ticket) -> None:
    controller, control, task, executor, payload, fixture = paused_ticket
    def fail_sibling(_number):
        raise RuntimeError('independent sibling pause')
    ticket_engine(controller.publisher.git, controller.states, fixture,
                  TicketAgents(fixture.parent, (2, 3), fail_sibling),
                  development_only=True).deliver_ticket(payload['run_id'], 3)
    state = controller.states.load_current_run(payload['run_id'])
    job = state['ticket_jobs']['2']
    job['blocked_reason'] = 'unclassified_worker_failure'
    state['active_ticket_job'] = job
    sibling = state['ticket_jobs']['3']
    sibling['blocked_reason'] = 'ticket_paused'
    sibling['pause_resume_phase'] = sibling.pop('execution_resume_phase')
    controller.states.save_run(payload['run_id'], state)
    payload['resume_intent'] = bind_resume_intent(state, ticket_number=2)
    before = deepcopy(state)
    resumed, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert not repeated
    assert resumed['ticket_jobs']['2']['phase'] == 'developing'
    assert resumed['ticket_jobs']['3'] == before['ticket_jobs']['3']
    assert resumed['ticket_jobs']['2']['pending_semantic_attempt'] == before['ticket_jobs']['2']['pending_semantic_attempt']
    assert resumed['ticket_jobs']['2']['review_budget'] == before['ticket_jobs']['2']['review_budget']
    duplicate, repeated = admit_ticket_resume(controller, control, task, executor, payload)
    assert repeated and duplicate == resumed
    calls = []

    def failed_again(number):
        calls.append(number)
        raise RuntimeError('controlled successor invocation')

    agents = TicketAgents(fixture.parent, (2, 3), failed_again)
    result = ticket_engine(controller.publisher.git, controller.states, fixture, agents,
                           development_only=True).deliver_ticket(payload['run_id'], 2)
    assert calls == [2]
    assert result['ticket_jobs']['2']['pending_semantic_attempt']['attempt_id'] == before['ticket_jobs']['2']['pending_semantic_attempt']['attempt_id']
    assert result['ticket_jobs']['2']['review_budget'] == before['ticket_jobs']['2']['review_budget']
    assert result['ticket_jobs']['3'] == before['ticket_jobs']['3']
