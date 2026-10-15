"""共享测试支撑：cli_payloads；每次调用保持独立状态。"""

from __future__ import annotations

from typing import Any


HUMAN_BLOCKER = (
    "GitHub denied access; tried gh issue view; grant Issue read access."
)


STANDARDS_PASS_EVIDENCE = "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。"


SPEC_PASS_EVIDENCE = "已核对的验收标准：当前交付的全部验收标准；覆盖结论：候选完整覆盖。"


def ticket() -> dict[str, Any]:
    return {
        "number": 3,
        "title": "Complete one ticket autonomously",
        "body": "Deliver the active ticket through independent acceptance.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def publication() -> dict[str, str]:
    return {
        "commit_message": "feat(delivery): complete one ticket autonomously",
        "pr_title": "feat(delivery): complete one ticket autonomously",
        "pr_body_markdown": """
## What Problem This Solves

Active tickets previously stopped before publication.

## Why This Change Was Made

A bounded delivery loop now owns the workflow.

## User Impact

The ticket reaches the Run Branch without manual mutations.

## Evidence

The scripted CLI scenario passed.
""".strip(),
    }


def final_run_publication() -> dict[str, str]:
    return {
        "commit_message": "feat(run): publish the completed delivery",
        "pr_title": "feat(run): publish the completed delivery",
        "pr_body_markdown": """
## What Problem This Solves

The completed Ticket needs one review boundary.

## Why This Change Was Made

The Run branch keeps the standard delivery route.

## User Impact

Maintainers can approve the complete Parent delivery.

## Evidence

The independent expected-merge review passed.
""".strip(),
    }


def final_run_agents() -> dict[str, object]:
    return {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": "ticket-developer",
                "summary": "Delivered the Ticket.",
                "write_files": {"feature.txt": "done\n"},
            }
        ],
        "publications": [publication()],
        "reviews": [passing_acceptance("ticket-reviewer", "Ticket passed.")],
        "run_reviews": [passing_acceptance("run-reviewer", "Run passed.")],
        "run_publications": [final_run_publication()],
    }


def passing_acceptance(thread_id: str, evidence: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "pass",
                "evidence": f"操作或命令：执行 CLI 公开验收流程；退出码：0；结果：{evidence}",
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


def repair_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "The first candidate lacks the repair.",
                "findings": [
                    "问题：修复缺失；证据：feature.txt 只有一行；必须修复：添加修复；复验：检查 feature.txt。"
                ],
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


def human_blocker_step(thread_id: str) -> dict[str, object]:
    return {
        "expected_thread_id": None,
        "thread_id": thread_id,
        "human_blockers": [HUMAN_BLOCKER],
    }


def parent_publication() -> dict[str, str]:
    return {
        "commit_message": "feat(parent): deliver the standalone parent request",
        "pr_title": "feat(parent): deliver the standalone parent request",
        "pr_body_markdown": """
## What Problem This Solves

独立的 Parent 需求此前没有可交付路径。

## Why This Change Was Made

Parent-only 流程复用候选与独立验收门禁，并保持普通合并边界。

## User Impact

维护者可以直接审查并批准一张 Parent PR。

## Evidence

脚本化 CLI 流程完成独立验收、检查和显式批准。
""".strip(),
    }


def parent_round_agents(rounds: int, *, passing_last: bool) -> dict[str, object]:
    development_steps: list[dict[str, object]] = []
    for ordinal in range(1, rounds + 1):
        step: dict[str, object] = {
            "expected_thread_id": None if ordinal == 1 else "parent-round-developer",
            "thread_id": "parent-round-developer",
            "summary": f"Completed Parent-only Development {ordinal}.",
            "write_files": {"parent-feature.txt": f"candidate-{ordinal}\n"},
        }
        if ordinal > 1:
            step["expected_files"] = {
                "parent-feature.txt": f"candidate-{ordinal - 1}\n"
            }
        development_steps.append(step)
    reviews = [
        (
            passing_acceptance(
                f"parent-round-reviewer-{ordinal}", "The final candidate passed."
            )
            if passing_last and ordinal == rounds
            else repair_acceptance(f"parent-round-reviewer-{ordinal}")
        )
        for ordinal in range(1, rounds + 1)
    ]
    return {
        "developments": development_steps,
        "publications": [parent_publication()],
        "reviews": reviews,
    }
