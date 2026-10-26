#!/usr/bin/env python3
"""使用示例任务展示当前源码实际生成的各阶段 Prompt。仅需 Python 3.11+。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from agent_run.config.resources import resource, resolve_resources
from agent_run.contracts.artifact import role_schema
from agent_run.core.prompts.role import prompt_schema, render_prompt
from agent_run.install.probe import PROBE_SCHEMA


def scenarios(parent_url: str, task_url: str, language: str = "zh") -> dict[str, tuple[str, str, dict[str, Any]]]:
    previews: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def add(name: str, title: str, role: str, request: dict[str, Any], *, continuation: bool = False) -> None:
        previews[name] = (title, render_prompt(request, role, continuation=continuation),
                          prompt_schema(role, request))

    shared = {"_prompt_resources": resolve_resources(language), "language": language,
              "parent_issue_url": parent_url, "checkout": "<示例受管工作区>",
              "_invocation_mode": "new-thread"}
    task = {**shared, "acceptance_scope": "ticket", "task_issue_url": task_url}
    parent = {**shared, "acceptance_scope": "parent_only"}
    overall = {**shared, "acceptance_scope": "run"}
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
    overall["ticket_completions"] = [{
        "subject": "ticket:2", "generation": 1,
        "revision": "example-ticket-revision", "parent_revision": "example-parent-revision",
        "graph_revision": "example-graph-revision", "source_sha": "<示例Ticket发布SHA>",
        "merge_sha": "<示例Ticket合入Run的SHA>", "tree": "<示例Ticket成果tree>",
        "pr_number": 3, "gate_mode": "review", "review_attempt": "example-ticket-review",
        "development_attempt": "example-ticket-development",
        "comment_effect": "example-completion-comment", "close_effect": "example-ticket-close",
        "review": {"attempt": "example-ticket-review", "base_sha": "<示例Ticket开发起点>",
                   "candidate_sha": "<示例Ticket候选SHA>", "candidate_tree": "<示例Ticket成果tree>",
                   "outcome": "pass", "artifact": passed},
        "development": {"attempt": "example-ticket-development", "base_sha": "<示例Ticket开发起点>",
                        "candidate_sha": None, "candidate_tree": None, "outcome": "development",
                        "artifact": {"result_kind": "development", "summary": "【示例】完成当前 Ticket 修改与自测。",
                                     "human_blockers": None}},
        "delivered_candidate": {"sha": "<示例Ticket候选SHA>", "tree": "<示例Ticket成果tree>",
                                "development_base": "<示例Ticket开发起点>"},
    }]

    def identity(base: str, candidate: str) -> dict[str, str]:
        return {"reviewed_base_sha": base, "reviewed_candidate_sha": candidate,
                "reviewed_candidate_tree": "<示例固定成果tree>"}

    def review_request(scope: dict[str, Any], base: str, candidate: str,
                       **evidence: Any) -> dict[str, Any]:
        current = identity("<示例交付身份基准>", candidate)
        facts = {"current_review_identity": current, **evidence}
        if "ticket_completions" in scope:
            facts["ticket_completions"] = scope["ticket_completions"]
        return {**scope, "reviewed_commit": candidate, "current_review_identity": current,
                "review_context": {"facts": facts,
                                   "review_range": {"base_sha": base, "candidate_sha": candidate}}}

    ci = {"pr_number": 3, "head_sha": "<示例CI失败head>", "result": "fail",
          "details": [["example-regression", "fail"]],
          "logs": "【示例原始日志】AssertionError: expected greeting\r\n"}
    integration = {
        "effect_id": "example-integrate", "receipt_id": "example-receipt",
        "source_ref": "refs/heads/example-source", "target_ref": "refs/heads/example-target",
        "candidate_sha": "<示例集成源SHA>", "target_sha": "<示例集成目标SHA>",
        "receipt": {"disposition": "failure", "raw": {
            "command": ["git", "merge", "--no-commit", "<示例集成源SHA>"],
            "head": "<示例集成目标SHA>", "merge_head": "<示例集成源SHA>",
            "conflict_paths": ["example.txt"],
            "stdout": "【示例原始输出】CONFLICT (content): Merge conflict in example.txt\n",
            "stderr": ""}},
        "development_attempt": "example-integration-development",
        "development_result": "example-integration-result",
        "development_summary": "【示例】保留双方配置意图，并验证组合后的调用路径。",
    }
    budget = {"completed_review_attempts": 1, "remaining_review_attempts": 2}
    continuations: list[tuple[str, str, str, dict[str, Any]]] = []
    for prefix, label, scope in (("", "Ticket", task), ("parent-", "Parent-only", parent),
                                  ("run-", "多 Ticket Parent", overall)):
        repair = {**scope, "thread_id": "example-development-thread", "_invocation_mode": "new-attempt",
                  "repair_source": "acceptance", "acceptance_artifact": failed,
                  "review_budget_context": budget}
        if prefix != "run-":
            add(prefix + "development", f"初次开发：{label}", "development", scope)
        add(prefix + "repair-acceptance", f"Findings 修复：{label}", "development", repair)
        first = review_request(scope, "<示例集成目标SHA>" if prefix == "run-" else "<示例开发起点SHA>",
                               "<示例首次审核提交>")
        if prefix != "run-":
            first["review_context"]["facts"]["development_summary"] = "【示例】本轮实际开发说明。"
        add(prefix + "review", f"首次固定成果审核：{label}", "review", first)
        followup = review_request(scope, "<示例上轮实际审核提交>", "<示例修复审核提交>",
                                  previous_review_identity=identity("<示例交付身份基准>", "<示例上轮实际审核提交>"),
                                  previous_acceptance_artifact=failed,
                                  development_summary="【示例】按原 Findings 修复，并验证直接回归。")
        add(prefix + "review-followup", f"Findings 后新线程复审：{label}", "review", followup)
        if prefix == "run-":
            revised = review_request(scope, "<示例集成目标SHA>", "<示例人工修订后固定提交>",
                previous_review_identity=identity("<示例交付身份基准>", "<示例人工修订前实际审核提交>"),
                previous_acceptance_artifact=passed,
                development_summary="【示例】按维护者修订完成修改，程序已固定新成果。",
                human_feedback="【示例原始修订】请核实维护者追加的错误提示修改。\r\n")
            add("run-human-revision-review", "维护者修订后重新固定的最终成果", "review", revised)
            add("run-repair-human_revision", "最终批准前的维护者修订", "development", {
                **scope, "repair_source": "human_revision",
                "human_feedback": "【示例原始回复】请补齐错误提示。\r\n"})

        ci_development = {**scope, "work_source": "ci", "repair_source": "required_checks", "ci_evidence": ci}
        add(prefix + "repair-required_checks", f"CI 失败修复：{label}", "development", ci_development)
        ci_review = review_request({**scope, "work_source": "ci"}, "<示例CI失败head>", "<示例CI首次审核提交>",
                                   ci_evidence=ci, development_summary="【示例】修复失败检查的根因。")
        add(prefix + "ci-review", f"CI 修复审核（Ticket 开关开启）：{label}" if not prefix
            else f"CI 修复审核（必须审核）：{label}", "review", ci_review)
        ci_findings = {**repair, "work_source": "ci", "ci_evidence": ci}
        add(prefix + "ci-findings-repair", f"CI Review Findings 修复：{label}", "development", ci_findings)
        ci_followup = review_request({**scope, "work_source": "ci"}, "<示例CI上轮实际审核提交>",
                                    "<示例CI复审提交>", ci_evidence=ci,
                                    previous_review_identity=identity("<示例交付身份基准>", "<示例CI上轮实际审核提交>"),
                                    previous_acceptance_artifact=failed,
                                    development_summary="【示例】针对 CI Findings 的实际修复说明。")
        add(prefix + "ci-review-followup", f"CI Findings 后新线程复审：{label}", "review", ci_followup)
        integration_development = {**scope, "repair_source": "integration", "integration_evidence": integration["receipt"]}
        add(prefix + "repair-integration", f"实际集成冲突处理：{label}", "development", integration_development)
        add(prefix + "repair-git_integrity", f"非冲突 Git 检查修复：{label}", "development", {
            **scope, "repair_source": "git_integrity", "git_integrity_evidence": {
                "disposition": "failure", "raw": {"command": ["git", "diff", "--check"],
                "stderr": "【示例原始输出】example.txt:3: trailing whitespace.\n"}}})
        if prefix:
            integrated = review_request(scope, "<示例集成目标SHA>", "<示例集成实际审核提交>",
                                        integration_record=integration)
            add(prefix + "integration-review", f"原始集成冲突与处理说明审核：{label}", "review", integrated)
            integrated_followup = review_request(scope, "<示例集成上轮实际审核提交>", "<示例集成复审提交>",
                integration_record={**integration,
                    "development_attempt": "example-integration-findings-development",
                    "development_result": "example-integration-findings-result",
                    "development_summary": "【示例】最新集成 Findings 修复；保留原始失败现场。"},
                previous_review_identity=identity("<示例集成目标SHA>", "<示例集成上轮实际审核提交>"),
                previous_acceptance_artifact=failed)
            add(prefix + "integration-review-followup", f"最近集成失败后的复审：{label}", "review", integrated_followup)

        publication = {**scope, "acceptance_artifact": passed}
        publication_role = "final-publication" if prefix == "run-" else "publication"
        publication_name = "final-publication" if prefix == "run-" else prefix + "publication"
        add(publication_name, f"已审核成果的交付文案：{label}", publication_role, publication)
        continuations.extend(((prefix + "repair", f"{label} 修复", "development", repair),
                              (prefix + "review", f"{label} 审核", "review", followup),
                              (publication_name, f"{label} 文案", publication_role, publication)))

    fallback = {**task, "fallback_publication_context": {
        "last_review_identity": identity("<示例开发起点SHA>", "<示例上轮实际审核提交>"),
        "last_review_artifact": failed,
        "current_candidate_identity": {"candidate_sha": "<示例当前候选SHA>"},
        "current_candidate_has_additional_review": False,
        "development_summary": "【示例】审核额度耗尽后完成的最后一轮修改，未获新审核。"}}
    ci_waiver = {**task, "ci_repair_publication_context": {
        "failed_required_checks": ci, "previous_published_head": "<示例CI失败head>",
        "current_candidate_identity": {"candidate_sha": "<示例CI免审候选SHA>"},
        "current_candidate_has_additional_review": False,
        "development_summary": "【示例】CI 开关关闭时完成的修复，尚无新 CI 结果。"}}
    add("fallback-publication", "Ticket 末轮审核豁免文案", "publication", fallback)
    add("ci-waiver-publication", "Ticket CI 开关关闭后的免审文案", "publication", ci_waiver)
    continuations.extend((("fallback-publication", "Ticket 末轮豁免文案", "publication", fallback),
                          ("ci-waiver-publication", "Ticket CI 免审文案", "publication", ci_waiver)))
    for name, title, role, request in continuations:
        resumed = {**request, "thread_id": "example-thread", "_invocation_mode": "resume"}
        add("continue-" + name, "同轮原线程续接：" + title, role, resumed, continuation=True)
        add("fresh-" + name, "新线程完整输入：" + title, role,
            {**request, "thread_id": None, "_invocation_mode": "new-thread"})
        add("human-" + name, "人工回复后同轮续接：" + title, role, {
            **resumed, "prior_human_blockers": ["【示例】需要确认目标行为。"],
            "human_response_history": [{"response": "【示例原始回复】已确认目标行为，请核实后继续。\r\n"}],
        }, continuation=True)
    for name, output_name in (("development", "Development result"), ("review", "Acceptance Artifact"),
                              ("publication", "Publication Artifact")):
        add("output-repair-" + name, "仅修正结果格式：" + name, "output-repair", {
            **task, "output_name": output_name, "contract_error": "【示例】输出缺少必填字段。"})
    previews["runner-probe"] = ("安装器兼容性探针（仅展示）", resource(task, "context/probe"), PROBE_SCHEMA)
    previews["publication-probe"] = ("文案输出格式握手（仅展示）", resource(task, "publication/handshake"),
                                     role_schema("publication"))
    return previews


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", default="all", help="阶段名称；默认 all 展示全部，名称见 --list")
    parser.add_argument("--list", action="store_true", help="只列出阶段目录")
    parser.add_argument("--language", choices=("zh", "en"), default="zh", help="角色正文语言；默认 zh")
    parser.add_argument("--output", type=Path, metavar="FILE", help="将展示结果写入 UTF-8 文件")
    base = "https://github.com/example/project/issues/"
    parser.add_argument("--parent-url", default=base + "131", help="替换示例背景需求 URL")
    parser.add_argument("--task-url", default=base + "203", help="替换示例当前任务 URL")
    args = parser.parse_args()
    try:
        previews = scenarios(args.parent_url, args.task_url, args.language)
        if args.stage != "all" and args.stage not in previews:
            parser.error(f"未知阶段 {args.stage!r}，使用 --list 查看可用名称")
        if args.list:
            output = "\n".join(f"{name:36} {title}" for name, (title, _, _) in previews.items()) + "\n"
        else:
            names = list(previews) if args.stage == "all" else [args.stage]
            blocks = [
                "# 当前源码的 Prompt 预览",
                f"源码目录：{ROOT}",
                "正文来自统一资源读取与实际运行时组装；探针使用当前包的内部资源。",
                "任务链接、SHA、次数和证据均为示例，不代表真实任务关系或验收结论。",
                "发布仅展示生产已提供的事实；完整 PR 与增量提交范围示例待 #446 的生产输入合同交付后补齐。",
                "这里只展示项目角色 Prompt；不启动 Codex，不加载它的系统指令、AGENTS.md 或 Skill 内容。",
                "## 阶段目录\n\n" + "\n".join(f"- {name}：{previews[name][0]}" for name in names),
            ]
            blocks.extend(
                f"## {name} — {previews[name][0]}\n\n```text\n{previews[name][1]}\n```"
                + "\n\n独立传送的输出 schema（不复制进角色正文）：\n\n```json\n"
                + json.dumps(previews[name][2], ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
                for name in names)
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
