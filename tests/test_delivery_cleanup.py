from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery_cleanup import DeliveryCleanupEngine, remove_run_worktrees
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
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


class FailingBranchCleanupGit(GitRepository):
    def delete_managed_delivery_branch(self, branch: str) -> None:
        raise OSError(f"cannot delete {branch}")


class FailingPruneGit(GitRepository):
    def prune_worktrees(self) -> None:
        raise GitError("cannot prune worktree registry")


class RecordingBranchPublisher:
    def __init__(self) -> None:
        self.deleted_branches: list[str] = []

    def delete_managed_branch(self, branch: str) -> None:
        self.deleted_branches.append(branch)


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


def test_completed_ticket_cleanup_retries_without_reopening_delivery(
    git_repo: Path,
) -> None:
    states = StateStore(git_repo / ".agent-run")
    branch = "agent-run/run-1/ticket-3"
    git = GitRepository(git_repo)
    git.ensure_run_branch(branch, git.resolve("main"))
    state = {
        "run_id": "run-1",
        "status": "completed",
        "ticket_jobs": {},
    }
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }
    states.save_run("run-1", state)

    failed = DeliveryCleanupEngine(
        git=FailingBranchCleanupGit(git_repo), states=states
    ).complete_ticket(state, job)

    cleanup = failed["delivery_cleanup"]
    assert failed["status"] == "completed"
    assert cleanup["status"] == "cleanup_pending"
    assert cleanup["last_error"] == f"cannot delete {branch}"
    assert cleanup["items"][branch]["attempts"] == 3
    assert git.resolve(branch)

    recovered = DeliveryCleanupEngine(git=git, states=states).resume("run-1")

    assert recovered["status"] == "completed"
    assert recovered["delivery_cleanup"]["status"] == "completed"
    assert recovered["delivery_cleanup"]["items"][branch]["attempts"] == 4
    with pytest.raises(GitError):
        git.resolve(branch)


def test_cleanup_never_deletes_a_maintainer_branch(git_repo: Path) -> None:
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    branch = "maintainer/keep-me"
    git.ensure_run_branch(branch, git.resolve("main"))
    state = {"run_id": "run-1", "status": "completed", "ticket_jobs": {}}
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }

    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(
        state, job
    )

    assert result["status"] == "completed"
    assert result["delivery_cleanup"]["status"] == "cleanup_pending"
    assert git.resolve(branch)


def test_abandoned_run_never_schedules_or_retries_cleanup(git_repo: Path) -> None:
    branch = "agent-run/run-1/ticket-2"
    state = {
        "run_id": "run-1",
        "status": "abandoned",
        "ticket_jobs": {
            "2": {
                "phase": "completed",
                "ticket_number": 2,
                "ticket_branch": branch,
                "integrated_sha": "integrated",
            }
        },
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "items": {
                branch: {
                    "kind": "ticket",
                    "branch": branch,
                    "checkout": str(
                        git_repo
                        / ".agent-run"
                        / "worktrees"
                        / "run-1"
                        / "ticket-2"
                    ),
                    "attempts": 3,
                    "status": "cleanup_pending",
                }
            },
        },
    }
    states = StateStore(git_repo / ".agent-run")
    states.save_run("run-1", state)
    github = RecordingBranchPublisher()

    resumed = DeliveryCleanupEngine(
        git=GitRepository(git_repo), states=states, github=github
    ).resume("run-1")

    assert resumed == state
    assert github.deleted_branches == []


def test_abandonment_prunes_missing_worktree_registry_entry(
    git_repo: Path,
) -> None:
    git = GitRepository(git_repo)
    root = git_repo / ".agent-run" / "worktrees" / "run-1"
    checkout = root / "ticket-2"
    branch = "agent-run/run-1/ticket-2"
    git.prepare_ticket_checkout(
        branch=branch,
        base_sha=git.resolve("main"),
        checkout=checkout,
    )
    shutil.rmtree(root)
    assert str(checkout) in subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    remove_run_worktrees(git, StateStore(git_repo / ".agent-run"), "run-1")

    assert str(checkout) not in subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def test_abandonment_keeps_recovery_pending_when_prune_fails(
    git_repo: Path,
) -> None:
    with pytest.raises(GitError, match="cannot prune worktree registry"):
        remove_run_worktrees(
            FailingPruneGit(git_repo),
            StateStore(git_repo / ".agent-run"),
            "run-1",
        )
