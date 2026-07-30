from __future__ import annotations

from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture
from test_delivery import PassAgents, ScriptedPublisher, issue


class PartialCheckoutGit(GitRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cleanup_called = False

    def prepare_ticket_checkout(
        self,
        *,
        branch: str,
        base_sha: str,
        checkout: Path,
    ) -> None:
        checkout.mkdir(parents=True)
        (checkout / "partial").write_text("leftover\n", encoding="utf-8")
        raise OSError("simulated partial checkout failure")

    def remove_worktree(self, checkout: Path) -> None:
        self.cleanup_called = True
        super().remove_worktree(checkout)


def test_partial_checkout_is_cleaned_when_preparation_fails(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = (
        git_repo
        / ".agent-run"
        / "worktrees"
        / state["run_id"]
        / "ticket-3"
    )
    partial_git = PartialCheckoutGit(git_repo)

    with pytest.raises(OSError, match="partial checkout"):
        TicketDeliveryEngine(
            git=partial_git,
            states=states,
            github=ScriptedPublisher(git_repo),
            agents=PassAgents(checkout),
        ).deliver(state["run_id"])

    assert partial_git.cleanup_called
    assert not checkout.exists()
