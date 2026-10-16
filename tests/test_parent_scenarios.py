"""独立核对 Parent-only 审批前置的真实 Git、持久化和证据适用性。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery_loop import ParentDeliveryAdapter
from agent_run.parent_integration import parent_integration_record_is_valid
from agent_run.required_checks_observation import required_checks_observation_is_current
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.parent_scenarios import prepare_approval_ready_parent
from support.seeding import write_fixture
from support.workspace import managed_repo, managed_state


def test_approval_ready_parent_has_real_git_identity_and_idle_ownership(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    prepared = prepare_approval_ready_parent(git_repo, fixture)
    run_id = str(prepared["run_id"])
    root = managed_state(git_repo)
    states = StateStore(root)
    loaded = states.load_current_run(run_id)
    assert loaded == prepared
    assert loaded is not None
    assert loaded["status"] == "parent_approval_pending"
    job = loaded["parent_job"]
    assert job["phase"] == "ready_for_approval"
    candidate, published, base = (
        job["candidate_sha"], job["publication_sha"], loaded["base"]["sha"],
    )
    assert len({candidate, published, base}) == 3
    git = GitRepository(managed_repo(git_repo))
    tree = git.resolve(f"{candidate}^{{tree}}")
    assert git.commit_parents(candidate) == git.commit_parents(published) == [base]
    assert git.resolve(f"{published}^{{tree}}") == tree
    assert git.resolve(job["parent_branch"]) == job["published_sha"] == published
    assert git.resolve(loaded["base"]["branch"]) == base
    assert subprocess.run(
        ["git", "show", f"{candidate}:parent-feature.txt"], cwd=git.root,
        check=True, capture_output=True, text=True,
    ).stdout == "done\n"
    assert not (root / "worktrees" / run_id).exists()
    assert job["parent_integration"] == {
        "candidate_sha": candidate, "candidate_tree": tree,
        "source_sha": base, "target_sha": base,
        "head_sha": candidate, "head_tree": tree, "conflict_paths": [],
    }
    assert parent_integration_record_is_valid(git, job)
    assert job["acceptance_record"] == {
        "acceptance_scope": "parent_only", "parent_number": 1,
        "effective_revision": loaded["parent"]["revision"],
        "reviewed_base_sha": base, "reviewed_candidate_sha": candidate,
        "reviewed_candidate_tree": tree, "expected_merge_tree": tree,
        "reviewer_thread_id": "parent-reviewer-1", "artifact": job["acceptance_artifact"],
    }
    github = FixtureGitHubPublisher(fixture, git)
    assert github.live_branch_heads(job["parent_branch"], "main") == (published, base)
    live = github.live_pull_request(job["pr_number"])
    assert live["head_sha"] == published
    assert live["base_sha"] == base
    assert live["head_repository"] == live["base_repository"] == loaded["repository"]
    assert live["head_branch"] == job["parent_branch"]
    assert live["base_branch"] == "main"
    assert live["state"] == "OPEN"
    assert required_checks_observation_is_current(
        job["required_checks_evidence"], pr_number=job["pr_number"],
        head_sha=published, allowed_results={"none", "pass"},
    )
    assert job["deterministic_integration_record"]["publication_sha"] == published
    assert job["merge_intent"]["attempts"] == 0
    assert "approval_grant" not in job
    # 累计历史用量不冒充本次准备真正调用过 Agent。
    assert job["modification_attempts"] == job["validation_attempts"] == 1
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert loaded["agent_invocation_history"] == []
    assert loaded["active_agent_invocation"] is None
    assert not job.get("semantic_attempt_history")
    control = TaskControlStore(root).load(TaskKey(git.root, str(loaded["repository"]), 1))
    assert control is not None
    assert control["run_id"] == run_id
    assert control["action"] is control["executor"] is None
    assert control["next_generation"] == 1

    refreshed, reused = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    assert reused
    assert refreshed["status"] == "parent_approval_pending"
    # Controller returns a current projection; its archived histories remain
    # unloaded. Every returned fact and the complete durable Job must agree.
    assert refreshed["parent_job"] == {
        key: job[key] for key in refreshed["parent_job"]
    }
    reopened = StateStore(root).load_current_run(run_id)
    assert reopened is not None
    assert reopened["parent_job"] == job


def test_approval_ready_parent_review_rejects_changed_revision_base_and_same_tree_head(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    state = prepare_approval_ready_parent(git_repo, fixture)
    git = GitRepository(managed_repo(git_repo))
    adapter = ParentDeliveryAdapter(git, FixtureGitHubPublisher(fixture, git))
    job = state["parent_job"]
    record = job["acceptance_record"]
    assert adapter.acceptance_is_current(state, job, record)

    changed_revision = deepcopy(state)
    changed_revision["parent"]["revision"] = "changed-parent-requirement"
    assert not adapter.acceptance_is_current(changed_revision, job, record)
    for field in ("base_sha", "candidate_sha"):
        changed_job = deepcopy(job)
        changed_job[field] = job["publication_sha"]
        assert not adapter.acceptance_is_current(state, changed_job, record)
        assert not parent_integration_record_is_valid(git, changed_job)
    reopened = StateStore(managed_state(git_repo)).load_current_run(str(state["run_id"]))
    assert reopened is not None
    assert reopened["parent_job"] == job
