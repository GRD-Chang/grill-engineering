"""Resume and global controls remain live after development lanes drain."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_run.controller import Controller
from agent_run.executor_admission import executor_admission_scope
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.resume_intent import bind_resume_intent
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.ticket_resume import admit_ticket_resume
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize('publication_timing', ['ongoing', 'final', 'integration_only'])
def test_resume_dispatches_after_development_drains_before_observer_returns(
    git_repo: Path, publication_timing: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), workers=2)
    run_id = state['run_id']
    publishing, release_publication, developing, release_development = (Event() for _ in range(4))
    drained, admitted, resumed = (Event() for _ in range(3))
    errors: list[BaseException] = []
    preparation = True

    def develop(number: int) -> None:
        if number == 4:
            if preparation:
                raise RuntimeError('controlled initial failure')
            resumed.set()
        if number == 3 and not preparation:
            developing.set()
            assert release_development.wait(10)

    def publication(number: int) -> None:
        if number == 2:
            publishing.set()
            assert release_publication.wait(10)

    agents = TicketAgents(git_repo, (2, 3, 4), develop, before_publication=publication)
    development = ticket_engine(git, states, fixture, agents, development_only=True)
    development.deliver_ticket(run_id, 4)
    development.deliver_ticket(run_id, 2)
    if publication_timing == 'integration_only':
        development.deliver_ticket(run_id, 3)
    preparation = False
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.resume(run_id)
    budget = deepcopy(state['ticket_jobs']['4']['review_budget'])
    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, 'example/project', 1)
    claim = control.claim_action(task, kind='run', payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id).generation
    identity = dict(action_id=claim.action_id, generation=generation)
    control.bind_run(task, claim.action_id, run_id, generation=generation)
    control.mark_handshake(task, **identity, pid=900, process_start_token='executor')
    control.record_application(task, **identity, run_id=run_id, payload_digest=claim.action['payload_digest'])
    control.complete_action(task, **identity, result_status='active')
    engine = ticket_engine(git, states, fixture, agents)

    def after_lane(lanes: frozenset[int]) -> None:
        if not lanes:
            drained.set()
            if publication_timing == 'ongoing':
                assert admitted.wait(10)

    def supervise(lanes: frozenset[int]) -> None:
        if publication_timing == 'ongoing' or not lanes:
            engine.supervise(run_id, lanes)

    def run() -> None:
        try:
            if publication_timing == 'integration_only':
                DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, state)
            else:
                engine.develop_frontier(run_id, state, after_lane=after_lane, supervise=supervise)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        if publication_timing != 'integration_only':
            assert developing.wait(10), errors
            if publication_timing == 'ongoing':
                assert publishing.wait(10), errors
            release_development.set()
            assert drained.wait(10), errors
        assert publishing.wait(10), errors
        current = states.load_current_run(run_id)
        assert current is not None
        assert current['ticket_jobs']['3']['phase'] == 'ready_for_integration'
        assert [(item['ticket_number'], item['role']) for item in current['ticket_work_queue']] == [(2, 'publication')]
        assert current['ticket_work_queue'][0]['lease_id'] is not None
        accepted, duplicate = admit_ticket_resume(controller, control, task, control.load(task)['executor'], {
            'parent': 1, 'run_id': run_id, 'ticket_number': 4, 'new_thread': False,
            'message': None, 'resume_budget_checkpoint': False,
            'policy': current['policy_snapshot'],
            'resume_intent': bind_resume_intent(current, ticket_number=4),
        })
        assert not duplicate
        assert accepted['ticket_jobs']['4']['review_budget'] == budget
        assert [item['ticket_number'] for item in accepted['ticket_work_queue']] == [2, 4]
        admitted.set()
        assert resumed.wait(5), errors
        assert not release_publication.is_set() and thread.is_alive()
    finally:
        admitted.set()
        release_development.set()
        release_publication.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    final = states.load_current_run(run_id)
    assert final is not None
    assert final['ticket_jobs']['4']['candidate_sha'] is not None
    assert final['ticket_jobs']['4']['review_budget']['development_attempts'] == budget['development_attempts'] == 1
    assert final['ticket_work_queue'] == []
    require_current_run_state(final)


@pytest.mark.parametrize('outcome', ['complete', 'failure', 'stop'])
def test_observer_only_interval_handles_completion_failure_and_stop(git_repo: Path, outcome: str) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,), workers=1)
    run_id = state['run_id']
    observing, release_observer, drained, continue_dispatch, closed = (Event() for _ in range(5))
    errors: list[BaseException] = []
    closures: list[BaseException | None] = []
    failure = RuntimeError('controlled shared observer failure')

    def develop(_number: int) -> None:
        assert observing.wait(10)

    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), develop))

    def observe(_lanes: frozenset[int]) -> None:
        observing.set()
        assert release_observer.wait(10)
        if outcome == 'failure':
            raise failure

    def after_lane(lanes: frozenset[int]) -> None:
        if not lanes:
            drained.set()
            assert continue_dispatch.wait(10)

    def close(error: BaseException | None) -> None:
        closures.append(error)
        closed.set()

    def run() -> None:
        try:
            with executor_admission_scope(close):
                engine.develop_frontier(run_id, state, after_lane=after_lane, supervise=observe)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert drained.wait(10), errors
        if outcome == 'stop':
            current = states.load_current_run(run_id)
            assert current is not None
            current['status'] = 'operator_stopped'
            current['terminal_kind'] = 'operator_stopped'
            states.save_run(run_id, current)
        continue_dispatch.set()
        if outcome == 'stop':
            assert closed.wait(5), errors
            assert closures == [None]
            assert thread.is_alive() and not release_observer.is_set()
        release_observer.set()
        thread.join(10)
        assert not thread.is_alive()
        assert errors == ([failure] if outcome == 'failure' else [])
        assert closures == ([failure] if outcome == 'failure' else [None] if outcome == 'stop' else [])
    finally:
        continue_dispatch.set()
        release_observer.set()
        thread.join(10)
    assert not thread.is_alive()
