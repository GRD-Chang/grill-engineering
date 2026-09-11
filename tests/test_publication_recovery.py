from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture
from test_delivery import PassAgents, ScriptedPublisher, issue


@pytest.mark.parametrize("remote", ["previous", "target", "unknown", "wrong_base"])
def test_resume_reconciles_known_publication_heads_and_rejects_external_changes(
    git_repo: Path, remote: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    publisher = ScriptedPublisher(git_repo)

    class Reader(FixtureGitHubReader):
        def live_pull_request(self, pr_number: int) -> dict[str, Any]:
            return publisher.live_pull_request(pr_number)

    controller = Controller(Reader(fixture), git, states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    agents = PassAgents(checkout)
    publisher.checks = ["pending"]
    engine = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    waiting = engine.deliver(run_id)
    job = waiting["active_ticket_job"]
    # Model a newer, already-authorized publication B whose previous remote A
    # remains unchanged. Publication preparation does not itself push B.
    job["published_sha"] = job["base_sha"]
    job["phase"] = "publishing"
    publisher.live_head = job["base_sha"]
    if remote == "target":
        publisher.live_head = job["publication_sha"]
    elif remote == "unknown":
        publisher.live_head = "unknown-external-head"
    elif remote == "wrong_base":
        publisher.base_branch = "main"
    states.save_run(run_id, waiting)

    resumed, _ = controller.resume(run_id)

    if remote in {"unknown", "wrong_base"}:
        assert resumed["status"] == "blocked"
        assert resumed["diagnostics"][0]["code"] == (
            "change_pr_head_changed_externally" if remote == "unknown"
            else "change_pr_base_changed_externally"
        )
    else:
        assert resumed["status"] != "blocked"
    assert resumed["active_ticket_job"]["publication_sha"] == job["publication_sha"]
    assert len(agents.publication_requests) == 1
    if remote == "previous":
        published = engine.deliver(run_id)
        assert publisher.live_head == job["publication_sha"]
        assert published["active_ticket_job"]["published_sha"] == job["publication_sha"]
        assert len(agents.development_requests) == 1
        assert len(agents.publication_requests) == 1


@pytest.mark.parametrize("intent", ["matching", "missing", "foreign"])
def test_publication_reconciles_completed_push_only_with_matching_write_intent(
    git_repo: Path, intent: str,
) -> None:
    class Publisher(ScriptedPublisher):
        writes = 0

        def verify_ticket_pr_before_publish(self, **kwargs: Any) -> None:
            if self.live_head is not None:
                if kwargs["expected_head_sha"] != self.live_head:
                    raise ValueError("remote PR identity mismatch")

        def publish_branch(
            self, branch: str, head_sha: str, *, expected_remote_sha: str
        ) -> None:
            self.writes += 1
            super().publish_branch(branch, head_sha, expected_remote_sha=expected_remote_sha)

    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    agents = PassAgents(states.root / "worktrees" / run_id / "ticket-3")
    publisher = Publisher(git_repo)
    publisher.checks = ["pending"]
    engine = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    waiting = engine.deliver(run_id)
    job = waiting["active_ticket_job"]
    job["phase"] = "publishing"
    job["published_sha"] = job["base_sha"]
    job["ticket_write_intent"] = {
        "action": "publish_ticket_ref",
        "branch": job["ticket_branch"],
        "expected_remote_sha": job["base_sha"],
        "head_sha": job["publication_sha"],
    }
    if intent == "missing":
        job.pop("ticket_write_intent")
    elif intent == "foreign":
        job["ticket_write_intent"]["branch"] = "foreign-branch"
    states.save_run(run_id, waiting)

    if intent != "matching":
        with pytest.raises(ValueError, match="remote PR identity mismatch"):
            engine.deliver(run_id)
        assert publisher.writes == 1
        assert len(agents.publication_requests) == 1
        return
    recovered = engine.deliver(run_id)

    assert recovered["active_ticket_job"]["published_sha"] == job["publication_sha"]
    assert "ticket_write_intent" not in recovered["active_ticket_job"]
    assert publisher.writes == 1
    assert len(agents.publication_requests) == 1
