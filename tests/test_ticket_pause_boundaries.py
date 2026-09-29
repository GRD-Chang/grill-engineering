"""Ticket-local gates at real orchestration and durable state boundaries."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery_facts import multi_ticket_progress
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_identity import GitHubIdentityError
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.runner_credentials import RunnerCredentialError
from agent_run.state_contract import require_current_run_state
from agent_run.ticket_state import TicketState
from agent_run.task_control import TaskControlBusyError
from support.cli_payloads import human_blocker_step, passing_acceptance
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("lane", ["development", "supervision"])
@pytest.mark.parametrize("failure_type", [GitHubIdentityError, RunnerCredentialError])
def test_work_scheduler_preserves_shared_failure_boundary(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    lane: str, failure_type: type[Exception],
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,), workers=1)
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2,), lambda _: None)
    engine = ticket_engine(git, states, fixture, agents)
    if lane == "supervision":
        ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
    before = states.load_current_run(run_id)
    assert before is not None
    failure = failure_type("confirmed shared identity failure")

    def fail(**_kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(git, "prepare_ticket_checkout", fail)

    def advance() -> None:
        if lane == "development":
            engine.develop_frontier(run_id, before)
        else:
            assert engine.supervise(run_id, frozenset()) is False

    if failure_type is GitHubIdentityError:
        with pytest.raises(GitHubIdentityError) as caught:
            advance()
        assert caught.value is failure
    else:
        advance()
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved["ticket_jobs"]["2"].get("blocked_reason") != "ticket_execution_failed"
    assert saved["agent_invocation_history"] == before["agent_invocation_history"]
    if failure_type is RunnerCredentialError:
        assert saved["status"] == "execution_failed"
        assert saved["diagnostics"][0]["code"] == "runner_credential_failed"
    require_current_run_state(saved)


@pytest.mark.parametrize('role', ['development', 'review'])
def test_human_blocker_waits_with_dependency_while_independent_ticket_completes(
    git_repo: Path, role: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), blocked_by={4: [2]})
    agents = TicketAgents(git_repo, (2, 3, 4), lambda _: None)
    if role == 'development':
        agents.backends[2].data['developments'] = [human_blocker_step('developer-2')]
    else:
        artifact = passing_acceptance('reviewer-2', 'controlled review')
        artifact['checks']['e2e'].update(status='blocked', evidence='Need product decision')
        agents.backends[2].data['reviews'] = [artifact]
    result = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    ).deliver_from_state(str(state['run_id']), state)
    assert result['ticket_jobs']['2']['blocked_reason'] == (
        'agent_requires_human' if role == 'development' else 'reviewer_requires_human'
    )
    assert result['ticket_jobs']['3']['phase'] == 'completed'
    assert '4' not in result['ticket_jobs']
    assert result['ticket_integration_holder'] is None
    assert result['ticket_integration_queue'] == []
    require_current_run_state(states.load_current_run(str(state['run_id'])))


def test_ci_timeout_delivers_independent_ticket_and_retains_exact_wait(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision

    clock = [100.25]
    monkeypatch.setattr(supervision, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, 'ExternalSupervisor',
                        lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pending')
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), blocked_by={4: [2]})
    run_id = str(state['run_id'])
    agents = TicketAgents(git_repo, (2, 3, 4), lambda _: None)
    engine = ticket_engine(git, states, fixture, agents)
    waiting = engine.deliver(run_id)
    original = deepcopy(waiting['ticket_jobs']['2'])
    deadline = original['external_wait']['supervision_window']['deadline']
    clock[0] = deadline
    engine.supervise(run_id, frozenset())
    monkeypatch.setattr(FixtureGitHubPublisher, '_next_required_checks_result', lambda *_: 'pass')
    result = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states), tickets=engine,
    ).deliver(run_id)
    job = result['ticket_jobs']['2']
    assert job['blocked_reason'] == 'ticket_supervision_timeout'
    assert job['supervision_wait']['deadline'] == deadline
    for key in ('candidate_sha', 'publication_sha', 'pr_number', 'review_budget', 'ci_recovery'):
        assert job[key] == original[key]
    assert result['ticket_jobs']['3']['phase'] == 'completed'
    assert '4' not in result['ticket_jobs']
    assert result['ticket_integration_holder'] is None
    progress = multi_ticket_progress(result, executor_activity='exited')
    assert progress is not None
    paused = next(item for item in progress['tickets'] if item['ticket_number'] == 2)
    assert paused['supervision']['remaining_seconds'] == 0
    assert paused['recovery_action'].endswith('--ticket 2')
    assert (states.root / 'worktrees' / run_id / 'ticket-2').exists()
    require_current_run_state(states.load_current_run(run_id))


@pytest.mark.parametrize('sibling_blocked', [False, True])
def test_shared_credential_failure_is_durable_before_sibling_can_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, sibling_blocked: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state['run_id'])
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    if sibling_blocked:
        agents.backends[2].data['developments'] = [human_blocker_step('developer-2')]
    development = ticket_engine(git, states, fixture, agents, development_only=True)
    development.deliver_ticket(run_id, 2)
    development.deliver_ticket(run_id, 3)
    sibling = TicketState(states, run_id, 2)
    old = sibling.load_current_run(run_id)
    assert old is not None
    original_job = deepcopy(old['ticket_jobs']['2'])

    def fail(**_kwargs: Any) -> None:
        raise RunnerCredentialError('confirmed shared credential revoked')

    monkeypatch.setattr(git, 'prepare_ticket_checkout', fail)
    result = ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 3)
    assert result['status'] == 'execution_failed'
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved['diagnostics'][0]['code'] == 'runner_credential_failed'
    assert saved['active_ticket_job']['ticket_number'] == 3
    assert saved['ticket_jobs']['2'] == original_job
    with pytest.raises(TaskControlBusyError):
        sibling.save_run(run_id, old)
    require_current_run_state(saved)


def test_each_wait_keeps_its_deadline_through_other_ticket_progress_and_refresh(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision
    from agent_run.github import GitHubReadError

    clock = [100.25]
    monkeypatch.setattr(supervision, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, 'ExternalSupervisor',
                        lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4))
    run_id = str(state['run_id'])

    def unavailable(number: int) -> None:
        if number in (2, 3):
            raise GitHubReadError('github_read_failed', 'unknown transport failure')

    agents = TicketAgents(git_repo, (2, 3, 4), unavailable)
    development = ticket_engine(git, states, fixture, agents, development_only=True)
    development.deliver_ticket(run_id, 2)
    clock[0] += 10
    development.deliver_ticket(run_id, 3)
    waiting = states.load_current_run(run_id)
    deadlines = {n: waiting['ticket_jobs'][str(n)]['external_wait']['supervision_window']['deadline']
                 for n in (2, 3)}
    assert deadlines[3] - deadlines[2] == 10
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver_ticket(run_id, 4)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    controller.resume(run_id)
    clock[0] = deadlines[2]
    assert ticket_supervision.development_wait_due(engine, run_id, 2) is None
    assert ticket_supervision.development_wait_due(engine, run_id, 3) is True
    saved = states.load_current_run(run_id)
    assert saved['ticket_jobs']['2']['supervision_wait']['deadline'] == deadlines[2]
    assert saved['ticket_jobs']['3']['external_wait']['supervision_window']['deadline'] == deadlines[3]
    assert saved['ticket_jobs']['4']['phase'] == 'completed'
    clock[0] = deadlines[3]
    assert ticket_supervision.development_wait_due(engine, run_id, 3) is None
    require_current_run_state(states.load_current_run(run_id))
