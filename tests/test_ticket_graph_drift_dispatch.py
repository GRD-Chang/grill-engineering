"""Graph currentness is reconciled before the shared dispatch pool drains."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.executor_admission import executor_admission_scope
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.notification_events import events
from agent_run.revisions import TicketGraphDriftError
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.task_control import ActionBusyError, TaskControlBusyError, TaskControlStore, TaskKey
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("source", ["lane", "supervision"])
@pytest.mark.parametrize("refresh_contention", [False, True])
def test_graph_removal_closes_resume_and_notifies_before_sibling_finishes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, source: str, refresh_contention: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = state["run_id"]
    started, release, closed = Event(), Event(), Event()
    errors: list[BaseException] = []
    notices: list[dict[str, Any]] = []
    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id).generation
    identity = dict(action_id=claim.action_id, generation=generation, run_id=run_id)
    control.bind_run(task, claim.action_id, run_id, generation=generation)
    control.mark_handshake(task, action_id=claim.action_id, generation=generation,
                           pid=900, process_start_token="executor")
    control.record_application(task, **identity, payload_digest=claim.action["payload_digest"])
    control.complete_action(task, action_id=claim.action_id, generation=generation, result_status="active")
    states.run_saved_observer = lambda current: notices.extend(events(current))

    def remove_ticket() -> None:
        data = json.loads(fixture.read_text())
        data["parent"]["sub_issues"].remove(2)
        replacement = fixture.with_suffix(".replacement")
        replacement.write_text(json.dumps(data))
        replacement.replace(fixture)

    development_calls: list[int] = []

    def develop(number: int) -> None:
        development_calls.append(number)
        if number == 3:
            started.set()
            assert release.wait(10)
        elif source == "lane":
            assert started.wait(10)
            remove_ticket()
        else:
            assert release.wait(10)

    tickets = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), develop))
    if source == "supervision":
        def drift(_run_id: str, _lanes: frozenset[int]) -> bool:
            assert started.wait(10)
            remove_ticket()
            raise TicketGraphDriftError("Ticket #2 is no longer present in the live graph")
        monkeypatch.setattr(tickets, "supervise", drift)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    refresh = controller.resume
    refresh_calls = 0

    def raced_refresh(target: str):
        nonlocal refresh_calls
        from agent_run.state import ConcurrentRunStateError
        refresh_calls += 1
        if refresh_contention and refresh_calls == 1:
            raise TaskControlBusyError("refresh raced") from ConcurrentRunStateError("sibling saved")
        return refresh(target)

    monkeypatch.setattr(controller, "resume", raced_refresh)
    engine = DeliveryRunEngine(controller=controller, tickets=tickets)

    def close(error: BaseException | None) -> None:
        control.close_ticket_resume_admission(task, **identity, can_exit=lambda: True)
        closed.set()

    def run() -> None:
        try:
            with executor_admission_scope(close):
                engine.deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert started.wait(5), errors
        assert closed.wait(5), errors
        assert thread.is_alive() and not release.is_set()
        current = states.load_current_run(run_id)
        assert current["status"] == "unsupported_scope_change", current["diagnostics"]
        assert any(item.get("current") and item.get("status_code") == "unsupported_scope_change"
                   for item in notices), notices
        with pytest.raises(ActionBusyError):
            with control.ticket_resume_transaction(task, **identity):
                pytest.fail("graph-invalid dispatch admitted Resume")
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    assert development_calls.count(2) == development_calls.count(3) == 1
    if refresh_contention:
        assert refresh_calls >= 2


def test_recovered_graph_observation_keeps_same_dispatcher_alive(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.delivery as delivery

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.01)
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    started, release, observed_again = Event(), Event(), Event()
    errors: list[BaseException] = []
    closures: list[BaseException | None] = []
    calls: list[int] = []

    def develop(number: int) -> None:
        calls.append(number)
        started.set()
        assert release.wait(10)

    tickets = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), develop))
    original = tickets.supervise
    observations = 0

    def transient_drift(run_id: str, lanes: frozenset[int]) -> bool:
        nonlocal observations
        assert started.wait(5)
        observations += 1
        if observations == 1:
            raise TicketGraphDriftError("graph changed during observation")
        observed_again.set()
        return original(run_id, lanes)

    monkeypatch.setattr(tickets, "supervise", transient_drift)
    engine = DeliveryRunEngine(controller=Controller(FixtureGitHubReader(fixture), git, states), tickets=tickets)

    def run() -> None:
        try:
            with executor_admission_scope(closures.append):
                engine.deliver_from_state(state["run_id"], state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert observed_again.wait(5), errors
        assert thread.is_alive() and not release.is_set()
        assert closures == []
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    assert calls.count(2) == calls.count(3) == 1
