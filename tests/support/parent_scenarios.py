"""Parent-only 审批规则的有限合法前置，保留真实 Git、SQLite 和 CLI ownership。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Any

from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.integration_resync_budget import (
    complete_integration_window,
    observe_integration_snapshot,
)
from agent_run.parent_delivery_loop import ParentDeliveryAdapter
from agent_run.review_budget import new_budget
from agent_run.state import StateStore
from support.cli import stdout_json
from support.cli_payloads import parent_publication
from support.seeding import seed_run
from support.state import persist_run_fixture
from support.workspace import managed_repo, managed_state


def _git(root: Path, *arguments: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, input=input_text,
        text=True, capture_output=True, check=True,
    ).stdout.strip()


def _historical_commits(
    git: GitRepository, base: str, branch: str, publication: dict[str, str],
) -> tuple[str, str, str]:
    """首轮 Publication 与 Candidate 同 tree，分别以固定开发基线为 parent。"""
    blob = _git(git.root, "hash-object", "-w", "--stdin", input_text="done\n")
    entries = _git(git.root, "ls-tree", base)
    assert "\tparent-feature.txt" not in entries
    tree = _git(
        git.root, "mktree",
        input_text=f"{entries}\n100644 blob {blob}\tparent-feature.txt\n",
    )
    candidate = _git(
        git.root, "commit-tree", tree, "-p", base,
        "-m", "chore(ticket-0): candidate 1",
    )
    published = _git(
        git.root, "commit-tree", tree, "-p", base,
        "-m", publication["commit_message"],
    )
    _git(git.root, "update-ref", f"refs/heads/{branch}", published, base)
    return candidate, published, tree


def prepare_approval_ready_parent(repo: Path, fixture: Path) -> dict[str, Any]:
    """准备一轮已验收、已发布且尚未批准的 Parent-only。

    只接受本例新建的零 Child fixture；历史开发、审核和发布是明确构造的
    累计事实，不补造 Agent 调用或 Semantic Attempt 历史。本轮 approve /
    resume 仍由真实 CLI 执行；完整前序由专门的完整交付与配对轮次测试覆盖。
    不缓存可变状态，不创建额外 checkout，也不授予 Approval Grant。
    """
    seeded = seed_run(repo, fixture, idle_control=True, reuse_existing=False)
    assert seeded.returncode == 0, seeded.stderr
    run_id = str(stdout_json(seeded)["run_id"])
    states = StateStore(managed_state(repo))
    state = states.load_run(run_id)
    assert state is not None
    assert state["delivery_type"] == "parent_only"
    assert not state.get("parent_job")
    git = GitRepository(managed_repo(repo))
    base = str(state["base"]["sha"])
    branch = str(state["parent_branch"])
    publication = parent_publication()
    candidate, published, tree = _historical_commits(git, base, branch, publication)
    github = FixtureGitHubPublisher(fixture, git)
    github.ensure_parent_branch(
        parent_number=1, branch=branch, base_branch=str(state["base"]["branch"]),
    )
    github.publish_branch(branch, published, expected_remote_sha=base)
    pr_number = github.ensure_change_pr(
        branch=branch, base_branch=str(state["base"]["branch"]),
        title=publication["pr_title"], body=publication["pr_body_markdown"],
        expected_head_sha=published, expected_base_sha=base,
    )
    checks = github.required_checks_snapshot(pr_number, expected_head_sha=published)
    assert checks["result"] in {"none", "pass"}
    artifact = {"checks": {
        lane: {
            "status": "pass", "findings": [],
            "evidence": f"历史审批前置：Parent-only Candidate 的 {lane} 验收已通过。",
        }
        for lane in ("e2e", "standards", "spec")
    }}
    job: dict[str, Any] = {
        "run_id": run_id,
        "parent_branch": branch,
        "base_sha": base,
        "effective_revision": state["parent"]["revision"],
        "parent_generation": 1,
        "phase": "ready_for_approval",
        "modification_attempts": 1,
        "development_thread_id": "parent-developer-1",
        "development_thread_history": [],
        "reviewer_thread_ids": ["parent-reviewer-1"],
        "validation_attempts": 1,
        "candidate_sha": candidate,
        "last_review_candidate_sha": candidate,
        "acceptance_artifact": artifact,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget_history": [],
        "ci_recovery": new_ci_recovery_budget(),
        "parent_integration": {
            "candidate_sha": candidate, "candidate_tree": tree,
            "source_sha": base, "target_sha": base,
            "head_sha": candidate, "head_tree": tree, "conflict_paths": [],
        },
        "publication": publication,
        "publication_attempts": 1,
        "publication_sha": published,
        "publication_parent_sha": base,
        "publication_prior_published_sha": base,
        "published_sha": published,
        "pr_number": pr_number,
        "required_checks_evidence": checks,
        "linked_branch_display": {"display_attempted": True, "status": "linked"},
        "merge_intent": {
            "head_sha": published, "base_branch": state["base"]["branch"],
            "base_sha": base, "commit_message": publication["commit_message"],
            "effective_revision": state["parent"]["revision"], "attempts": 0,
        },
    }
    adapter = ParentDeliveryAdapter(git, github)
    acceptance = adapter.acceptance_record(state, job, "parent-reviewer-1", artifact)
    job["acceptance_record"] = acceptance
    job["review_budget"] = new_budget(
        development_attempts=1, reviewer_invocations=1,
        review_artifacts=[{
            "reviewer_thread_id": "parent-reviewer-1",
            "candidate_sha": candidate, "reviewed_base_sha": base,
            "review_identity": adapter.review_identity(state, job),
            "artifact": deepcopy(artifact),
        }],
    )
    observe_integration_snapshot(
        job, source_sha=base, target_sha=base,
        policy_snapshot=job["policy_snapshot"],
    )
    complete_integration_window(job)
    live = github.live_pull_request(pr_number)
    job["deterministic_integration_record"] = {
        "source": "accepted", "policy_snapshot": deepcopy(job["policy_snapshot"]),
        "pr_number": pr_number, "base_sha": base, "candidate_sha": candidate,
        "candidate_tree": tree, "publication_sha": published,
        "required_checks_evidence": deepcopy(checks),
        "pr": {"number": pr_number, **{
            key: live[key] for key in (
                "state", "head_branch", "head_sha", "head_repository",
                "base_branch", "base_sha", "base_repository", "mergeable",
            )
        }},
        "final_ci_fix_used": False, "window": 1,
        "review_budget": deepcopy(job["review_budget"]),
        "effective_revision": job["effective_revision"],
        "acceptance_record": deepcopy(acceptance),
    }
    state.update(
        parent_job=job, status="parent_approval_pending",
        terminal_kind="waiting_human", diagnostics=[],
    )
    persist_run_fixture(states, run_id, state)
    loaded = states.load_current_run(run_id)
    assert loaded is not None
    return loaded
