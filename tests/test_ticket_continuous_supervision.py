"""Continuous Run orchestration with real Git/state and controlled external I/O."""
from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.controller import Controller
from agent_run.delivery_facts import multi_ticket_progress
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_state import TicketState
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize(('workers', 'role'), [(1, 'development'), (3, 'development'), (1, 'review')])
def test_ci_green_closes_and_cleans_ticket_before_occupied_lanes_return(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, workers: int, role: str,
) -> None:
    import agent_run.delivery as delivery
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision

    monkeypatch.setattr(delivery, 'SUPERVISION_POLL_SECONDS', 0.02)
    clock = [100.0]
    monkeypatch.setattr(supervision, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, 'ExternalSupervisor', lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    numbers = (2, *range(3, workers + 3))
    state, states, git, fixture = ticket_run(git_repo, numbers, workers=workers)
    run_id = str(state['run_id'])
    green, release, complete = Event(), Event(), Event()
    entered = {number: Event() for number in numbers[1:]}
    publications: list[int] = []

    def hold(number: int) -> None:
        if number != 2:
            entered[number].set()
            assert release.wait(10)

    def invocation_gate(_backend: Any, kind: str, request: dict[str, Any]) -> None:
        if kind == ('developments' if role == 'development' else 'reviews'):
            hold(int(request['ticket']['number']))

    monkeypatch.setattr(FixtureAgentBackend, '_wait_for_invocation_gate', invocation_gate)
    agents = TicketAgents(git_repo, numbers, lambda _: None, before_publication=publications.append)
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result',
                        lambda _self, _pr: 'pass' if green.is_set() else 'pending')
    engine = ticket_engine(git, states, fixture, agents)
    first = engine.deliver(run_id)
    first_job = first['ticket_jobs']['2']
    assert first_job['phase'] == 'waiting_checks'
    published_head = first_job['publication_sha']
    deadline = first_job['external_wait']['supervision_window']['deadline']
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.resume(run_id)
    assert state['active_ticket_job']['ticket_number'] != 2

    def observe(current: dict[str, Any]) -> None:
        job = current['ticket_jobs'].get('2', {})
        cleanup = current.get('delivery_cleanup', {}).get('items', {}).get(job.get('ticket_branch'), {})
        if job.get('phase') == 'completed' and cleanup.get('status') == 'completed':
            complete.set()

    states.run_saved_observer = observe
    errors: list[BaseException] = []

    def run() -> None:
        try:
            DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert all(event.wait(10) for event in entered.values()), errors
        current = states.load_current_run(run_id)
        assert current is not None
        progress = multi_ticket_progress(current, executor_activity='running')
        assert progress is not None and progress['development_workers_active'] == workers
        assert current['ticket_jobs']['2']['external_wait']['supervision_window']['deadline'] == deadline
        green.set()
        clock[0] += 61
        assert complete.wait(10), errors
        assert not release.is_set() and thread.is_alive()
        saved = states.load_current_run(run_id)
        assert saved is not None
        job = saved['ticket_jobs']['2']
        assert job['publication_sha'] == published_head
        assert job['ticket_closed_by_run'] is True
        assert job['ticket_close_ownership']
        assert job['deterministic_integration_record']['pr']['state'] == 'MERGED'
        assert git.resolve(saved['run_branch']) == job['integrated_sha']
        assert not (states.root / 'worktrees' / run_id / 'ticket-2').exists()
        assert all((states.root / 'worktrees' / run_id / f'ticket-{n}').exists() for n in numbers[1:])
        assert publications.count(2) == 1
        require_current_run_state(saved)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    final = states.load_current_run(run_id)
    assert final is not None and final['ticket_jobs']['2']['phase'] == 'completed'
    assert publications.count(2) == 1


def test_blocked_external_read_does_not_hold_state_lock_or_delay_other_lane_result(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.delivery as delivery
    monkeypatch.setattr(delivery, 'SUPERVISION_POLL_SECONDS', 0.02)
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state['run_id'])
    reading, release_read, started, release_agent, ready = (Event() for _ in range(5))
    original = FixtureGitHubPublisher.required_checks_snapshot

    def read(publisher: FixtureGitHubPublisher, pr_number: int, **kwargs: Any) -> Any:
        if reading.is_set():
            assert release_read.wait(10)
        return original(publisher, pr_number, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pending')
    monkeypatch.setattr(FixtureGitHubPublisher, 'required_checks_snapshot', read)

    def hold(number: int) -> None:
        if number == 3:
            started.set()
            assert release_agent.wait(10)

    agents = TicketAgents(git_repo, (2, 3), hold)
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver(run_id)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.resume(run_id)
    read_entered = Event()

    def blocked_read(publisher: FixtureGitHubPublisher, pr_number: int, **kwargs: Any) -> Any:
        read_entered.set()
        reading.set()
        return read(publisher, pr_number, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, 'required_checks_snapshot', blocked_read)
    states.run_saved_observer = lambda value: ready.set() if value['ticket_jobs'].get('3', {}).get('phase') == 'ready_for_integration' else None
    errors: list[BaseException] = []

    def run() -> None:
        try:
            DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert started.wait(10) and read_entered.wait(10), errors
        release_agent.set()
        assert ready.wait(10), errors
        # A public local control transaction completes while the external read is blocked.
        from agent_run.ticket_pause import request_ticket_pause
        paused = request_ticket_pause(states, run_id, 3)
        assert paused['ticket_jobs']['3']['blocked_reason'] == 'ticket_paused'
        assert not release_read.is_set()
    finally:
        release_agent.set()
        release_read.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    saved = states.load_current_run(run_id)
    assert saved is not None and saved['ticket_jobs']['3']['blocked_reason'] == 'ticket_paused'


def test_ticket_state_rejects_late_result_even_after_control_read(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3))
    run_id = str(state['run_id'])
    lane = TicketState(states, run_id, 2)
    snapshot = lane.load_current_run(run_id)
    assert snapshot is not None
    latest = states.load_current_run(run_id)
    assert latest is not None
    latest['ticket_jobs']['2']['source_revision_changed'] = True
    latest['ticket_jobs']['2']['base_sha'] = 'replaced-job'
    latest['active_ticket_job'] = latest['ticket_jobs']['2']
    states.save_run(run_id, latest)
    # A pause/ownership read during a running call must not rebase its CAS token.
    lane.load_current_run(run_id)
    with pytest.raises(TaskControlBusyError):
        lane.save_run(run_id, snapshot)
    assert states.load_current_run(run_id) == latest


def test_ticket_timeout_releases_seat_and_resumes_same_publication(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision
    from agent_run.resume_intent import bind_resume_intent

    clock = [100.0]
    monkeypatch.setattr(supervision, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, 'ExternalSupervisor', lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    green = Event()
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pass' if green.is_set() else 'pending')
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=1)
    run_id = str(state['run_id'])
    publications: list[int] = []
    agents = TicketAgents(git_repo, (2, 3), lambda _: None, before_publication=publications.append)
    engine = ticket_engine(git, states, fixture, agents)
    waiting = engine.deliver(run_id)
    job = waiting['ticket_jobs']['2']
    candidate, head, budget = job['candidate_sha'], job['publication_sha'], job['review_budget'].copy()
    deadline = job['external_wait']['supervision_window']['deadline']
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    controller.resume(run_id)
    clock[0] = deadline + 0.1
    engine.supervise(run_id, frozenset({3}))
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved['ticket_integration_holder'] is None
    assert saved['ticket_jobs']['2']['blocked_reason'] == 'ticket_supervision_timeout'
    assert saved['ticket_jobs']['2']['supervision_wait']['deadline'] == deadline
    assert saved['active_ticket_job']['ticket_number'] == 3
    assert saved['status'] != 'supervision_timeout'
    assert bind_resume_intent(saved, ticket_number=2)['authorization'] == 'continue_ticket'
    resumed, _ = controller.resume(run_id, explicit_resume=True, ticket_number=2)
    assert resumed['ticket_jobs']['2']['candidate_sha'] == candidate
    assert resumed['ticket_jobs']['2']['publication_sha'] == head
    assert resumed['ticket_jobs']['2']['review_budget'] == budget
    assert 'external_wait' not in resumed['ticket_jobs']['2']
    green.set()
    completed = engine.deliver(run_id)
    assert completed['ticket_jobs']['2']['phase'] == 'completed'
    assert publications == [2]
    require_current_run_state(states.load_current_run(run_id))


def test_ticket_wait_rejects_malformed_or_other_head_observation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy
    from agent_run.state_contract import IncompatibleRunStateError

    state, states, git, fixture = ticket_run(git_repo, (2,))
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pending')
    agents = TicketAgents(git_repo, (2,), lambda _: None)
    waiting = ticket_engine(git, states, fixture, agents).deliver(str(state['run_id']))
    for field, value in [('diagnostics', 'not-a-list'), ('supervision_window', None), ('status', 'active')]:
        malformed = deepcopy(waiting)
        malformed['ticket_jobs']['2']['external_wait'][field] = value
        with pytest.raises(IncompatibleRunStateError, match='Ticket external wait'):
            require_current_run_state(malformed)
    stale = deepcopy(waiting)
    stale['ticket_jobs']['2']['external_wait']['supervision_window']['identity'] = 'another-head'
    with pytest.raises(IncompatibleRunStateError, match='stale supervision identity'):
        require_current_run_state(stale)


def test_ticket_scope_cannot_read_or_write_another_run(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2,))
    states.save_run('other', {'value': 'preserved'})
    scoped = TicketState(states, str(state['run_id']), 2)
    with pytest.raises(ValueError, match='another Run'):
        scoped.load_current_run('other')
    with pytest.raises(ValueError, match='another Run'):
        scoped.save_run('other', {'value': 'overwrite'})
    assert states.load_run('other') == {'value': 'preserved'}


def test_foreground_timeout_uses_the_same_ticket_pause_and_resume_boundary(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.external_supervision as supervision
    from agent_run.run_driver import DirectRunOperations, RunDriver
    from agent_run.resume_intent import bind_resume_intent

    clock = [100.0]
    monkeypatch.setattr(supervision, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(supervision, 'CHECKS_BUDGET_SECONDS', 1)
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pending')
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state['run_id'])
    agents = TicketAgents(git_repo, (2,), lambda _: None)
    waiting = ticket_engine(git, states, fixture, agents).deliver(run_id)
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    operations = DirectRunOperations(
        controller=controller, states=states, git=git, github_reader=reader,
        publisher_factory=lambda: FixtureGitHubPublisher(fixture, git), agents=agents,
    )

    def advance_clock(seconds: float) -> None:
        clock[0] += seconds

    paused = RunDriver(
        operations=operations, states=states,
        supervisor=supervision.ExternalSupervisor(now=lambda: clock[0], sleeper=advance_clock),
    ).advance(waiting)
    assert paused['status'] == 'blocked'
    assert paused['ticket_jobs']['2']['blocked_reason'] == 'ticket_supervision_timeout'
    assert paused['ticket_integration_holder'] is None
    assert bind_resume_intent(paused, ticket_number=2)['authorization'] == 'continue_ticket'
    require_current_run_state(states.load_current_run(run_id))


def test_integration_preparation_failure_resumes_queued_candidate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state['run_id'])
    agents = TicketAgents(git_repo, (2,), lambda _: None)
    ready = ticket_engine(git, states, fixture, agents, development_only=True).deliver(run_id)
    candidate = ready['ticket_jobs']['2']['candidate_sha']
    budget = ready['ticket_jobs']['2']['review_budget'].copy()
    original = git.prepare_ticket_checkout

    def fail_checkout(**_kwargs: Any) -> None:
        raise OSError('checkout temporarily unavailable')

    monkeypatch.setattr(git, 'prepare_ticket_checkout', fail_checkout)
    engine = ticket_engine(git, states, fixture, agents)
    engine.supervise(run_id, frozenset())
    paused = states.load_current_run(run_id)
    assert paused is not None
    assert paused['ticket_jobs']['2']['blocked_reason'] == 'ticket_execution_failed'
    assert paused['ticket_jobs']['2']['execution_resume_phase'] == 'ready_for_integration'
    monkeypatch.setattr(git, 'prepare_ticket_checkout', original)
    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True, ticket_number=2,
    )
    assert resumed['ticket_integration_queue'][0]['ticket_number'] == 2
    assert resumed['ticket_jobs']['2']['candidate_sha'] == candidate
    assert resumed['ticket_jobs']['2']['review_budget'] == budget
    completed = engine.deliver(run_id)
    assert completed['ticket_jobs']['2']['phase'] == 'completed'
