from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from test_codex_prompt_contract import _capture_public_prompt


def test_initial_development_recommends_one_review_round_without_stage_selection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {"acceptance_scope": "ticket"},
        name="initial-review-recommendation",
    )

    assert "建议安排审查子 Agent" in prompt
    assert "本次开发最多组织一轮" in prompt
    assert 'fork_turns: "none"' in prompt
    assert "如果是初次开发" not in prompt
    assert "Initial Development" not in prompt
    assert "Development Preflight Round" not in prompt


@pytest.mark.parametrize("invocation", [{}, {"_invocation_mode": "new-thread"}])
def test_fresh_repair_thread_reads_requirements_and_uses_repair_review_exception(
    tmp_path: Path, monkeypatch: Any, invocation: dict[str, str]
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "repair_source": "acceptance",
            "acceptance_artifact": {"finding": "RAW_REPAIR_EVIDENCE"},
            **invocation,
        },
        name="fresh-repair-requirements",
    )

    assert "开始前先读取具体任务，再读取背景" in prompt
    assert "RAW_REPAIR_EVIDENCE" in prompt
    assert "默认不再组织独立审查" in prompt
    assert "既改变原方案、又引入此前未覆盖的关键风险" in prompt
    assert "才允许一次针对该风险的审查" in prompt
    assert "已掌握的需求" not in prompt
    assert "建议安排审查子 Agent" not in prompt


def test_task_and_background_urls_are_labeled_without_requirement_bodies(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task": {"title": "TASK_TITLE_PRIVATE", "body": "TASK_BODY_PRIVATE"},
            "parent": {"title": "PARENT_TITLE_PRIVATE", "body": "PARENT_BODY_PRIVATE"},
        },
        name="labeled-requirements",
    )

    assert "本次负责的具体任务：https://github.com/example/project/issues/2" in prompt
    assert "用于理解整体需求的背景：https://github.com/example/project/issues/1" in prompt
    assert "不自动增加其他子任务的工作" in prompt
    for private_text in (
        "TASK_TITLE_PRIVATE", "TASK_BODY_PRIVATE", "PARENT_TITLE_PRIVATE", "PARENT_BODY_PRIVATE"
    ):
        assert private_text not in prompt


def test_review_requires_real_user_and_code_call_paths(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, "review", {"acceptance_scope": "ticket"},
        name="review-real-paths",
    )

    for instruction in (
        "网页或浏览器扩展使用真实浏览器",
        "实际执行命令",
        "核对用户可见结果",
        "通过实际接口或真实调用方",
        "调用链和集成行为",
        "不在验证脚本中重新实现待测逻辑",
        "同时涉及上述两类内容时",
        "纯文档",
        "无需人为增加浏览器操作",
        "如实说明未验证范围",
        "不把局部通过写成端到端通过",
    ):
        assert instruction in prompt


def test_review_allows_only_necessary_consistent_temporary_runtime_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, "review", {"acceptance_scope": "ticket"},
        name="review-runtime-copy",
    )

    for instruction in (
        "整个工作区保持只读",
        "优先使用项目支持的外部缓存和运行目录",
        "确实无法在只读目录运行时",
        "允许在工作区外创建临时运行副本",
        "包括未提交修改和未跟踪的交付文件",
        "不能只复制 HEAD 的提交内容",
        "不修复或改写其中的交付源码、测试、配置及依赖声明",
        "运行前后核对这些文件",
        "不能把修改副本后的通过作为原交付的验收结果",
        "关闭本轮启动的服务和浏览器",
        "清理临时副本及其他本轮临时产物",
        "在 evidence 中简述副本对应的验收对象和内容一致性核验情况",
    ):
        assert instruction in prompt


@pytest.mark.parametrize("method,scope", [
    ("publication", "ticket"),
    ("publication", "parent_only"),
    ("publication", "run"),
    ("run_publication", "run"),
])
def test_publication_explains_all_four_parts_without_imposing_output_sections(
    tmp_path: Path, monkeypatch: Any, method: str, scope: str
) -> None:
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, method,
        {"acceptance_scope": scope, "acceptance_artifact": {}},
        name=f"{method}-{scope}-narrative",
    )

    for instruction in (
        "What Problem This Solves", "改动前的问题或限制", "改动后的对应行为",
        "Why This Change Was Made", "关键设计决定和实际取舍",
        "User Impact", "兼容性、配置或迁移要求", "没有用户可见变化时如实说明",
        "Evidence", "实际测试或检查、结果及覆盖范围", "旧代码的通过不能写成当前代码已通过",
        "按变更规模调整篇幅、合并章节", "只读当前工作区与证据",
        "若在工作区外创建本轮临时文件，定位并清理",
    ):
        assert instruction in prompt
    assert "skill:implement" not in prompt
    assert "skill:code-review" not in prompt


def test_ordinary_run_review_binds_merge_preview_instead_of_head_commit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, "review",
        {
            "acceptance_scope": "run",
            "current_review_identity": {
                "default_base_sha": "DEFAULT_BASE",
                "run_head_sha": "DELIVERY_HEAD",
                "expected_merge_tree": "EXPECTED_MERGE_TREE",
            },
        },
        name="ordinary-run-merge-preview",
    )

    assert "默认分支与本次全部交付合并后的未提交预览" in prompt
    assert "HEAD 保留在基准提交是正常情况" in prompt
    assert "检查工作树中的整体结果" in prompt
    for identity in ("DEFAULT_BASE", "DELIVERY_HEAD", "EXPECTED_MERGE_TREE"):
        assert prompt.count(identity) == 1
    assert "当前工作区对应该提交" not in prompt


@pytest.mark.parametrize("method", ["develop", "review", "publication", "run_publication"])
@pytest.mark.parametrize("mode", ["new-thread", "same-thread", "resume"])
def test_overall_work_rereads_subtasks_only_on_a_fresh_thread(
    tmp_path: Path, monkeypatch: Any, method: str, mode: str
) -> None:
    request: dict[str, Any] = {
        "acceptance_scope": "run",
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "acceptance_artifact": {},
        "current_review_identity": {
            "default_base_sha": "BASE", "run_head_sha": "HEAD", "expected_merge_tree": "TREE",
        },
        "thread_id": "existing-thread",
    }
    if method == "develop":
        request["repair_source"] = "acceptance"
    if mode != "same-thread":
        request["_invocation_mode"] = mode
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, method, request, name=f"overall-{method}-{mode}",
    )

    assert "整体结果" in prompt
    assert "最终用户路径" in prompt
    should_read = mode == "new-thread" or (mode == "same-thread" and method in ("publication", "run_publication"))
    assert ("读取最终子任务及依赖" in prompt) is should_read
    if not should_read:
        assert "开始前先读取" not in prompt


@pytest.mark.parametrize("kind", ["development", "repair", "continuation"])
def test_development_worker_command_matches_context_contract(
    tmp_path: Path, monkeypatch: Any, kind: str
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    request: dict[str, Any] = {
        "acceptance_scope": "ticket",
        "task_issue_url": "https://github.com/example/project/issues/2",
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "_invocation_mode": "new-thread",
    }
    if kind != "development":
        request.update(
            repair_source="human_revision",
            human_feedback="RAW_CURRENT_REVISION",
            prior_human_blockers=["RAW_BLOCKER"],
            human_response_history=[{"response": "RAW_RESPONSE"}],
        )
    if kind == "continuation":
        request.update(thread_id="existing-thread", _invocation_mode="resume")
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, "develop", request,
        name=kind, worker_calls=calls,
    )
    arguments, options = calls[0]
    checkout = tmp_path / kind
    assert options["cwd"] == checkout
    assert "--dangerously-bypass-approvals-and-sandbox" in arguments
    assert "--output-schema" in arguments
    if kind == "continuation":
        assert "resume" in arguments
        assert "existing-thread" in arguments
        assert "开始前先读取" not in prompt
        assert "继续完成你负责的修复任务" in prompt
    else:
        assert "resume" not in arguments
        assert arguments[arguments.index("--cd") + 1] == str(checkout)
        assert f"当前 checkout：{checkout}" in prompt
        assert "继续当前目录中已有及未提交的代码" in prompt
        assert "自行读取当前权威需求并建立本轮需求基线" in prompt
        assert "程序摘要、旧开发总结、历史对话和旧验收结论不能替代" in prompt
        assert "只读 `gh issue view`" in prompt
        assert "不暂存、commit、改写 Git 历史或执行 GitHub 写入" in prompt
        assert '"result_kind":"development"' in prompt
        assert "后续审查和最终完整测试，由另一位验收工程师负责" in prompt
    if kind != "development":
        for evidence in ("RAW_CURRENT_REVISION", "RAW_BLOCKER", "RAW_RESPONSE"):
            assert evidence in prompt
        assert "建议安排审查子 Agent" not in prompt
    if kind == "repair":
        assert "默认不再组织独立审查" in prompt
        assert "已掌握的需求" not in prompt
