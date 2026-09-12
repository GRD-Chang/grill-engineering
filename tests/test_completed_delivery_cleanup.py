from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from conftest import write_fixture
from run_acceptance_test_support import ScriptedRunAgents, _completed_run
from test_delivery import PassAgents, issue


class NewWorkAfterMergePublisher(FixtureGitHubPublisher):
    newer_head: str | None = None
    source_branch: str | None = None

    def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
        super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
        if self.newer_head is not None:
            return
        pull = next(
            pull for pull in self.data["delivery"]["pull_requests"]
            if pull.get("integrated_sha") == integrated_sha
        )
        branch = str(pull["branch"])
        head = self.git.resolve(branch)
        self.newer_head = subprocess.run(
            ["git", "commit-tree", self.git.resolve(f"{head}^{{tree}}"), "-p", head],
            input="preserve new local work\n", cwd=self.git.root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.source_branch = branch
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{branch}", self.newer_head, head],
            cwd=self.git.root, capture_output=True, check=True,
        )


@pytest.mark.parametrize("subject", ["ticket", "run_repair"])
def test_completed_delivery_finally_preserves_a_new_source_commit(
    git_repo: Path, subject: str,
) -> None:
    if subject == "run_repair":
        state, states, git = _completed_run(git_repo)
        publisher = NewWorkAfterMergePublisher(git_repo / "github.json", git)
        engine = RunAcceptanceEngine(
            git=git, states=states, github=publisher, agents=ScriptedRunAgents(),
        )
        result = engine.accept(str(state["run_id"]))
        assert result["status"] == "run_publication_pending"
        checkout = states.root / "worktrees" / state["run_id"] / "run-repair"
    else:
        fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
        git = GitRepository(git_repo)
        states = StateStore(git_repo / ".agent-run")
        state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
        checkout = states.root / "worktrees" / state["run_id"] / "ticket-3"
        publisher = NewWorkAfterMergePublisher(fixture, git)
        result = TicketDeliveryEngine(
            git=git, states=states, github=publisher, agents=PassAgents(checkout),
        ).deliver(str(state["run_id"]))
        assert result["status"] == "ticket_completed"

    assert result["delivery_cleanup"]["status"] == "cleanup_pending"
    assert publisher.newer_head is not None
    assert checkout.is_dir()
    assert git.checkout_head(checkout) == publisher.newer_head
    assert git.resolve(str(publisher.source_branch)) == publisher.newer_head
    require_current_run_state(states.load_current_run(str(state["run_id"])))
