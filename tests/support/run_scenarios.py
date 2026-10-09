"""有限合法前置：completed Ticket 和 accepted Run，保留真实 Git 与持久化。"""
from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_run.agents import ReviewResult
from agent_run.controller import Controller
from agent_run.ci_recovery_budget import (
    new_ci_recovery_budget, observe_repairable_ci_failure,
)
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.ci_recovery_budget import new_ticket_ci_recovery_budget
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import StateStore
from support.seeding import write_fixture
from support.run_artifacts import _passing_artifact


def save_run_with_ci_fixture(
    states: StateStore, run_id: str, state: dict[str, Any]
) -> None:
    """Complete hand-built test Jobs with the canonical independent CI budget."""

    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and "phase" in job:
                job.setdefault("ci_recovery", new_ticket_ci_recovery_budget())
    for key in ("parent_job", "run_acceptance"):
        subject = state.get(key)
        if isinstance(subject, dict):
            subject.setdefault("ci_recovery", new_ci_recovery_budget())
    run = state.get("run_acceptance")
    if isinstance(run, dict):
        request = run.get("repair_request")
        ci_budget = run.get("ci_recovery")
        if (
            isinstance(request, dict)
            and request.get("repair_source") == "required_checks"
            and isinstance(ci_budget, dict)
            and ci_budget.get("developments_used") == 0
        ):
            evidence = request.get("ci_evidence")
            checks = evidence.get("checks") if isinstance(evidence, dict) else None
            first_check = checks[0] if isinstance(checks, list) and checks else None
            check_job = first_check.get("job") if isinstance(first_check, dict) else None
            head_sha = (
                evidence.get("head_sha") if isinstance(evidence, dict) else None
            ) or (check_job.get("head_sha") if isinstance(check_job, dict) else None)
            if isinstance(head_sha, str):
                observe_repairable_ci_failure(
                    run, head_sha=head_sha,
                    limit=3, is_ticket=False,
                )
    if isinstance(run, dict) and isinstance(run.get("repair_job"), dict):
        run["repair_job"].setdefault("ci_recovery", deepcopy(run["ci_recovery"]))
    states.save_run(run_id, state)


def _completed_run(
    git_repo: Path, *, ticket_number: int = 2, state_root: Path | None = None,
    delivery_policy: DeliveryPolicy | None = None,
) -> tuple[dict[str, Any], StateStore, GitRepository]:
    """构造历史 completed Ticket 及其 accepted Integration，保存真实 Run。

    后续 Run 验收测试不重跑 Ticket 生命周期；专门验证 Ticket 的测试仍走真实流程。
    """
    ticket_key = str(ticket_number)
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            ticket_key: {
                "number": ticket_number,
                "title": f"Ticket {ticket_number}",
                "body": f"Deliver ticket {ticket_number}.",
                "state": "CLOSED",
                "labels": [],
                "blocked_by": [],
            }
        },
    )
    states = StateStore(state_root if state_root is not None else git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(
        FixtureGitHubReader(fixture), git, states,
        delivery_policy=delivery_policy,
    )
    state, _ = controller.start(1)
    tree = git.resolve(f"{state['run_branch']}^{{tree}}")
    integrated = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            str(state["run_branch"]),
            "-m",
            "feat(ticket): integrated accepted ticket",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{state['run_branch']}", integrated],
        cwd=git_repo,
        check=True,
    )
    FixtureGitHubPublisher(fixture, git).sync_run_branch(
        run_branch=str(state["run_branch"]), integrated_sha=integrated,
    )
    state["active_ticket_job"] = None
    state["frontier"] = []
    state["status"] = "run_acceptance_pending"
    state["terminal_kind"] = "all_tickets_completed"
    state["ticket_jobs"] = {
        ticket_key: {
            "ticket_number": ticket_number,
            "phase": "completed",
            "development_thread_id": "ticket-developer",
            "development_thread_history": [],
            "reviewer_thread_ids": ["ticket-reviewer"],
            "modification_attempts": 1,
            "effective_revision": state["ticket_graph"]["tickets"][ticket_key][
                "content_revision"
            ],
            "integrated_sha": integrated,
            "review_budget_history": [],
            "ci_recovery": new_ticket_ci_recovery_budget(),
        }
    }
    integrated_sha = str(state["ticket_jobs"][ticket_key]["integrated_sha"])
    effective_revision = state["ticket_graph"]["tickets"][ticket_key][
        "content_revision"
    ]
    candidate_tree = git.resolve(f"{integrated_sha}^{{tree}}")
    acceptance_record = {
        "acceptance_scope": "change_job",
        "reviewed_base_sha": str(state["base"]["sha"]),
        "reviewed_candidate_sha": integrated_sha,
        "reviewed_candidate_tree": candidate_tree,
        "effective_revision": effective_revision,
        "reviewer_thread_id": "ticket-reviewer",
        "artifact": _passing_artifact(),
    }
    state["ticket_jobs"][ticket_key]["acceptance_record"] = acceptance_record
    review_artifact = {
        "reviewer_thread_id": acceptance_record["reviewer_thread_id"],
        "candidate_sha": acceptance_record["reviewed_candidate_sha"],
        "reviewed_base_sha": acceptance_record["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": acceptance_record["reviewed_base_sha"],
            "reviewed_candidate_sha": acceptance_record["reviewed_candidate_sha"],
            "reviewed_candidate_tree": acceptance_record["reviewed_candidate_tree"],
        },
        "artifact": acceptance_record["artifact"],
    }
    state["ticket_jobs"][ticket_key]["review_budget"] = {
        "window": 1,
        "development_attempts": 1,
        "reviewer_invocations": 1,
        "final_ci_fix_used": False,
        "review_artifacts": [review_artifact],
        "checkpoint_reason": None,
    }
    ticket_integration = {
        "base_sha": str(state["base"]["sha"]),
        "candidate_sha": integrated_sha,
        "candidate_tree": candidate_tree,
        "source_sha": str(state["base"]["sha"]),
        "target_sha": str(state["base"]["sha"]),
        "head_sha": integrated_sha,
        "head_tree": candidate_tree,
        "conflict_paths": [],
    }
    state["ticket_jobs"][ticket_key]["ticket_integration"] = ticket_integration
    state["ticket_jobs"][ticket_key]["deterministic_integration_record"] = {
        "source": "accepted",
        "base_sha": str(state["base"]["sha"]),
        "candidate_sha": integrated_sha,
        "candidate_tree": candidate_tree,
        "publication_sha": integrated_sha,
        "integrated_sha": integrated_sha,
        "integrated_publication_sha": integrated_sha,
        "integrated_tree": candidate_tree,
        "integrated_message": git.commit_subject(integrated_sha),
        "integrated_parents": git.commit_parents(integrated_sha),
        "effective_revision": effective_revision,
        "pr_number": 1,
        "window": 1,
        "final_ci_fix_used": False,
        "review_budget": deepcopy(state["ticket_jobs"][ticket_key]["review_budget"]),
        "required_checks_mode": "configured",
        "required_checks": "pass",
        "required_checks_evidence": {
            "pr_number": 1,
            "head_sha": integrated_sha,
            "result": "pass",
            "checks": [{"name": "fixture", "bucket": "pass"}],
        },
        "pr": {
            "number": 1,
            "state": "MERGED",
            "head_sha": integrated_sha,
            "base_sha": str(state["base"]["sha"]),
            "merge_commit_sha": integrated_sha,
        },
        "acceptance_record": acceptance_record,
        "ticket_integration": deepcopy(ticket_integration),
    }
    states.save_run(str(state["run_id"]), state)
    return state, states, git


def _sync_completed_ticket_integrated_sha(
    state: dict[str, Any], git: GitRepository, integrated_sha: str
) -> None:
    """Keep fixture Ticket state and its Integration Record at one commit."""

    job = state["ticket_jobs"]["2"]
    record = job["deterministic_integration_record"]
    parents = git.commit_parents(integrated_sha)
    base_sha = parents[0]
    job["integrated_sha"] = integrated_sha
    record.update(
        {
            "base_sha": base_sha,
            "integrated_sha": integrated_sha,
            "integrated_tree": git.resolve(f"{integrated_sha}^{{tree}}"),
            "integrated_message": git.commit_subject(integrated_sha),
            "integrated_parents": parents,
        }
    )
    record["pr"].update({"base_sha": base_sha, "merge_commit_sha": integrated_sha})
    integration = job["ticket_integration"]
    integration.update({
        "base_sha": base_sha,
        "target_sha": base_sha,
        "head_tree": git.resolve(f"{integrated_sha}^{{tree}}"),
    })
    record["ticket_integration"] = deepcopy(integration)
    authorization = record.get("acceptance_record")
    if isinstance(authorization, dict):
        authorization["reviewed_base_sha"] = base_sha
    budget = job.get("review_budget")
    if isinstance(budget, dict):
        artifacts = budget.get("review_artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, dict):
                    continue
                item["reviewed_base_sha"] = base_sha
                identity = item.get("review_identity")
                if isinstance(identity, dict) and "reviewed_base_sha" in identity:
                    identity["reviewed_base_sha"] = base_sha
        record_budget = record.get("review_budget")
        if isinstance(record_budget, dict):
            record["review_budget"] = deepcopy(budget)


class PassingRunReviewer:
    def review(self, request: dict[str, Any]) -> ReviewResult:
        del request
        return ReviewResult("run-reviewer", _passing_artifact())


def _accepted_run(
    git_repo: Path, *, ticket_number: int = 2, state_root: Path | None = None,
) -> tuple[dict[str, Any], StateStore, GitRepository, FixtureGitHubPublisher]:
    """从 completed Ticket 的真实集成通过 RunAcceptanceEngine 取得 Run 通过记录。"""
    state, states, git = _completed_run(
        git_repo, ticket_number=ticket_number, state_root=state_root,
    )
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=PassingRunReviewer(),
        github=publisher,
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    return accepted, states, git, publisher
