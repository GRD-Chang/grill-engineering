"""Early global failure recording must retain concurrent Ticket progress."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state_contract import require_current_run_state
from agent_run.state import StateStore
from agent_run.operator_action_facts import operator_action_facts
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("kind", ["execution", "contradiction"])
def test_global_failure_retains_sibling_candidate_saved_during_identity_read(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = state["run_id"]
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)
    completed = states.load_current_run(run_id)["ticket_jobs"]["2"]
    assert completed["phase"] == "completed" and completed["ticket_closed_by_run"]
    entered, release = Event(), Event()
    boundary = "repository" if kind == "execution" else "repository_hint"
    original = getattr(reader, boundary)

    def identity_read() -> Any:
        entered.set()
        assert release.wait(5), "identity read was not released"
        return original()

    monkeypatch.setattr(reader, boundary, identity_read)

    def record() -> bool:
        if kind == "execution":
            return controller.record_execution_failure(run_id, "shared dispatch failure")
        return controller.record_deterministic_contradiction(
            run_id, "github_invalid_response", "repository response invalid",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recording = pool.submit(record)
        try:
            assert entered.wait(5)
            engine = ticket_engine(
                git, states, fixture, agents,
                development_only=True,
            )
            development = pool.submit(engine.deliver_ticket, run_id, 3)
            development.result(timeout=5)
            saved = states.load_current_run(run_id)
            candidate = saved["ticket_jobs"]["3"]
            assert candidate["phase"] == "ready_for_integration"
            assert candidate["candidate_sha"]
        finally:
            release.set()
        assert recording.result(timeout=5)
    failed = StateStore(states.root).load_current_run(run_id)
    assert failed["status"] == (
        "execution_failed" if kind == "execution" else "deterministic_contradiction"
    )
    assert failed["ticket_jobs"]["3"] == candidate
    assert failed["ticket_agent_invocations"]["3"] == saved["ticket_agent_invocations"]["3"]
    assert failed["ticket_jobs"]["2"] == completed
    assert failed["active_ticket_job"]["ticket_number"] == 2
    assert operator_action_facts(failed)[0]["work_subject"] == f"run:{run_id}"
    require_current_run_state(failed)
