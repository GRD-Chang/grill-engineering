"""Admitted development waits remain supervised during unrelated long work."""
from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state_contract import require_current_run_state
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize(("outcome", "slow_read"), [("retry", False), ("timeout", False), ("timeout", True)])
def test_development_wait_is_supervised_before_other_agent_returns(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, slow_read: bool,
) -> None:
    import agent_run.delivery as delivery
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.02)
    clock = [100.0]
    monkeypatch.setattr(supervision, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, "ExternalSupervisor",
                        lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    numbers = (2, 3, 4) if slow_read else (2, 3)
    state, states, git, fixture = ticket_run(git_repo, numbers, workers=2)
    run_id = str(state["run_id"])
    entered, release, waiting, advanced = (Event() for _ in range(4))
    available = Event()
    reading, release_read = Event(), Event()
    attempts: list[int] = []

    def before_develop(number: int) -> None:
        if number == 3:
            entered.set()
            assert release.wait(10)
        elif number == 2:
            attempts.append(number)
            if not available.is_set():
                raise GitHubReadError("github_read_failed", "controlled external read")

    def observe(current: dict[str, Any]) -> None:
        job = current["ticket_jobs"].get("2", {})
        if isinstance(job.get("external_wait"), dict):
            waiting.set()
        target = "completed" if outcome == "retry" else "blocked"
        if job.get("phase") == target:
            advanced.set()

    states.run_saved_observer = observe
    agents = TicketAgents(git_repo, numbers, before_develop)
    engine = ticket_engine(git, states, fixture, agents)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    if slow_read:
        monkeypatch.setattr(FixtureGitHubPublisher, "_next_required_checks_result", lambda *_: "pending")
        engine.deliver_ticket(run_id, 4)
        state, _ = controller.resume(run_id)
        original = FixtureGitHubPublisher.required_checks_snapshot

        def read(publisher: FixtureGitHubPublisher, pr_number: int, **kwargs: Any) -> Any:
            reading.set()
            assert release_read.wait(10)
            return original(publisher, pr_number, **kwargs)

        monkeypatch.setattr(FixtureGitHubPublisher, "required_checks_snapshot", read)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10) and waiting.wait(10), errors
        if slow_read:
            assert reading.wait(10), errors
        saved = states.load_current_run(run_id)
        assert saved is not None
        deadline = saved["ticket_jobs"]["2"]["external_wait"]["supervision_window"]["deadline"]
        if outcome == "retry":
            available.set()
            clock[0] += 61
        else:
            clock[0] = deadline + 1
        assert advanced.wait(10), errors
        assert not release.is_set() and thread.is_alive()
        assert not release_read.is_set()
        current = states.load_current_run(run_id)
        assert current is not None
        job = current["ticket_jobs"]["2"]
        if outcome == "timeout":
            assert job["blocked_reason"] == "ticket_supervision_timeout"
            assert job["execution_resume_phase"] == "developing"
            assert job["supervision_wait"]["deadline"] == deadline
        else:
            assert len(attempts) >= 2
            assert job["ticket_closed_by_run"] is True
        require_current_run_state(current)
    finally:
        release.set()
        release_read.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors


@pytest.mark.parametrize("wait_kind", ["branch", "credential"])
def test_preparation_wait_leaves_fifo_and_expires_while_work_slot_is_full(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, wait_kind: str,
) -> None:
    import agent_run.delivery as delivery
    import agent_run.external_supervision as supervision
    import agent_run.ticket_supervision as ticket_supervision

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.02)
    clock = [100.0]
    monkeypatch.setattr(supervision, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ticket_supervision, "ExternalSupervisor",
                        lambda: supervision.ExternalSupervisor(now=lambda: clock[0]))
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=1)
    run_id = state["run_id"]
    entered, release, expired = Event(), Event(), Event()
    retries: list[int] = []
    original = FixtureGitHubPublisher.ensure_ticket_branch

    def prepare(publisher: FixtureGitHubPublisher, **kwargs: Any) -> None:
        if kwargs["ticket_number"] != 2 or wait_kind != "branch":
            original(publisher, **kwargs)
            return
        current = states.load_current_run(run_id)
        wait = current["ticket_jobs"]["2"].get("external_wait", {})
        retry = wait.get("supervision_window", {}).get("retry_count", 0)
        assert not retries or retry > retries[-1], "preparation bypassed durable backoff"
        retries.append(retry)
        raise GitHubReadError("github_read_failed", "branch preparation unavailable")

    def develop(number: int) -> None:
        if number == 2 and wait_kind == "credential":
            from agent_run.credential_lifecycle import InitialCredentialUnavailable

            current = states.load_current_run(run_id)
            wait = current["ticket_jobs"]["2"].get("external_wait", {})
            retry = wait.get("supervision_window", {}).get("retry_count", 0)
            assert not retries or retry > retries[-1], "credential retry bypassed backoff"
            retries.append(retry)
            raise InitialCredentialUnavailable("issuer unavailable")
        assert number == 3
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(FixtureGitHubPublisher, "ensure_ticket_branch", prepare)
    states.run_saved_observer = lambda value: expired.set() if value["ticket_jobs"].get("2", {}).get("blocked_reason") == "ticket_supervision_timeout" else None
    agents = TicketAgents(git_repo, (2, 3), develop)
    engine = ticket_engine(git, states, fixture, agents)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            engine.develop_frontier(run_id, state, supervise=lambda lanes: engine.supervise(run_id, lanes))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10), errors
        current = states.load_current_run(run_id)
        wait = current["ticket_jobs"]["2"]["external_wait"]
        clock[0] = wait["supervision_window"]["deadline"] + 1
        assert expired.wait(10), errors
        assert not release.is_set()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    final = states.load_current_run(run_id)
    assert final["ticket_jobs"]["2"]["blocked_reason"] == "ticket_supervision_timeout"
    assert final["ticket_work_queue"] == []
    if wait_kind == "credential":
        job = final["ticket_jobs"]["2"]
        assert job["review_budget"]["development_attempts"] == 0
        assert "2" not in final["ticket_agent_invocations"] or final["ticket_agent_invocations"]["2"] is None
        assert not any(item.get("work_subject") == "ticket:2" for item in final["agent_invocation_history"])
        assert job["supervision_wait"]["deadline"] == wait["supervision_window"]["deadline"]
