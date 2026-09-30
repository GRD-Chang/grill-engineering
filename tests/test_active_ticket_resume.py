"""Active Resume crosses real CLI/ownership boundaries into the shared FIFO."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from support.cli import issue, run_cli, stdout_json
from support.control import _bind_running_executor
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state


def test_active_resume_is_durable_fifo_and_automatically_restarts_review(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Agent responses are controlled; CLI, Git, scheduler and disk are real."""
    import agent_run.delivery as delivery

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.02)
    numbers = (2, 3, 4, 5)
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): issue(number) for number in numbers
    })
    seeded = seed_run(git_repo, fixture, "1", "--ticket-development-workers", "2")
    assert seeded.returncode == 0, seeded.stderr
    run_id = stdout_json(seeded)["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    started = {number: Event() for number in numbers}
    release = {number: Event() for number in numbers}
    review_attempts: list[dict[str, Any]] = []
    starts: list[int] = []
    errors: list[BaseException] = []

    def develop(number: int) -> None:
        if number != 2:
            starts.append(number)
            started[number].set()
            assert release[number].wait(15), f"Ticket {number} gate was not released"

    def review(number: int) -> None:
        if number != 2:
            return
        current = states.load_current_run(run_id)
        assert current is not None
        review_attempts.append(deepcopy(current["ticket_jobs"]["2"]["pending_semantic_attempt"]))
        if len(review_attempts) == 1:
            raise RuntimeError("controlled Reviewer process failure")
        starts.append(number)
        started[number].set()
        assert release[number].wait(15), "resumed Reviewer gate was not released"

    agents = TicketAgents(git_repo, numbers, develop, before_review=review)
    paused = ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
    assert paused["active_ticket_job"]["blocked_reason"] == "ticket_execution_failed"
    engine = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    )

    def run() -> None:
        try:
            engine.deliver(run_id)
        except BaseException as error:
            errors.append(error)

    control, task, worker = _bind_running_executor(git_repo, run_id)
    ownership = control.load(task)
    thread = Thread(target=run, name="active-ticket-resume-test")
    thread.start()
    try:
        assert started[3].wait(10) and started[4].wait(10), errors
        before = states.load_current_run(run_id)
        assert before is not None
        target = deepcopy(before["ticket_jobs"]["2"])
        siblings = {str(number): deepcopy(before["ticket_jobs"][str(number)]) for number in (3, 4)}
        assert target["candidate_sha"] is not None
        assert before["ticket_work_queue"][-1]["ticket_number"] == 5

        resumed = run_cli(git_repo, fixture, "resume", "1", "--ticket", "2")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        receipt = stdout_json(resumed)
        assert receipt["resume_accepted"] is True
        assert receipt["attached"] is False
        assert receipt["work_queue_position"] == 2
        assert receipt["resume_intent"]["authorization"] == "continue_ticket"
        assert receipt["resume_intent"]["pause_reason"] == "ticket_execution_failed"
        admitted = states.load_current_run(run_id)
        assert admitted is not None
        queue = deepcopy(admitted["ticket_work_queue"])
        assert [item["ticket_number"] for item in queue if item["lease_id"] is None] == [5, 2]
        assert sum(item["lease_id"] is not None for item in queue) == 2
        for key in (
            "candidate_sha", "ticket_branch_generation", "ticket_branch", "pr_number",
            "review_budget", "review_budget_history", "ci_recovery",
            "pending_semantic_attempt", "semantic_attempt_history",
            "development_thread_id", "reviewer_thread_ids",
        ):
            assert admitted["ticket_jobs"]["2"].get(key) == target.get(key), key
        assert {number: admitted["ticket_jobs"][number] for number in siblings} == siblings
        assert not started[2].is_set() and not started[5].is_set()

        repeated = run_cli(git_repo, fixture, "resume", "1", "--ticket", "2")
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert stdout_json(repeated)["attached"] is True
        assert stdout_json(repeated)["work_queue_position"] == 2
        for _ in range(2):
            status = run_cli(git_repo, fixture, "status", run_id, "--json")
            assert status.returncode == 0, status.stdout + status.stderr
            progress = stdout_json(status)["progress"]["multi_ticket"]
            detail = next(item for item in progress["tickets"] if item["ticket_number"] == 2)
            assert detail["resume_accepted"] is True
            assert detail["work_queue_position"] == 2
            assert detail["has_work_slot"] is False
        reread = states.load_current_run(run_id)
        assert reread is not None
        assert reread["ticket_work_queue"] == queue
        assert reread["ticket_jobs"]["2"]["last_ticket_resume"] == admitted["ticket_jobs"]["2"]["last_ticket_resume"]
        assert control.load(task) == ownership, "Resume created or replaced Executor ownership"
        assert worker.poll() is None and thread.is_alive()

        release[3].set()
        assert started[5].wait(10), errors
        assert not started[2].is_set() and not release[4].is_set()
        release[5].set()
        assert started[2].wait(10), errors
        assert starts.index(5) < starts.index(2)
        assert len(review_attempts) == 2
        assert review_attempts[1] == review_attempts[0]
        assert not release[4].is_set() and thread.is_alive()
        current = states.load_current_run(run_id)
        assert current is not None
        assert current["ticket_jobs"]["2"]["candidate_sha"] == target["candidate_sha"]
        assert current["ticket_jobs"]["2"]["review_budget"] == target["review_budget"]
        require_current_run_state(current)
    finally:
        for event in release.values():
            event.set()
        thread.join(15)
        worker.terminate()
        worker.wait(timeout=5)
    assert not thread.is_alive() and not errors, errors
    final = states.load_current_run(run_id)
    assert final is not None
    assert all(final["ticket_jobs"][str(number)]["phase"] == "completed" for number in numbers)
    assert final["ticket_jobs"]["2"]["review_budget"]["development_attempts"] == 1
    assert final["ticket_jobs"]["2"]["review_budget"]["reviewer_invocations"] == 1
    assert final["ticket_work_queue"] == []
