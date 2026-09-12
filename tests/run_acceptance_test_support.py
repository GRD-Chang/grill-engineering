from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_run.semantic_attempt import canonical_fingerprint


from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore

from conftest import write_fixture

_BLOCKED_EVIDENCE = (
    "发生：GitHub 拒绝访问 Parent Issue；尝试：执行 gh issue view；人必须：授予 Issue 读取权限。"
)

_PASS_EVIDENCE = {
    "e2e": "操作或命令：执行完整 Run 验收流程；退出码：0；结果：完整 Run 通过。",
    "standards": "审查范围或基线：仓库编码规范与完整 Run diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：Parent Issue 的全部验收标准；覆盖结论：完整 Run 已覆盖。",
}


def _canonical_run_budget() -> dict[str, object]:
    return {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }

def _passing_artifact() -> dict[str, object]:
    return {
        "checks": {
            name: {"status": "pass", "evidence": _PASS_EVIDENCE[name], "findings": []}
            for name in ("e2e", "standards", "spec")
        },
    }

def _repair_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The accumulated flow loses the first Ticket behavior.",
        "findings": [
            "问题：集成流程不完整；证据：两个 Ticket 组合后端到端场景失败；必须修复：恢复完整组合流程；复验：运行完整累计场景。"
        ],
    }
    return artifact

def _candidate_finding_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["spec"] = {
        "status": "fail",
        "evidence": "The candidate still misses the refreshed Parent contract.",
        "findings": [
            "问题：Candidate 未覆盖新的 Parent 约束；证据：Spec lane 仍失败；必须修复：补齐约束并重新验证；复验：重新运行完整 Candidate Acceptance。"
        ],
    }
    return artifact

def _human_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": _BLOCKED_EVIDENCE,
        "findings": [],
    }
    return artifact

def _failed_invocation(
    *, role: str, phase: str, work_subject: str, generation: int,
    requested_thread_id: str | None, reported_thread_id: str | None,
    currentness_boundary: dict[str, object] | None = None,
    ordinal: int = 1,
) -> dict[str, object]:
    boundary = currentness_boundary or {}
    semantic_role = "reviewer" if role == "reviewer" else "publication"
    identity = {
        "role": semantic_role,
        "work_subject": work_subject,
        "generation": generation,
        "currentness_boundary_fingerprint": canonical_fingerprint(boundary),
        "ordinal": ordinal,
        "budget_window": 1 if semantic_role == "reviewer" else None,
    }
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": boundary,
        "semantic_attempt": {
            "attempt_id": canonical_fingerprint(identity),
            **identity,
            "status": "pending",
        },
        "status": "failed",
        "requested_thread_id": requested_thread_id,
        "reported_thread_id": reported_thread_id,
        "attempt_count": 1,
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:00:01+00:00",
        "error": "fixture failure",
        "return_code": 1,
        "signal": None,
    }

class ScriptedRunAgents:
    def __init__(self) -> None:
        self.development_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        # The initial Run rejection is followed by exactly one Candidate Run
        # Acceptance; promotion must not invoke a third whole-Run reviewer.
        self._reviews = [
            _repair_artifact(),
            _passing_artifact(),
        ]

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        (checkout / "run-repair.txt").write_text("repaired\n", encoding="utf-8")
        return DevelopmentResult(
            thread_id="run-repair-developer",
            summary="Repaired the accumulated behavior.",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        checkout = Path(str(request["checkout"]))
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if request.get("candidate_acceptance") is True:
            assert (checkout / "run-repair.txt").read_text(encoding="utf-8") == "repaired\n"
            assert not {
                "run_id",
                "parent",
                "ticket_graph",
                "ticket_completion_records",
                "base_sha",
                "run_head_sha",
                "candidate_sha",
                "repair_base_run_head_sha",
                "expected_merge_result",
                "acceptance_artifact",
                "ci_evidence",
                "human_feedback",
                "merge_conflict_evidence",
            }.intersection(request)
        return ReviewResult(
            thread_id=f"run-reviewer-{len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )

    def publication(self, request: dict[str, Any]) -> dict[str, str]:
        return {
            "commit_message": "fix(run): repair accumulated delivery behavior",
            "pr_title": "fix(run): repair accumulated delivery behavior",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe combined Run failed.\n\n"
                "## Why This Change Was Made\n\nThe repair restores the combined behavior.\n\n"
                "## User Impact\n\nThe complete delivery works together.\n\n"
                "## Evidence\n\nThe fresh Run validation will recheck it."
            ),
        }


class FreshCycleRunAgents(ScriptedRunAgents):
    """Use identities that cannot belong to an archived Repair Cycle."""

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        result = super().develop(request)
        return DevelopmentResult(
            thread_id="fresh-run-repair-developer",
            summary=result.summary,
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        return ReviewResult(
            thread_id=f"fresh-run-reviewer-{len(self.review_requests)}",
            artifact=result.artifact,
        )

def _completed_run(
    git_repo: Path, *, ticket_number: int = 2,
) -> tuple[dict[str, Any], StateStore, GitRepository]:
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
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
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
            "acceptance_record": {"artifact": _passing_artifact()},
            "integrated_sha": git.resolve(str(state["run_branch"])),
            "review_budget": {
                "window": 1,
                "development_attempts": 1,
                "reviewer_invocations": 0,
                "final_ci_fix_used": False,
                "review_artifacts": [],
                "checkpoint_reason": None,
            },
            "review_budget_history": [],
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
        "integrated_parents": [str(state["base"]["sha"])],
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
