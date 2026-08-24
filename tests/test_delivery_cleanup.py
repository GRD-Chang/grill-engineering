from __future__ import annotations

import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery_cleanup import DeliveryCleanupEngine, remove_run_worktrees
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import DirtyManagedCheckoutError, GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture
from test_delivery import (
    E2E_PASS_EVIDENCE,
    SPEC_PASS_EVIDENCE,
    STANDARDS_PASS_EVIDENCE,
    PassAgents,
    ScriptedPublisher,
    issue,
)


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

    def remove_worktree(
        self, checkout: Path, *, discard_worktree: bool = False
    ) -> None:
        self.cleanup_called = True
        super().remove_worktree(checkout, discard_worktree=discard_worktree)


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


def test_completed_ticket_cleanup_preserves_tracked_modifications(
    git_repo: Path,
) -> None:
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    run_id = "run-dirty-tracked"
    branch = f"agent-run/{run_id}/ticket-3"
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    git.prepare_ticket_checkout(
        branch=branch,
        base_sha=git.resolve("main"),
        checkout=checkout,
    )
    (checkout / "README.md").write_text("unsaved delivery\n", encoding="utf-8")
    state = {"run_id": run_id, "status": "completed", "ticket_jobs": {}}
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }
    github = RecordingBranchPublisher()

    result = DeliveryCleanupEngine(
        git=git, states=states, github=github
    ).complete_ticket(state, job)

    cleanup = result["delivery_cleanup"]
    item = cleanup["items"][branch]
    assert cleanup["status"] == "cleanup_pending"
    assert item["status"] == "cleanup_pending"
    assert str(checkout) in item["last_error"]
    assert "tracked modifications" in item["last_error"]
    assert f"agent-run resume {run_id}" in item["last_error"]
    assert checkout.exists()
    assert git.resolve(branch)
    assert github.deleted_branches == []


def test_completed_ticket_cleanup_preserves_untracked_files(
    git_repo: Path,
) -> None:
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    run_id = "run-dirty-untracked"
    branch = f"agent-run/{run_id}/ticket-3"
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    git.prepare_ticket_checkout(
        branch=branch,
        base_sha=git.resolve("main"),
        checkout=checkout,
    )
    (checkout / "unsaved.txt").write_text("unsaved delivery\n", encoding="utf-8")
    state = {"run_id": run_id, "status": "completed", "ticket_jobs": {}}
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }

    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)

    item = result["delivery_cleanup"]["items"][branch]
    assert item["status"] == "cleanup_pending"
    assert "untracked files" in item["last_error"]
    assert checkout.exists()
    assert git.resolve(branch)


def test_completed_ticket_cleanup_removes_clean_checkout(git_repo: Path) -> None:
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    run_id = "run-clean"
    branch = f"agent-run/{run_id}/ticket-3"
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    git.prepare_ticket_checkout(
        branch=branch,
        base_sha=git.resolve("main"),
        checkout=checkout,
    )
    state = {"run_id": run_id, "status": "completed", "ticket_jobs": {}}
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }

    result = DeliveryCleanupEngine(git=git, states=states).complete_ticket(state, job)

    assert result["delivery_cleanup"]["status"] == "completed"
    assert not checkout.exists()
    with pytest.raises(GitError):
        git.resolve(branch)


def test_validation_checkout_remains_disposable_when_dirty(git_repo: Path) -> None:
    git = GitRepository(git_repo)
    checkout = git_repo / ".agent-run" / "worktrees" / "run-1" / "validation-run-1"
    git.prepare_validation_checkout(head_sha=git.resolve("main"), checkout=checkout)
    (checkout / "validation.tmp").write_text("generated\n", encoding="utf-8")

    git.remove_worktree(checkout)

    assert not checkout.exists()


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


def test_preexisting_inconsistent_ticket_checkout_is_preserved(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = states.root / "worktrees" / state["run_id"] / "ticket-3"
    checkout.mkdir(parents=True)
    evidence = checkout / "unsaved.txt"
    evidence.write_text("must survive\n", encoding="utf-8")

    with pytest.raises(GitError, match="existing ticket checkout does not match"):
        TicketDeliveryEngine(
            git=GitRepository(git_repo),
            states=states,
            github=ScriptedPublisher(git_repo),
            agents=PassAgents(checkout),
        ).deliver(state["run_id"])

    assert evidence.read_text(encoding="utf-8") == "must survive\n"


def test_completed_ticket_cleanup_retries_without_reopening_delivery(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state, _ = Controller(
        FixtureGitHubReader(fixture), git, states
    ).start(1)
    state["status"] = "completed"
    branch = f"agent-run/{state['run_id']}/ticket-3"
    git.ensure_run_branch(branch, git.resolve("main"))
    job = {
        "ticket_number": 3,
        "ticket_branch": branch,
        "phase": "completed",
        "integrated_sha": git.resolve(branch),
    }
    states.save_run(state["run_id"], state)

    failed = DeliveryCleanupEngine(
        git=FailingBranchCleanupGit(git_repo), states=states
    ).complete_ticket(state, job)

    cleanup = failed["delivery_cleanup"]
    assert failed["status"] == "completed"
    assert cleanup["status"] == "cleanup_pending"
    assert cleanup["last_error"] == f"cannot delete {branch}"
    assert cleanup["items"][branch]["attempts"] == 3
    assert git.resolve(branch)

    recovered = DeliveryCleanupEngine(git=git, states=states).resume(state["run_id"])

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
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    run_id = state["run_id"]
    branch = f"agent-run/{run_id}/ticket-2"
    state["status"] = "abandoned"
    state["ticket_jobs"]["2"].update(
        {
            "phase": "completed",
            "ticket_branch": branch,
            "integrated_sha": "integrated",
            "deterministic_integration_record": {
                "source": "accepted",
                "base_sha": "base",
                "candidate_sha": "integrated",
                "candidate_tree": "tree",
                "publication_sha": "integrated",
                "integrated_sha": "integrated",
                "integrated_publication_sha": "integrated",
                "integrated_tree": "tree",
                "integrated_message": "feat: integrated ticket",
                "integrated_parents": ["base"],
                "effective_revision": "ticket-2-revision",
                "pr_number": 1,
                "window": 1,
                "final_ci_fix_used": False,
                "required_checks_mode": "configured",
                "required_checks": "pass",
                "required_checks_evidence": {
                    "pr_number": 1,
                    "head_sha": "integrated",
                    "result": "pass",
                    "checks": [{"name": "fixture", "bucket": "pass"}],
                },
                "pr": {
                    "number": 1,
                    "state": "MERGED",
                    "head_sha": "integrated",
                    "base_sha": "base",
                    "merge_commit_sha": "integrated",
                },
                "acceptance_record": {
                    "acceptance_scope": "change_job",
                    "reviewed_base_sha": "base",
                    "reviewed_candidate_sha": "integrated",
                    "reviewed_candidate_tree": "tree",
                    "effective_revision": "ticket-2-revision",
                    "reviewer_thread_id": "ticket-reviewer",
                    "artifact": {
                        "checks": {
                            "e2e": {
                                "status": "pass",
                                "evidence": E2E_PASS_EVIDENCE,
                                "findings": [],
                            },
                            "standards": {
                                "status": "pass",
                                "evidence": STANDARDS_PASS_EVIDENCE,
                                "findings": [],
                            },
                            "spec": {
                                "status": "pass",
                                "evidence": SPEC_PASS_EVIDENCE,
                                "findings": [],
                            },
                        }
                    },
                },
            },
            "review_budget": {
                "window": 1,
                "development_attempts": 0,
                "reviewer_invocations": 0,
                "final_ci_fix_used": False,
                "review_artifacts": [],
                "checkpoint_reason": None,
            },
            "review_budget_history": [],
        }
    )
    cleanup_job = state["ticket_jobs"]["2"]
    cleanup_record = cleanup_job["deterministic_integration_record"]
    cleanup_acceptance = cleanup_record["acceptance_record"]
    cleanup_review_artifact = {
        "reviewer_thread_id": cleanup_acceptance["reviewer_thread_id"],
        "candidate_sha": cleanup_acceptance["reviewed_candidate_sha"],
        "reviewed_base_sha": cleanup_acceptance["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": cleanup_acceptance["reviewed_base_sha"],
            "reviewed_candidate_sha": cleanup_acceptance[
                "reviewed_candidate_sha"
            ],
            "reviewed_candidate_tree": cleanup_acceptance[
                "reviewed_candidate_tree"
            ],
        },
        "artifact": cleanup_acceptance["artifact"],
    }
    cleanup_job["review_budget"] = {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 1,
        "final_ci_fix_used": False,
        "review_artifacts": [cleanup_review_artifact],
        "checkpoint_reason": None,
    }
    cleanup_record["review_budget"] = deepcopy(cleanup_job["review_budget"])
    state["delivery_cleanup"] = {
        "status": "cleanup_pending",
        "items": {
            branch: {
                "kind": "ticket",
                "branch": branch,
                "checkout": str(
                    git_repo / ".agent-run" / "worktrees" / run_id / "ticket-2"
                ),
                "attempts": 3,
                "status": "cleanup_pending",
            }
        },
    }
    states.save_run(run_id, state)
    github = RecordingBranchPublisher()

    resumed = DeliveryCleanupEngine(
        git=GitRepository(git_repo), states=states, github=github
    ).resume(run_id)

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


def test_abandonment_preflight_reports_every_dirty_managed_checkout(
    git_repo: Path,
) -> None:
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    root = states.root / "worktrees" / "run-1"
    checkouts = [root / "ticket-2", root / "run-repair"]
    branches = ["agent-run/run-1/ticket-2", "agent-run-repair/run-1/run"]
    for checkout, branch in zip(checkouts, branches, strict=True):
        git.prepare_ticket_checkout(
            branch=branch,
            base_sha=git.resolve("main"),
            checkout=checkout,
        )
        (checkout / "unsaved.txt").write_text("unsaved\n", encoding="utf-8")

    with pytest.raises(DirtyManagedCheckoutError) as caught:
        remove_run_worktrees(git, states, "run-1")

    assert all(str(checkout) in str(caught.value) for checkout in checkouts)
    assert all(checkout.exists() for checkout in checkouts)

    remove_run_worktrees(git, states, "run-1", discard_worktree=True)

    assert all(not checkout.exists() for checkout in checkouts)


def test_managed_checkout_with_missing_git_metadata_is_preserved(
    git_repo: Path,
) -> None:
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    checkout = states.root / "worktrees" / "run-1" / "ticket-2"
    git.prepare_ticket_checkout(
        branch="agent-run/run-1/ticket-2",
        base_sha=git.resolve("main"),
        checkout=checkout,
    )
    (checkout / "unsaved.txt").write_text("unsaved\n", encoding="utf-8")
    (checkout / ".git").unlink()

    with pytest.raises(
        DirtyManagedCheckoutError, match="Git metadata is missing or inconsistent"
    ):
        remove_run_worktrees(git, states, "run-1")

    assert (checkout / "unsaved.txt").is_file()

    remove_run_worktrees(git, states, "run-1", discard_worktree=True)

    assert not checkout.exists()


def test_abandonment_keeps_recovery_pending_when_prune_fails(
    git_repo: Path,
) -> None:
    with pytest.raises(GitError, match="cannot prune worktree registry"):
        remove_run_worktrees(
            FailingPruneGit(git_repo),
            StateStore(git_repo / ".agent-run"),
            "run-1",
        )
