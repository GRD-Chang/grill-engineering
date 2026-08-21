from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


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
) -> dict[str, object]:
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": currentness_boundary or {},
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

def _completed_run(git_repo: Path) -> tuple[dict[str, Any], StateStore, GitRepository]:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
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
        "2": {
            "ticket_number": 2,
            "phase": "completed",
            "development_thread_id": "ticket-developer",
            "development_thread_history": [],
            "reviewer_thread_ids": ["ticket-reviewer"],
            "modification_attempts": 1,
            "effective_revision": state["ticket_graph"]["tickets"]["2"][
                "content_revision"
            ],
            "acceptance_record": {"artifact": _passing_artifact()},
            "integrated_sha": git.resolve(str(state["run_branch"])),
        }
    }
    states.save_run(str(state["run_id"]), state)
    return state, states, git
