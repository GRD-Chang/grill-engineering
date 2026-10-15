"""共享测试支撑：delivery_agents；每次调用保持独立状态。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from agent_run.agents import DevelopmentResult, ReviewResult


E2E_PASS_EVIDENCE = "操作或命令：执行公开候选流程；退出码：0；结果：候选通过端到端复验。"


E2E_FAIL_EVIDENCE = "执行公开候选流程后，delivered.txt 缺少修复标记。"


STANDARDS_PASS_EVIDENCE = "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。"


SPEC_PASS_EVIDENCE = "已核对的验收标准：Ticket 的全部验收标准；覆盖结论：候选完整覆盖。"


def issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Deliver one ticket",
        "body": "Implement the requested behavior.\n\n## Acceptance criteria\n\n- It works.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


class ScriptedAgents:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.development_requests: list[dict[str, Any]] = []
        self.publication_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        self.development_thread_ids: list[str | None] = []
        self.reviewer_thread_ids: list[str] = []
        self.publication_diffs: list[str] = []
        self.validation_checkouts: list[Path] = []
        self.events: list[str] = []
        self.review_count = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.events.append("develop")
        self.development_requests.append(request)
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert request["head_sha"] == actual_head
        self.development_thread_ids.append(request.get("thread_id"))
        thread_id = str(request.get("thread_id") or "development-thread-1")
        target = self.checkout / "delivered.txt"
        text = "first attempt\n" if not target.exists() else target.read_text()
        if request.get("acceptance_artifact"):
            text += "repair applied\n"
        target.write_text(text, encoding="utf-8")
        return DevelopmentResult(
            thread_id=thread_id,
            summary="Implemented and tested the active ticket.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("publication")
        self.publication_requests.append(request)
        diff = subprocess.run(
            [
                "git",
                "diff",
                str(request["base_sha"]),
                str(request["candidate_sha"]),
            ],
            cwd=Path(str(request["checkout"])),
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.publication_diffs.append(diff)
        return {
            "commit_message": "feat(delivery): complete one ticket autonomously",
            "pr_title": "feat(delivery): complete one ticket autonomously",
            "pr_body_markdown": """
## What Problem This Solves

The ticket stopped before publication.

## Why This Change Was Made

The delivery loop now owns the bounded workflow.

## User Impact

The active ticket reaches the Run Branch automatically.

## Evidence

The scripted end-to-end scenario passed.
""".strip(),
        }

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.events.append("review")
        self.review_requests.append(request)
        validation_checkout = Path(str(request["checkout"]))
        self.validation_checkouts.append(validation_checkout)
        assert validation_checkout != self.checkout
        reviewed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=validation_checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert reviewed_head == request["candidate_sha"]
        assert "publication_sha" not in request
        assert "publication" not in request
        (validation_checkout / "validation.tmp").write_text(
            "temporary validation evidence\n", encoding="utf-8"
        )
        reviewer_id = f"reviewer-{self.review_count + 1}"
        self.reviewer_thread_ids.append(reviewer_id)
        self.review_count += 1
        common = {
            "checks": {
                "e2e": {
                    "status": "pass" if self.review_count == 2 else "fail",
                    "evidence": (
                        E2E_PASS_EVIDENCE
                        if self.review_count == 2
                        else E2E_FAIL_EVIDENCE
                    ),
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
            },
        }
        if self.review_count == 1:
            artifact = {
                **common,
            }
            artifact["checks"]["e2e"]["findings"] = [
                "问题：修复标记缺失；证据：delivered.txt 只有第一次尝试内容；必须修复：应用修复；复验：检查 delivered.txt。"
            ]
        else:
            artifact = common
        return ReviewResult(thread_id=reviewer_id, artifact=artifact)


class PassAgents(ScriptedAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        validation_checkout = Path(str(request["checkout"]))
        assert validation_checkout != self.checkout
        assert (validation_checkout / "delivered.txt").is_file()
        self.review_count += 1
        return ReviewResult(
            thread_id=f"reviewer-{self.review_count}",
            artifact={
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
                },
            },
        )
