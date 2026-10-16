"""独立核验有限 Ticket 前置的 Git、持久化、currentness 和首次 checks。"""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.change_currentness import stale_change_job_reason
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_loop import TicketDeliveryAdapter
from agent_run.git import GitRepository
from agent_run.state import StateStore
from agent_run.ticket_integration import integration_record_is_valid
from support.delivery_agents import PassAgents
from support.delivery_publisher import ScriptedPublisher
from support.ticket_scenarios import published_ticket


def test_published_ticket_scenario_persists_real_git_and_current_authority(
    git_repo: Path,
) -> None:
    index = (git_repo / ".git" / "index").read_bytes()
    publisher = ScriptedPublisher(git_repo)
    state, states, checkout = published_ticket(git_repo, publisher=publisher)
    reopened = StateStore(states.root).load_current_run(str(state["run_id"]))
    assert reopened == state
    job = reopened["active_ticket_job"]
    git = GitRepository(git_repo)
    candidate, publication, base = (
        job["candidate_sha"], job["publication_sha"], job["base_sha"]
    )
    tree = git.resolve(f"{candidate}^{{tree}}")
    assert candidate != publication != base
    assert tree != git.resolve(f"{base}^{{tree}}")
    assert git.commit_parents(candidate) == git.commit_parents(publication) == [base]
    assert git.resolve(f"{publication}^{{tree}}") == tree
    assert git.resolve(str(job["ticket_branch"])) == publisher.live_head == publication
    assert git.resolve(str(state["run_branch"])) == base
    assert job["publication_parent_sha"] == base
    assert job["ticket_integration"]["head_sha"] == candidate
    assert job["ticket_integration"]["candidate_tree"] == tree
    assert job["ticket_integration"]["head_tree"] == tree
    assert integration_record_is_valid(git, job)
    assert subprocess.run(
        ["git", "show", f"{publication}:delivered.txt"], cwd=git_repo,
        text=True, capture_output=True, check=True,
    ).stdout == "first attempt\n"
    assert (git_repo / ".git" / "index").read_bytes() == index
    assert not (git_repo / "delivered.txt").exists()
    assert not checkout.exists()
    assert not list(states.root.glob("ticket-scenario-*"))
    assert job["phase"] == "publishing"
    assert "required_checks_evidence" not in job
    assert "pr_number" not in job
    assert publisher.created_prs == publisher.check_position == 0
    assert state["ticket_integration_holder"] == 3
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert len(job["review_budget"]["review_artifacts"]) == 1
    assert job["development_thread_id"] == "development-thread-1"
    reviewer = job["acceptance_record"]["reviewer_thread_id"]
    assert job["reviewer_thread_ids"] == [reviewer]
    assert reviewer != job["development_thread_id"]
    assert (
        job["modification_attempts"], job["validation_attempts"], job["publication_attempts"]
    ) == (1, 1, 1)
    adapter = TicketDeliveryAdapter(git, publisher)
    assert adapter.acceptance_is_current(reopened, job, job["acceptance_record"])
    assert stale_change_job_reason(reopened, "ticket:3", job, git) is None
    stale = deepcopy(reopened)
    stale["parent"]["revision"] = "sha256:changed-parent"
    assert stale_change_job_reason(stale, "ticket:3", job, git) == (
        "ticket_requirements_changed"
    )
    changed = deepcopy(job)
    changed["candidate_sha"] = base
    assert not adapter.acceptance_is_current(reopened, changed, job["acceptance_record"])


def test_published_ticket_first_checks_are_observed_from_publishing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["pending", "pass"]
    state, states, checkout = published_ticket(git_repo, publisher=publisher)
    agents = PassAgents(checkout)
    observed: list[dict[str, Any]] = []
    snapshot = publisher.required_checks_snapshot

    def observe(pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
        persisted = StateStore(states.root).load_current_run(str(state["run_id"]))
        assert persisted is not None
        observed.append(deepcopy(persisted["active_ticket_job"]))
        return snapshot(pr_number, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(publisher, "required_checks_snapshot", observe)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents,
    )
    waiting = engine.deliver(str(state["run_id"]))
    assert waiting["status"] == "waiting_checks"
    assert len(observed) == 1
    assert observed[0]["phase"] == "publishing"
    assert "required_checks_evidence" not in observed[0]
    assert observed[0]["pr_number"] == publisher.pr_number
    assert waiting["active_ticket_job"]["required_checks_evidence"]["head_sha"] == (
        state["active_ticket_job"]["publication_sha"]
    )
    assert agents.development_requests == agents.publication_requests == []
    assert agents.review_count == 0
    completed = engine.deliver(str(state["run_id"]))
    assert completed["status"] == "ticket_completed"
    job = completed["active_ticket_job"]
    assert (
        job["modification_attempts"], job["validation_attempts"], job["publication_attempts"]
    ) == (1, 1, 1)
    assert agents.development_requests == agents.publication_requests == []
    assert agents.review_count == 0
    assert publisher.closed_issues == [3]
    assert publisher.merged_head == state["active_ticket_job"]["publication_sha"]
    reopened = StateStore(states.root).load_current_run(str(state["run_id"]))
    assert reopened is not None
    assert reopened["active_ticket_job"]["deterministic_integration_record"] == (
        job["deterministic_integration_record"]
    )
