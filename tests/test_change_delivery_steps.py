"""Real Git and persistence at the shared delivery scheduling boundary."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_loop import TicketDeliveryLoop
from agent_run.delivery_step import DeliveryStepKind
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from support.delivery_agents import PassAgents, issue
from support.delivery_publisher import ScriptedPublisher
from support.seeding import write_fixture


@pytest.mark.parametrize("conflict", [False, True])
def test_ticket_steps_yield_agent_work_then_finish_current_ci_without_agent(
    git_repo: Path, tmp_path: Path, conflict: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["pending", "pass"]

    class Agents(PassAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            current = states.load_current_run(run_id)
            assert current is not None
            owned = [item for item in current["ticket_work_queue"] if item["ticket_number"] == 3]
            assert len(owned) == 1 and owned[0]["lease_id"] is not None
            if request.get("repair_source") == "merge_conflict":
                assert owned[0]["role"] == "integration_conflict"
                self.development_requests.append(request)
                (checkout / "delivered.txt").write_text("first attempt\nexternal\n")
                return DevelopmentResult(thread_id="development-thread-1", summary="Resolved both contributions")
            return super().develop(request)

    agents = Agents(checkout)
    state = TicketDeliveryEngine(
        git=git, states=states, github=publisher, agents=agents, development_only=True,
    ).deliver(run_id)
    job = state["active_ticket_job"]
    assert job["phase"] == "ready_for_integration"
    # The complete demand entry removes its checkout; restore its managed ref.
    git.prepare_ticket_checkout(branch=job["ticket_branch"], base_sha=job["base_sha"], checkout=checkout)
    if conflict:
        external = tmp_path / "external"
        git.prepare_validation_checkout(head_sha=job["base_sha"], checkout=external)
        try:
            (external / "delivered.txt").write_text("external\n")
            source = git.commit_candidate(external, ticket_number=3, attempt=1, expected_head=job["base_sha"])
            assert source
            publisher.live_head = source
        finally:
            git.remove_worktree(external)
    loop = TicketDeliveryLoop(git=git, states=states, github=publisher, agents=agents)
    engine = loop._engine(state, job)
    assert engine.advance(state, job, checkout).kind == DeliveryStepKind.CONTINUE
    budget = deepcopy(job["review_budget"])
    recovery = deepcopy(job["ci_recovery"])
    step = engine.advance(state, job, checkout)
    assert step.kind == DeliveryStepKind.NEEDS_AGENT
    assert step.reason == ("integration_conflict" if conflict else "publication")
    assert len(agents.development_requests) == 1
    assert not agents.publication_requests
    assert job["review_budget"] == budget
    assert job["ci_recovery"] == recovery
    assert "pending_semantic_attempt" not in job
    if conflict:
        assert job["integration_conflict"]["attempts"] == 0
        assert engine.advance(state, job, checkout, allow_agent=True).kind == DeliveryStepKind.NEEDS_AGENT
        assert len(agents.development_requests) == 2
        assert not agents.publication_requests
    assert engine.advance(state, job, checkout, allow_agent=True).kind == DeliveryStepKind.CONTINUE
    assert job["phase"] == "publishing"
    assert engine.advance(state, job, checkout).kind == DeliveryStepKind.WAITING
    assert state["ticket_integration_holder"] == 3
    assert job["phase"] == "waiting_checks"
    published = job["publication_sha"]

    # Recreate engine and load canonical state: green CI needs no Agent slot.
    loaded = states.load_current_run(run_id)
    assert loaded is not None
    job = loaded["active_ticket_job"]
    step = loop._engine(loaded, job).advance(loaded, job, checkout)
    assert step.kind == DeliveryStepKind.COMPLETED
    assert job["publication_sha"] == published
    assert publisher.closed_issues == [3]
    assert publisher.merged_head == published
    assert loaded["ticket_integration_holder"] is None
    assert len(agents.publication_requests) == 1
    assert agents.review_count == 1
    assert job["review_budget"] == budget
    assert job["ci_recovery"] == recovery
    assert states.load_current_run(run_id)["active_ticket_job"]["phase"] == "completed"
