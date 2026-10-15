"""Real delivery/state/Git paths; Agent and GitHub boundaries are controlled."""

from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery_facts import multi_ticket_progress
from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
from agent_run.run_orchestration import DeliveryRunEngine
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
            saved = states.load_current_run(run_id)
            assert saved is not None
            owned = [item for item in saved["ticket_work_queue"] if item["ticket_number"] == number]
            assert len(owned) == 1 and owned[0]["lease_id"] is not None
            assert owned[0]["role"] == role
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
    errors: list[BaseException] = []

    def run() -> None:
        try:
            DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(run_id, state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
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
    finally:
        release_a.set()
        release_b.set()
        release_closeout.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert peak == 2 and active == set()
    assert all(saved["ticket_jobs"][str(n)]["phase"] == "completed" for n in (2, 3, 4))
    assert {role for _, role in calls} == {"development", "reviewer", "publication"}
    assert saved["ticket_work_queue"] == saved["ticket_integration_queue"] == []
    assert saved["ticket_integration_holder"] is None
    assert not (states.root / "worktrees" / run_id).exists()
