"""有限的 Ticket 发布前置；保留真实 Git、审核授权与 SQLite 状态。"""

from __future__ import annotations

import os
import subprocess
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_run.ci_recovery_budget import new_ticket_ci_recovery_budget
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.integration_resync_budget import (
    complete_integration_window,
    observe_integration_snapshot,
)
from agent_run.revisions import effective_revision
from agent_run.review_budget import new_budget
from agent_run.state import StateStore
from support.delivery_agents import (
    E2E_PASS_EVIDENCE,
    SPEC_PASS_EVIDENCE,
    STANDARDS_PASS_EVIDENCE,
    issue,
)
from support.delivery_publisher import ScriptedPublisher
from support.seeding import write_fixture
from support.state import persist_run_fixture


def published_ticket(
    git_repo: Path,
    *,
    publisher: ScriptedPublisher,
    states: StateStore | None = None,
) -> tuple[dict[str, Any], StateStore, Path]:
    """构造 Ticket 3 分支已发布、尚未建 PR 或首次读取 checks 的起点。

    初始开发和独立审核由专门的全流程用例覆盖。这里显式保存一次历史
    开发、审核及发布文案的累计事实，不调用 Agent，也不修改 fake 的调用
    计数或请求历史。调用方仍通过真实 TicketDeliveryEngine 建 PR、读取
    首次 checks 并继续交付；不接受任意阶段或状态 overrides。
    """
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    if states is None:
        states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    job = state["active_ticket_job"]
    base = git.resolve(str(state["run_branch"]))
    branch = f"agent-run/{state['run_id']}/ticket-3"
    candidate, publication = _published_commits(git_repo, states.root, base)
    tree = git.resolve(f"{candidate}^{{tree}}")
    revision = effective_revision(
        ticket_revision=state["ticket_graph"]["tickets"]["3"]["content_revision"],
        parent_revision=state["parent"]["revision"],
        graph_revision=state["ticket_graph"]["revision"],
    )
    artifact = {
        "checks": {
            name: {"status": "pass", "evidence": evidence, "findings": []}
            for name, evidence in (
                ("e2e", E2E_PASS_EVIDENCE),
                ("standards", STANDARDS_PASS_EVIDENCE),
                ("spec", SPEC_PASS_EVIDENCE),
            )
        }
    }
    identity = {
        "reviewed_base_sha": base,
        "reviewed_candidate_sha": candidate,
        "reviewed_candidate_tree": tree,
    }
    reviewer = "historical-ticket-reviewer"
    job.update({
        "ticket_branch": branch,
        "ticket_branch_generation": 1,
        "ticket_branch_initialized": True,
        "base_sha": base,
        "effective_revision": revision,
        "phase": "publishing",
        "candidate_sha": candidate,
        "modification_attempts": 1,
        "code_modification_attempts": 1,
        "attempt_kind": "ordinary",
        "development_thread_id": "development-thread-1",
        "development_thread_history": [],
        "reviewer_thread_ids": [reviewer],
        "validation_attempts": 1,
        "last_review_candidate_sha": candidate,
        "acceptance_artifact": artifact,
        "acceptance_record": {
            "acceptance_scope": "change_job",
            **identity,
            "effective_revision": revision,
            "reviewer_thread_id": reviewer,
            "artifact": deepcopy(artifact),
        },
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": new_budget(
            development_attempts=1,
            reviewer_invocations=1,
            review_artifacts=[{
                "reviewer_thread_id": reviewer,
                "candidate_sha": candidate,
                "reviewed_base_sha": base,
                "review_identity": identity,
                "artifact": artifact,
            }],
        ),
        "review_budget_history": [],
        "ci_recovery": new_ticket_ci_recovery_budget(),
        "ticket_integration": {
            "base_sha": base,
            "candidate_sha": candidate,
            "candidate_tree": tree,
            "source_sha": base,
            "target_sha": base,
            "head_sha": candidate,
            "head_tree": tree,
            "conflict_paths": [],
        },
        "publication_attempts": 1,
        "publication": {
            "commit_message": "feat(delivery): publish historical ticket candidate",
            "pr_title": "feat(delivery): publish historical ticket candidate",
            "pr_body_markdown": "Historical Ticket publication prepared for Required Checks.",
        },
        "publication_sha": publication,
        "publication_parent_sha": base,
        "publication_prior_published_sha": base,
        "published_sha": publication,
    })
    observe_integration_snapshot(
        job, source_sha=base, target_sha=base,
        policy_snapshot=job["policy_snapshot"],
    )
    complete_integration_window(job)
    state["ticket_jobs"]["3"] = job
    state["ticket_integration_holder"] = 3
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{branch}", publication],
        cwd=git_repo, check=True, capture_output=True,
    )
    publisher.live_head = publication
    persist_run_fixture(states, str(state["run_id"]), state)
    checkout = states.root / "worktrees" / str(state["run_id"]) / "ticket-3"
    return state, states, checkout


def _published_commits(repository: Path, state_root: Path, base: str) -> tuple[str, str]:
    """用独立临时 index 写入真实 tree，不改工作区或用户 index。"""
    def run(*arguments: str, env: dict[str, str], input: str | None = None) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=repository, env=env, input=input,
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    with TemporaryDirectory(prefix="ticket-scenario-", dir=state_root) as temporary:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"))
        run("read-tree", base, env=environment)
        blob = run("hash-object", "-w", "--stdin", env=environment, input="first attempt\n")
        run(
            "update-index", "--add", "--cacheinfo",
            f"100644,{blob},delivered.txt", env=environment,
        )
        tree = run("write-tree", env=environment)
        candidate = run(
            "commit-tree", tree, "-p", base, "-m",
            "chore(ticket-3): candidate 1", env=environment,
        )
        publication = run(
            "commit-tree", tree, "-p", base, "-m",
            "feat(delivery): publish historical ticket candidate", env=environment,
        )
    return candidate, publication
