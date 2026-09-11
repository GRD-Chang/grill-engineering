#!/usr/bin/env python3
"""使用示例任务展示当前源码实际生成的各阶段 Prompt。仅需 Python 3.11+。"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from agent_run.development_prompts import development_prompt
from agent_run.prompt_context import structured_output_repair_prompt
from agent_run.publication_prompts import publication_prompt
from agent_run.reviewer_prompts import review_prompt


def inline_probe(filename: str, method_name: str) -> str:
    """只读取两处内联字面量，不导入或执行会启动 Codex 的 Backend。"""
    source = ROOT / "src" / "agent_run" / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method, = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    values: list[ast.expr] = []
    for node in ast.walk(method):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "prompt"
            for target in node.targets
        ):
            values.append(node.value)
        elif isinstance(node, ast.keyword) and node.arg == "prompt":
            values.append(node.value)
    if len(values) != 1 or not isinstance(values[0], ast.Constant):
        raise ValueError(f"{filename}:{method_name} 的 Prompt 已非单个字面量，请更新预览入口")
    value = values[0].value
    if not isinstance(value, str):
        raise ValueError(f"{filename}:{method_name} 的 Prompt 不是字符串")
    return value


def scenarios(parent_url: str, task_url: str) -> dict[str, tuple[str, str]]:
    previews: dict[str, tuple[str, str]] = {}

    def add(name: str, title: str, text: str) -> None:
        previews[name] = (title, text)

    task = {
        "acceptance_scope": "ticket",
        "parent_issue_url": parent_url,
        "task_issue_url": task_url,
    }
    parent = {"acceptance_scope": "parent_only", "parent_issue_url": parent_url}
    overall = {"acceptance_scope": "run", "parent_issue_url": parent_url}
    passed = {"checks": {
        lane: {"status": "pass", "evidence": "【示例】此处为本轮实际验证证据。", "findings": []}
        for lane in ("e2e", "standards", "spec")
    }}
    failed = {"checks": {
        **passed["checks"],
        "spec": {
            "status": "fail", "evidence": "【示例】合法输入没有得到需求规定的结果。",
            "findings": ["【示例】补齐缺失分支，并通过真实调用入口复验。"],
        },
    }}
    identity = {
        "reviewed_base_sha": "<示例基准SHA>",
        "reviewed_candidate_sha": "<示例候选SHA>",
        "reviewed_candidate_tree": "<示例文件树SHA>",
    }
    run_identity = {
        "default_base_sha": "<示例默认分支SHA>", "run_head_sha": "<示例整体交付SHA>",
        "expected_merge_tree": "<示例合并文件树SHA>",
    }
    repair_identity = {
        "run_base_sha": "<示例整体开发基准SHA>",
        "repair_candidate_sha": "<示例整体修复SHA>",
        "expected_merge_tree": "<示例修复合并文件树SHA>",
    }
    budget = {"completed_review_attempts": 1, "remaining_review_attempts": 2}
    repair = {
        **task, "thread_id": "example-development-thread",
        "repair_source": "acceptance", "acceptance_artifact": failed,
        "review_budget_context": budget,
    }
    add("development", "初次开发：当前子任务", development_prompt(task))
    add("parent-development", "初次开发：完整需求", development_prompt(parent))
    sources: list[tuple[str, str, str, Any]] = [
        ("acceptance", "验收退回", "acceptance_artifact", failed),
        ("git_integrity", "Git 完整性问题", "git_integrity_evidence",
         {"problem": "【示例】交付树包含不应保留的临时产物。"}),
        ("required_checks", "CI 失败", "ci_evidence",
         {"head_sha": "<示例候选SHA>", "failure": "【示例】需求对应的回归检查失败。"}),
        ("human_revision", "维护者修订", "human_feedback", "【示例】请按确认的需求补齐错误提示。"),
        ("merge_conflict", "合并冲突", "merge_conflict_evidence", "【示例】当前工作树的配置文件存在冲突。"),
    ]
    for source, title, field, evidence in sources:
        request = {**task, "thread_id": "example-development-thread",
                   "review_budget_context": budget, "repair_source": source, field: evidence}
        add(f"repair-{source}", f"同会话定向修复：{title}", development_prompt(request))
        add(f"fresh-repair-{source}", f"新会话定向修复：{title}",
            development_prompt({**request, "_invocation_mode": "new-thread"}))
    add("parent-repair", "完整需求修复", development_prompt({**repair, **parent}))
    add("run-repair", "整体范围修复", development_prompt({**repair, **overall}))

    reviewer: dict[str, Any] = {
        **task, "current_review_identity": identity,
        "review_budget_context": {"current_review_attempt": 1, "remaining_review_attempts": 2},
    }
    run_reviewer = {**reviewer, **overall, "current_review_identity": run_identity}
    repair_reviewer = {**run_reviewer, "repair_scope": "run_repair",
                       "current_review_identity": repair_identity}
    for name, title, request in (
        ("review", "独立验收：子任务候选", reviewer),
        ("parent-review", "独立验收：完整需求候选", {**reviewer, **parent}),
        ("run-review", "独立验收：整体合并预览", run_reviewer),
        ("run-repair-review", "独立验收：整体修复合并预览", repair_reviewer),
    ):
        add(name, title, review_prompt(request))
        previous = {key: value.replace("示例", "示例上次")
                    for key, value in request["current_review_identity"].items()}
        add(f"{name}-followup", f"{title}：带上次完整结果", review_prompt({
            **request, "previous_acceptance_artifact": failed, "previous_review_identity": previous,
            "review_budget_context": {"current_review_attempt": 2, "remaining_review_attempts": 1},
        }))
    add("candidate-run-review", "兼容分支：候选与默认分支合并后验收", review_prompt({
        **run_reviewer, "candidate_acceptance": True,
        "current_review_identity": {
            "default_base_sha": "<示例默认分支SHA>", "candidate_sha": "<示例修复SHA>",
            "expected_merge_tree": "<示例合并文件树SHA>",
        },
    }))
    add("run-review-evidence", "整体验收：包含子任务审查与集成证据", review_prompt({
        **run_reviewer,
        "fallback_ticket_records": [{"task_issue_url": task_url, "last_acceptance_artifact": failed}],
        "ticket_integration_records": [{"task_issue_url": task_url, "evidence": "【示例】集成记录。"}],
    }))

    publication = {**task, "acceptance_artifact": passed}
    fallback = {**task, "fallback_publication_context": {
        "last_review_identity": {**identity, "reviewed_candidate_sha": "<示例上次候选SHA>"},
        "last_review_lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "fail"},
        "current_candidate_identity": {"candidate_sha": "<示例当前候选SHA>"},
        "development_delta": True, "current_candidate_has_additional_review": False,
        "repair_source": "acceptance", "failure_evidence_source": "【示例】上次审查原始结果。",
        "candidate_delta": "【示例】后续代码修改。", "git_integrity": "【示例】文件树检查结果。",
    }}
    for name, title, request in (
        ("publication", "文案：子任务已获验收", publication),
        ("fallback-publication", "文案：当前代码尚未获验收", fallback),
        ("parent-publication", "文案：完整需求", {**publication, **parent}),
        ("run-repair-publication", "文案：整体修复", {**publication, **overall}),
    ):
        add(name, title, publication_prompt(request))
    add("final-publication", "文案：最终整体交付",
        publication_prompt(publication, final_run=True))

    resumed = {"thread_id": "example-thread", "_invocation_mode": "resume"}
    for name, title, render, request in (
        ("development", "开发", development_prompt, task),
        ("repair", "修复", development_prompt, repair),
        ("review", "验收", review_prompt, reviewer),
        ("publication", "文案", publication_prompt, publication),
        ("fallback-publication", "尚未获验收的文案", publication_prompt, fallback),
    ):
        add(f"continue-{name}", f"执行续接：{title}", render({**request, **resumed}))
        add(f"human-{name}", f"人工回复后继续：{title}", render({
            **request, **resumed, "prior_human_blockers": ["【示例】需要确认目标行为。"],
            "human_response_history": [{"response": "【示例】已确认目标行为，请核验后继续。"}],
        }))
    add("continue-final-publication", "执行续接：最终整体文案",
        publication_prompt({**publication, **resumed}, final_run=True))
    for name, role, title in (
        ("development", "Development result", "开发结果"),
        ("review", "Acceptance Artifact", "验收结果"),
        ("publication", "Publication Artifact", "文案结果"),
    ):
        add(f"output-repair-{name}", f"仅修复输出格式：{title}",
            structured_output_repair_prompt(role, "【示例】输出缺少必填字段。"))
    add("runner-probe", "安装器结构化输出兼容性检查", inline_probe("runner_probe.py", "_check"))
    add("publication-probe", "文案输出格式握手检查",
        inline_probe("codex.py", "publication_schema_handshake"))
    return previews


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", default="all", help="阶段名称；默认 all 展示全部，名称见 --list")
    parser.add_argument("--list", action="store_true", help="只列出阶段目录")
    parser.add_argument("--output", type=Path, metavar="FILE", help="将展示结果写入 UTF-8 文件")
    base = "https://github.com/example/project/issues/"
    parser.add_argument("--parent-url", default=base + "131", help="替换示例背景需求 URL")
    parser.add_argument("--task-url", default=base + "203", help="替换示例当前任务 URL")
    args = parser.parse_args()
    try:
        previews = scenarios(args.parent_url, args.task_url)
        if args.stage != "all" and args.stage not in previews:
            parser.error(f"未知阶段 {args.stage!r}，使用 --list 查看可用名称")
        if args.list:
            output = "\n".join(f"{name:36} {title}" for name, (title, _) in previews.items()) + "\n"
        else:
            names = list(previews) if args.stage == "all" else [args.stage]
            blocks = [
                "# 当前源码的 Prompt 预览",
                f"源码目录：{ROOT}",
                "正文来自当前运行时生成函数；两处静态探针从源码字面量读取。",
                "任务链接、SHA、次数和证据均为示例，不代表真实任务关系或验收结论。",
                "这里只展示项目角色 Prompt；不启动 Codex，不加载它的系统指令、AGENTS.md 或 Skill 内容。",
                "## 阶段目录\n\n" + "\n".join(f"- {name}：{previews[name][0]}" for name in names),
            ]
            blocks.extend(f"## {name} — {previews[name][0]}\n\n```text\n{previews[name][1]}\n```" for name in names)
            output = "\n\n".join(blocks) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
            print(f"已保存：{args.output.resolve()}")
        else:
            print(output, end="")
    except BrokenPipeError:
        # 支持管道接 head 等提前关闭读取端的工具。
        sys.stdout = open("/dev/null", "w")
    except (OSError, ValueError) as error:
        parser.exit(1, f"无法生成预览：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
