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
