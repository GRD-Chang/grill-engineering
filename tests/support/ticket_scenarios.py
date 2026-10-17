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
    state, git = _reviewed_ticket(git_repo, fixture, states, ticket_number=3)
    job = state["active_ticket_job"]
    base = str(job["base_sha"])
    publication = subprocess.run(
        ["git", "commit-tree", git.resolve(f"{job['candidate_sha']}^{{tree}}"),
         "-p", base, "-m", "feat(delivery): publish historical ticket candidate"],
        cwd=git_repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    job.update({
        "phase": "publishing",
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
    return _save_prepared_ticket(state, states, git, head=publication, publisher=publisher)


def accepted_ticket(
    git_repo: Path, *, fixture: Path, states: StateStore, ticket_number: int,
) -> tuple[dict[str, Any], StateStore, Path]:
    """在调用方需求上准备已审核 Candidate；Publication 仍由被测 Engine 执行。

    仅供 CLI 的单 Ticket #2/#3 场景，不改写 fixture 的需求、故障或历史，
    不构造 Invocation、Semantic Attempt、发布产物、远端 ref 或 PR。
    """
    assert ticket_number in {2, 3}
    state, git = _reviewed_ticket(git_repo, fixture, states, ticket_number=ticket_number)
    return _save_prepared_ticket(
        state, states, git, head=str(state["active_ticket_job"]["candidate_sha"]),
    )


def _reviewed_ticket(
    git_repo: Path, fixture: Path, states: StateStore, *, ticket_number: int,
) -> tuple[dict[str, Any], GitRepository]:
    """复用同一真实 Candidate/审核历史，不保存任何发布阶段事实。"""
    git = GitRepository(git_repo)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    job = state["active_ticket_job"]
    key = str(ticket_number)
    assert job["ticket_number"] == ticket_number
    assert set(state["ticket_graph"]["tickets"]) == {key}
    base = git.resolve(str(state["run_branch"]))
    branch = f"agent-run/{state['run_id']}/ticket-{ticket_number}"
    candidate = _candidate_commit(git_repo, states.root, base, ticket_number)
    tree = git.resolve(f"{candidate}^{{tree}}")
    revision = effective_revision(
        ticket_revision=state["ticket_graph"]["tickets"][key]["content_revision"],
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
        "phase": "accepted",
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
    })
    observe_integration_snapshot(
        job, source_sha=base, target_sha=base,
        policy_snapshot=job["policy_snapshot"],
    )
    complete_integration_window(job)
    state["ticket_jobs"][key] = job
    state["ticket_integration_holder"] = ticket_number
    return state, git


def _save_prepared_ticket(
    state: dict[str, Any], states: StateStore, git: GitRepository, *, head: str,
    publisher: ScriptedPublisher | None = None,
) -> tuple[dict[str, Any], StateStore, Path]:
    job = state["active_ticket_job"]
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{job['ticket_branch']}", head],
        cwd=git.root, check=True, capture_output=True,
    )
    if publisher is not None:
        publisher.live_head = head
    persist_run_fixture(states, str(state["run_id"]), state)
    checkout = states.root / "worktrees" / str(state["run_id"]) / f"ticket-{job['ticket_number']}"
    return state, states, checkout


def _candidate_commit(
    repository: Path, state_root: Path, base: str, ticket_number: int,
) -> str:
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
            f"chore(ticket-{ticket_number}): candidate 1", env=environment,
        )
    return candidate
