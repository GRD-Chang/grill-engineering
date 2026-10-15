"""Real delivery/state/Git paths; Agent and GitHub boundaries are controlled."""

from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery_facts import multi_ticket_progress
from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import ConcurrentRunStateError
from agent_run.task_control import TaskControlBusyError
from support.ticket_execution import TicketExecution
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("readiness", ["ready", "dependency", "program_closeout"])
def test_fifo_refills_while_sibling_runs_and_all_roles_share_slots(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, readiness: str,
) -> None:
    import agent_run.delivery as delivery

    monkeypatch.setattr(delivery, "SUPERVISION_POLL_SECONDS", 0.02)
    dependent = readiness == "dependency"
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), workers=2, blocked_by={4: [2]} if dependent else None)
    run_id = state["run_id"]
    release_a, release_b, entered_a, entered_b, entered_c = (Event() for _ in range(5))
    closeout, release_closeout = Event(), Event()
    publication_with_sibling, completed_one = Event(), Event()
    original_revision = FixtureGitHubPublisher.current_effective_revision

    def revision(publisher: FixtureGitHubPublisher, **kwargs: Any) -> str:
        current = states.load_current_run(run_id)
        if (
            readiness == "program_closeout" and kwargs["ticket_number"] == 2
            and current["ticket_jobs"].get("2", {}).get("phase") == "committing_candidate"
            and not closeout.is_set()
        ):
            closeout.set()
            assert release_closeout.wait(10)
        return original_revision(publisher, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, "current_effective_revision", revision)
    lock = Lock()
    active: set[int] = set()
    calls: list[tuple[int, str]] = []
    peak = 0

    def develop(number: int) -> None:
        if number == 2:
            entered_a.set()
            assert release_a.wait(10)
        elif number == 3:
            entered_b.set()
            assert release_b.wait(10)
        else:
            entered_c.set()

    agents = TicketAgents(git_repo, (2, 3, 4), develop)

    def track(method: Any, role: str) -> Any:
        def call(request: dict[str, Any]) -> Any:
            nonlocal peak
            number = int(request["ticket"]["number"])
            with lock:
                assert number not in active
                active.add(number)
                peak = max(peak, len(active))
                assert len(active) <= 2
                calls.append((number, role))
                shared_publication_slot = (
                    role == "publication" and number != 3
                    and 3 in active and not release_b.is_set()
                )
            saved = states.load_current_run(run_id)
            assert saved is not None
            owned = [item for item in saved["ticket_work_queue"] if item["ticket_number"] == number]
            assert len(owned) == 1 and owned[0]["lease_id"] is not None
            assert owned[0]["role"] == role
            if shared_publication_slot:
                publication_with_sibling.set()
            try:
                return method(request)
            finally:
                with lock:
                    active.remove(number)
        return call

    for method, role in (("develop", "development"), ("review", "reviewer"), ("publication", "publication")):
        monkeypatch.setattr(agents, method, track(getattr(agents, method), role))
    engine = ticket_engine(git, states, fixture, agents)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    refresh_pending = False

    def supervise(lanes: frozenset[int]) -> None:
        nonlocal refresh_pending
        if not completed_one.is_set() and engine.supervise(run_id, lanes):
            completed_one.set()
            refresh_pending = True
        if refresh_pending:
            try:
                controller.resume(run_id)
            except TaskControlBusyError as error:
                if not isinstance(error.__cause__, ConcurrentRunStateError):
                    raise
            else:
                refresh_pending = False

    def run() -> None:
        # One real integration proves Publication shares slots with blocked
        # development. Other Tickets stop at the existing stage boundary.
        assert engine.develop_frontier(run_id, state, supervise=supervise)

    def release_all() -> None:
        release_a.set()
        release_b.set()
        release_closeout.set()

    with TicketExecution(run, release=release_all, name="ticket-work-scheduling-test") as execution:
        errors, thread = execution.errors, execution.thread
        assert entered_a.wait(10) and entered_b.wait(10), errors
        saved = states.load_current_run(run_id)
        assert saved is not None
        progress = multi_ticket_progress(saved, executor_activity="running")
        assert progress is not None and progress["work_slots_active"] == 2
        assert progress["tickets"][2]["work_queue_position"] == (None if dependent else 1)
        assert not entered_c.is_set()
        release_a.set()
        if readiness == "program_closeout":
            assert closeout.wait(10), errors
        assert entered_c.wait(10), errors
        assert not release_closeout.is_set()
        assert not release_b.is_set() and thread.is_alive()
        with lock:
            if not dependent:
                assert (2, "reviewer") not in calls[:calls.index((4, "development"))]
            else:
                completed = states.load_current_run(run_id)
                assert completed["ticket_jobs"]["2"]["phase"] == "completed"
        # Verify each role's real durable lease while another Ticket still
        # occupies capacity, before permitting the synchronous closeout.
        release_closeout.set()
        assert publication_with_sibling.wait(10), errors
        with lock:
            assert {role for _, role in calls} == {"development", "reviewer", "publication"}
        assert not release_b.is_set() and thread.is_alive()
        execution.finish(timeout=10)
    assert completed_one.is_set()
    DeliveryRunEngine(controller=controller, tickets=engine).deliver(run_id)
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert peak == 2 and active == set()
    assert all(saved["ticket_jobs"][str(n)]["phase"] == "completed" for n in (2, 3, 4))
    assert {role for _, role in calls} == {"development", "reviewer", "publication"}
    assert saved["ticket_work_queue"] == saved["ticket_integration_queue"] == []
    assert saved["ticket_integration_holder"] is None
    assert not (states.root / "worktrees" / run_id).exists()
