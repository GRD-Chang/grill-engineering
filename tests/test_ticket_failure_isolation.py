"""Actual Invocation failure events must retain their Ticket owner."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlBusyError
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize(("failed", "workers"), [(2, 1), (2, 2), (3, 2)])
def test_worker_failure_pauses_only_its_ticket_and_preserves_original_error(
    git_repo: Path, failed: int, workers: int,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=workers)
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    agents.backends[failed].data["developments"][0]["error_after_writes"] = "original Worker failure"
    snapshots = []
    states.run_saved_observer = lambda value: snapshots.append(value["status"])
    result = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    ).deliver_from_state(str(state["run_id"]), state)

    failed_job = result["ticket_jobs"][str(failed)]
    assert failed_job["phase"] == "blocked"
    assert failed_job["blocked_reason"] == "ticket_execution_failed"
    assert failed_job["execution_resume_phase"] == "developing"
    assert failed_job["execution_failure"] == "original Worker failure"
    assert result["ticket_agent_invocations"][str(failed)]["error"] == "original Worker failure"
    checkout = states.root / "worktrees" / str(state["run_id"]) / f"ticket-{failed}"
    assert (checkout / f"ticket-{failed}.txt").read_text() == "done\n"
    other = result["ticket_jobs"][str(5 - failed)]
    assert other["phase"] == "completed" and other["ticket_closed_by_run"] is True
    assert "execution_failed" not in snapshots
    require_current_run_state(states.load_current_run(str(state["run_id"])))


@pytest.mark.parametrize("target_completed", [True, False])
@pytest.mark.parametrize("new_thread", [False, True])
def test_targeted_resume_uses_target_invocation_after_result_save_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    target_completed: bool, new_thread: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    completed_number = 3 if target_completed else 2
    failed_number = 5 - completed_number
    agents.backends[failed_number].data["developments"][0]["error_after_writes"] = "Worker failed"
    original_save = states._save_run_unlocked
    interrupted = False

    def fail_result_save_once(saved_run: str, saved: dict[str, Any]) -> None:
        nonlocal interrupted
        job = saved["ticket_jobs"].get(str(completed_number), {})
        if not interrupted and job.get("phase") == "committing_candidate":
            interrupted = True
            raise OSError("temporary result persistence failure")
        original_save(saved_run, saved)

    controller = Controller(FixtureGitHubReader(fixture), git, states)
    engine = DeliveryRunEngine(
        controller=controller, tickets=ticket_engine(git, states, fixture, agents),
    )
    with monkeypatch.context() as fault:
        fault.setattr(states, "_save_run_unlocked", fail_result_save_once)
        paused = engine.deliver_from_state(run_id, state)

    assert interrupted
    assert paused["active_ticket_job"]["ticket_number"] == 2
    assert all(job["blocked_reason"] == "ticket_execution_failed" for job in paused["ticket_jobs"].values())
    assert paused["ticket_agent_invocations"][str(completed_number)]["status"] == "completed"
    assert paused["ticket_agent_invocations"][str(failed_number)]["status"] == "failed"
    assert paused["ticket_jobs"][str(completed_number)]["execution_failure"] == "temporary result persistence failure"
    pending = deepcopy(paused["ticket_jobs"]["3"]["pending_semantic_attempt"])
    other_job = deepcopy(paused["ticket_jobs"]["2"])
    other_invocation = deepcopy(paused["ticket_agent_invocations"]["2"])
    successor_thread = "replacement-3" if new_thread else "developer-3"
    agents.backends[3].data["developments"].append({
        "expected_thread_id": None if new_thread else "developer-3",
        "thread_id": successor_thread,
        "summary": "Recovered the same attempt",
        "expected_files": {"ticket-3.txt": "done\n"},
    })
    agents.backends[3].data["publications"][0]["thread_id"] = successor_thread

    resumed, _ = controller.resume(
        run_id, explicit_resume=True, ticket_number=3, new_thread=new_thread,
    )
    result = engine.deliver_from_state(run_id, resumed)

    job = result["ticket_jobs"]["3"]
    assert job["phase"] == "completed", job.get("execution_failure")
    assert job["ticket_closed_by_run"] is True
    assert job["development_thread_id"] == successor_thread
    assert job["review_budget"]["development_attempts"] == 1
    successor = [
        item for item in result["agent_invocation_history"]
        if item["work_subject"] == "ticket:3" and item["role"] == "development"
    ]
    assert len(successor) == 2
    assert successor[-1]["status"] == "completed"
    assert successor[-1]["requested_thread_id"] == (None if new_thread else "developer-3")
    assert successor[-1]["mode"] == ("new-thread" if new_thread else "resume")
    assert successor[-1]["semantic_attempt"] == pending
    attempts = [item for item in job["semantic_attempt_history"] if item["role"] == "development"]
    assert len(attempts) == 1
    assert attempts[0]["attempt_id"] == pending["attempt_id"]
    assert result["ticket_jobs"]["2"] == other_job
    assert result["ticket_agent_invocations"]["2"] == other_invocation
    require_current_run_state(states.load_current_run(run_id))


def test_foreground_integration_failure_keeps_candidate_and_delivers_sibling(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.delivery_facts import multi_ticket_progress

    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), blocked_by={4: [2]})
    run_id = str(state['run_id'])
    agents = TicketAgents(git_repo, (2, 3, 4), lambda _: None)
    ready = ticket_engine(git, states, fixture, agents, development_only=True).deliver(run_id)
    original_job = deepcopy(ready['ticket_jobs']['2'])
    original = git.prepare_ticket_checkout
    original(branch=original_job['ticket_branch'], base_sha=original_job['base_sha'],
             checkout=states.root / 'worktrees' / run_id / 'ticket-2')

    def fail_first(**kwargs: Any) -> Any:
        if str(kwargs.get('checkout', '')).endswith('ticket-2'):
            raise OSError('controlled integration checkout failure')
        return original(**kwargs)

    monkeypatch.setattr(git, 'prepare_ticket_checkout', fail_first)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    engine = ticket_engine(git, states, fixture, agents)
    paused = engine.deliver(run_id)
    job = paused['ticket_jobs']['2']
    assert job['blocked_reason'] == 'ticket_execution_failed'
    assert job['execution_resume_phase'] == 'ready_for_integration'
    result = DeliveryRunEngine(controller=controller, tickets=engine).deliver(run_id)
    assert result['ticket_jobs']['3']['phase'] == 'completed'
    assert '4' not in result['ticket_jobs']
    assert result['ticket_integration_holder'] is None
    assert job['candidate_sha'] == original_job['candidate_sha']
    assert job['review_budget'] == original_job['review_budget']
    assert (states.root / 'worktrees' / run_id / 'ticket-2').exists()
    progress = multi_ticket_progress(result, executor_activity='exited')
    assert progress is not None
    details = {item['ticket_number']: item for item in progress['tickets']}
    assert details[2]['blocked_reason'] == 'ticket_execution_failed'
    assert details[4]['blocked_by'] == [2]
    assert details[2]['recovery_action'].endswith('--ticket 2')
    monkeypatch.setattr(git, 'prepare_ticket_checkout', original)
    resumed, _ = controller.resume(run_id, explicit_resume=True, ticket_number=2)
    assert resumed['ticket_jobs']['2']['candidate_sha'] == original_job['candidate_sha']
    completed = DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, resumed)
    assert all(completed['ticket_jobs'][str(n)]['phase'] == 'completed' for n in (2, 3, 4))
    require_current_run_state(states.load_current_run(run_id))


def test_control_contention_pauses_only_its_ticket(git_repo: Path) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)

    def develop(number: int) -> None:
        if number == 2:
            raise TaskControlBusyError("control lock deadline expired")

    agents = TicketAgents(git_repo, (2, 3), develop)
    result = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    ).deliver_from_state(state["run_id"], state)

    failed = result["ticket_jobs"]["2"]
    assert failed["phase"] == "blocked"
    assert failed["blocked_reason"] == "ticket_execution_failed"
    assert failed["execution_failure"] == "control lock deadline expired"
    assert failed["execution_resume_phase"] == "developing"
    assert result["ticket_jobs"]["3"]["phase"] == "completed"
    require_current_run_state(states.load_current_run(state["run_id"]))


def test_resume_after_local_contention_runs_before_sibling_finishes(git_repo: Path) -> None:
    from threading import Event, Thread
    from agent_run.resume_intent import bind_resume_intent
    from agent_run.task_control import TaskControlStore, TaskKey
    from agent_run.ticket_resume import admit_ticket_resume

    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = state['run_id']
    sibling_started, release_sibling, paused, resumed = (Event() for _ in range(4))
    failed_once = False
    errors: list[BaseException] = []
    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, 'example/project', 1)
    claim = control.claim_action(task, kind='run', payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id).generation
    identity = dict(action_id=claim.action_id, generation=generation)
    control.bind_run(task, claim.action_id, run_id, generation=generation)
    control.mark_handshake(task, **identity, pid=900, process_start_token='executor')
    control.record_application(task, **identity, run_id=run_id, payload_digest=claim.action['payload_digest'])
    control.complete_action(task, **identity, result_status='active')

    def develop(number: int) -> None:
        nonlocal failed_once
        if number == 3:
            sibling_started.set()
            assert release_sibling.wait(10)
        elif not failed_once:
            failed_once = True
            assert sibling_started.wait(10)
            raise TaskControlBusyError('control lock deadline expired')
        else:
            resumed.set()

    def saved(value: dict[str, Any]) -> None:
        if value['ticket_jobs'].get('2', {}).get('blocked_reason') == 'ticket_execution_failed':
            paused.set()

    states.run_saved_observer = saved
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    engine = DeliveryRunEngine(controller=controller, tickets=ticket_engine(
        git, states, fixture, TicketAgents(git_repo, (2, 3), develop),
    ))

    def run() -> None:
        try:
            engine.deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert paused.wait(10), errors
        current = states.load_current_run(run_id)
        budget = deepcopy(current['ticket_jobs']['2']['review_budget'])
        executor = control.load(task)['executor']
        accepted, duplicate = admit_ticket_resume(controller, control, task, executor, {
            'parent': 1, 'run_id': run_id, 'ticket_number': 2, 'new_thread': False,
            'message': None, 'resume_budget_checkpoint': False,
            'policy': current['policy_snapshot'],
            'resume_intent': bind_resume_intent(current, ticket_number=2),
        })
        assert not duplicate
        assert accepted['ticket_jobs']['2']['review_budget'] == budget
        assert resumed.wait(10), errors
        assert thread.is_alive() and not release_sibling.is_set()
    finally:
        release_sibling.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    final = states.load_current_run(run_id)
    assert all(final['ticket_jobs'][str(number)]['phase'] == 'completed' for number in (2, 3))
    assert final['ticket_jobs']['2']['review_budget']['development_attempts'] == 1
