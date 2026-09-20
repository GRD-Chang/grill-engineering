from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.delivery_loop import TicketDeliveryAdapter


PASS_EVIDENCE = {
    "e2e": "操作或命令：运行候选公开流程；退出码：0；结果：候选通过端到端复验。",
    "standards": "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：请求中的全部验收标准；覆盖结论：候选完整覆盖。",
}


def _capture_public_prompt(
    tmp_path: Path,
    monkeypatch: Any,
    method: str,
    request: dict[str, Any],
    *,
    name: str,
    worker_calls: list[tuple[list[str], dict[str, Any]]] | None = None,
) -> str:
    checkout = tmp_path / name
    checkout.mkdir()
    captured: list[str] = []
    if method == "develop":
        result: dict[str, Any] = {
            "result_kind": "development",
            "summary": "Implemented and verified.",
            "human_blockers": None,
        }
    elif method == "review":
        result = {
            "checks": {
                lane: {
                    "status": "pass",
                    "evidence": PASS_EVIDENCE[lane],
                    "findings": [],
                }
                for lane in ("e2e", "standards", "spec")
            }
        }
    else:
        result = {
            "result_kind": "publication",
            "commit_message": "fix(agent): publish validated repair",
            "pr_title": "fix(agent): publish validated repair",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nA validated change is ready.\n\n"
                "## Why This Change Was Made\n\nThe change follows the contract.\n\n"
                "## User Impact\n\nThe requested behavior is available.\n\n"
                "## Evidence\n\nIndependent validation passed."
            ),
            "human_blockers": None,
        }

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        captured.append(str(options["prompt"]))
        schema_index = arguments.index("--output-schema") + 1
        options["captured_schema"] = json.loads(
            Path(arguments[schema_index]).read_text(encoding="utf-8")
        )
        if worker_calls is not None:
            worker_calls.append((arguments, options))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        reported_thread = request.get("thread_id", "public-prompt-test")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"type":"thread.started","thread_id":"'
                + str(reported_thread)
                + '"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    getattr(backend, method)({**request, "checkout": str(checkout)})
    return captured[0]


def test_publication_prompts_keep_human_blocker_semantics(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ticket = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {"acceptance_scope": "ticket", "acceptance_artifact": {}},
        name="ticket-publication",
    )
    run_repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {"acceptance_scope": "run", "acceptance_artifact": {}},
        name="run-repair-publication",
    )
    final_run = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "run_publication",
        {"acceptance_artifact": {}},
        name="final-run-publication",
    )

    for prompt in (ticket, run_repair, final_run):
        assert "human_blocker" in prompt
        assert "commit_message" in prompt
        assert "pr_body_markdown" in prompt
        assert "summary" not in prompt


def test_reviewer_prompt_uses_role_specific_current_and_previous_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact = {
        "checks": {
            lane: {
                "status": "pass",
                "evidence": PASS_EVIDENCE[lane],
                "findings": [],
            }
            for lane in ("e2e", "standards", "spec")
        }
    }
    cases = (
        (
            "ticket-review-identity",
            {
                "acceptance_scope": "ticket",
                "current_review_identity": {
                    "reviewed_base_sha": "TICKET_CURRENT_BASE",
                    "reviewed_candidate_sha": "TICKET_CURRENT_CANDIDATE",
                    "reviewed_candidate_tree": "TICKET_CURRENT_TREE",
                },
                "previous_acceptance_artifact": artifact,
                "previous_review_identity": {
                    "reviewed_base_sha": "TICKET_PREVIOUS_BASE",
                    "reviewed_candidate_sha": "TICKET_PREVIOUS_CANDIDATE",
                    "reviewed_candidate_tree": "TICKET_PREVIOUS_TREE",
                },
            },
            (
                "TICKET_CURRENT_BASE",
                "TICKET_CURRENT_CANDIDATE",
                "TICKET_CURRENT_TREE",
                "TICKET_PREVIOUS_BASE",
                "TICKET_PREVIOUS_CANDIDATE",
                "TICKET_PREVIOUS_TREE",
                "上一次验收对象",
            ),
        ),
        (
            "run-review-identity",
            {
                "acceptance_scope": "run",
                "current_review_identity": {
                    "default_base_sha": "RUN_CURRENT_DEFAULT",
                    "run_head_sha": "RUN_CURRENT_HEAD",
                    "expected_merge_tree": "RUN_CURRENT_TREE",
                },
                "previous_acceptance_artifact": artifact,
                "previous_review_identity": {
                    "default_base_sha": "RUN_PREVIOUS_DEFAULT",
                    "run_head_sha": "RUN_PREVIOUS_HEAD",
                    "expected_merge_tree": "RUN_PREVIOUS_TREE",
                },
            },
            (
                "RUN_CURRENT_DEFAULT",
                "RUN_CURRENT_HEAD",
                "RUN_CURRENT_TREE",
                "RUN_PREVIOUS_DEFAULT",
                "RUN_PREVIOUS_HEAD",
                "上一次验收对象",
            ),
        ),
        (
            "run-repair-review-identity",
            {
                "acceptance_scope": "run",
                "candidate_acceptance": True,
                "repair_scope": "run_repair",
                "current_review_identity": {
                    "run_base_sha": "REPAIR_CURRENT_BASE",
                    "repair_candidate_sha": "REPAIR_CURRENT_CANDIDATE",
                    "expected_merge_tree": "REPAIR_CURRENT_TREE",
                },
                "previous_acceptance_artifact": artifact,
                "previous_review_identity": {
                    "run_base_sha": "REPAIR_PREVIOUS_BASE",
                    "repair_candidate_sha": "REPAIR_PREVIOUS_CANDIDATE",
                    "expected_merge_tree": "REPAIR_PREVIOUS_TREE",
                },
            },
            (
                "REPAIR_CURRENT_BASE",
                "REPAIR_CURRENT_CANDIDATE",
                "REPAIR_CURRENT_TREE",
                "REPAIR_PREVIOUS_BASE",
                "REPAIR_PREVIOUS_CANDIDATE",
                "上一次验收对象",
            ),
        ),
    )

    for name, request, required in cases:
        prompt = _capture_public_prompt(
            tmp_path, monkeypatch, "review", request, name=name
        )
        for marker in required:
            assert marker in prompt
        for marker in required[:3]:
            assert prompt.count(marker) == 1
        assert "Reviewer 2+" not in prompt
        assert "本窗口 Reviewer" not in prompt
        assert "因前次调用失败而继续的同 Thread Resume" not in prompt
        assert "优先复核原有问题" in prompt
        assert "这次修改" in prompt
        assert "直接回归" in prompt
        assert "没有具体风险依据时，不重复完整扫描未变化代码" in prompt
        assert "当前证据或影响需要时可以扩大检查" in prompt
        assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in prompt
        assert "旧结果不能证明当前代码已经通过" in prompt

    r2_prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "run",
            "candidate_acceptance": True,
            "repair_scope": "run_repair",
            "current_review_identity": {
                "run_base_sha": "R2_CURRENT_BASE",
                "repair_candidate_sha": "R2_CURRENT_CANDIDATE",
                "expected_merge_tree": "R2_CURRENT_TREE",
            },
            "previous_acceptance_artifact": artifact,
            "previous_review_identity": {
                "default_base_sha": "R1_DEFAULT_BASE",
                "run_head_sha": "R1_RUN_HEAD",
                "expected_merge_tree": "R1_EXPECTED_TREE",
            },
        },
        name="run-repair-review-previous-run",
    )
    assert "上一次验收对象" in r2_prompt
    assert "R1_DEFAULT_BASE" in r2_prompt
    assert "R1_RUN_HEAD" in r2_prompt
    assert "R1_EXPECTED_TREE" in r2_prompt


@pytest.mark.parametrize("resume", [False, True])
def test_fallback_publication_prompt_receives_only_minimal_projection(
    tmp_path: Path, monkeypatch: Any, resume: bool
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            "acceptance_scope": "ticket",
            **({"thread_id": "publication-thread", "_invocation_mode": "resume"} if resume else {}),
            "fallback_receipt": {
                "window": "PRIVATE_WINDOW",
                "reviewer_invocations": "PRIVATE_REVIEW_COUNT",
                "review_artifacts": ["PRIVATE_ARTIFACT"],
                "ci_evidence": "PRIVATE_CI",
            },
            "fallback_publication_context": {
                "last_review_identity": {
                    "reviewed_candidate_sha": "SAFE_PREVIOUS_CANDIDATE"
                },
                "current_candidate_identity": {
                    "reviewed_candidate_sha": "SAFE_CURRENT_CANDIDATE"
                },
                "development_delta": True,
                "current_candidate_has_additional_review": False,
                "failure_evidence_source": "SAFE_FAILURE_EVIDENCE_SOURCE",
            },
        },
        name="fallback-minimal-projection",
    )

    assert "SAFE_PREVIOUS_CANDIDATE" in prompt
    assert "SAFE_CURRENT_CANDIDATE" in prompt
    assert "SAFE_FAILURE_EVIDENCE_SOURCE" in prompt
    assert "PRIVATE_WINDOW" not in prompt
    assert "PRIVATE_REVIEW_COUNT" not in prompt
    assert "PRIVATE_ARTIFACT" not in prompt
    assert "PRIVATE_CI" not in prompt
    assert "当前修改没有独立验收通过的结论" in prompt
    assert "最近一次审查针对的是修改前的代码" in prompt
    assert "不能声称当前代码通过验收" in prompt
    assert "后续修改" in prompt


def test_prompt_roles_distinguish_ticket_parent_and_run_repair_publication(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ticket = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {"acceptance_scope": "ticket"},
        name="ticket-role",
    )
    parent = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {"acceptance_scope": "parent_only"},
        name="parent-role",
    )
    run_repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {"acceptance_scope": "run", "acceptance_artifact": {}},
        name="run-repair-role",
    )

    assert "独立验收工程师" in ticket
    assert "具体任务的标题、正文和验收条件确定本次范围" in ticket
    assert "独立验收工程师" in parent
    assert "该需求的标题、正文和全部验收条件确定本次范围" in parent
    assert "交付说明" in run_repair
    assert "读取最终子任务及依赖" in run_repair


def test_development_prompt_keeps_role_specific_human_blocker_semantics(
    tmp_path: Path, monkeypatch: Any
) -> None:
    development = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {"acceptance_scope": "ticket"},
        name="development",
    )

    assert "human_blocker" in development
    assert "summary" in development
    assert "pr_body_markdown" not in development


def test_development_prompt_keeps_git_authority_local_to_the_role(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
        },
        name="development-authority",
    )

    assert "程序会统一提交当前工作区的修改和未被忽略的新增文件" in prompt
    assert "检查全部未提交内容" in prompt
    assert "保留应交付的" in prompt
    assert "可以使用只读 Git" in prompt
    assert "只整理当前工作树，不暂存、commit、改写 Git 历史或执行 GitHub 写入" in prompt
    assert "工作区外的本轮临时路径也要定位并清理" in prompt
    assert "不进行宽泛删除" in prompt
    assert "不用忽略规则隐藏交付文件" in prompt
    assert "Controller" not in prompt
    assert "Publisher" not in prompt
    assert "后续 Git/GitHub 交付" not in prompt


def test_repair_prompt_preserves_raw_evidence_without_controller_triage(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact = {
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "RAW_EVIDENCE",
                "findings": ["RAW_FINDING"],
            },
            **{
                lane: {"status": "pass", "evidence": "pass", "findings": []}
                for lane in ("standards", "spec")
            },
        }
    }

    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "repair_source": "acceptance",
            "acceptance_artifact": artifact,
        },
        name="acceptance-repair",
    )

    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in prompt
    assert "当前范围内全部有证据支持的 findings" in prompt
    assert "延期说明和可选建议" in prompt
    assert "不是自动修改指令" in prompt
    assert "问题示例不限制调查范围，实现方式由你判断" in prompt
    assert "理解根因及其直接影响" in prompt
    assert "直接影响的同类场景" in prompt
    assert "本次修复可能造成的回归" in prompt


def test_git_integrity_prompt_preserves_only_raw_integrity_evidence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    evidence = {
        "kind": "git_integrity",
        "observed_head": "INTEGRITY_HEAD",
        "actual_subject": "agent-owned commit",
        "expected_subject": "chore(ticket-3): candidate 1",
        "workspace_clean": "true",
    }
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "repair_source": "git_integrity",
            "git_integrity_evidence": evidence,
        },
        name="git-integrity-repair",
    )

    assert "可在当前目录修复的完整性问题" in prompt
    assert "INTEGRITY_HEAD" in prompt
    assert "agent-owned commit" in prompt
    assert "reviewer_invocations" not in prompt
    assert "modification_attempts" not in prompt
    assert "fallback" not in prompt.lower()


@pytest.mark.parametrize(
    ("scope", "required", "forbidden"),
    [
        (
            "ticket",
            (
                "具体任务的标题、正文和验收条件确定本次范围",
                "背景用于理解整体目标",
                "不自动增加其他子任务的工作",
                "https://github.com/example/project/issues/125",
            ),
            ("该需求的标题、正文和全部验收条件确定本次范围",),
        ),
        (
            "parent_only",
            ("本次负责的完整需求", "该需求的标题、正文和全部验收条件确定本次范围"),
            ("本次负责的具体任务", "https://github.com/example/project/issues/125"),
        ),
        (
            "run",
            ("本次负责的完整需求", "最终子任务及依赖", "任务之间的配合和最终用户路径"),
            ("本次负责的具体任务", "https://github.com/example/project/issues/125"),
        ),
    ],
)
def test_prompt_scope_selects_the_matching_review_boundary(
    tmp_path: Path,
    monkeypatch: Any,
    scope: str,
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    request = {
        "acceptance_scope": scope,
        "parent_issue_url": "https://github.com/example/project/issues/90",
    }
    if scope == "ticket":
        request["task_issue_url"] = "https://github.com/example/project/issues/125"

    development = _capture_public_prompt(
        tmp_path, monkeypatch, "develop", request, name=f"{scope}-development"
    )
    review = _capture_public_prompt(
        tmp_path, monkeypatch, "review", request, name=f"{scope}-review"
    )

    for marker in required:
        assert marker in development
        assert marker in review
    for marker in forbidden:
        assert marker not in development
        assert marker not in review


def test_development_prompt_uses_risk_proportional_verification_and_stops_at_completion(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/90",
            "task_issue_url": "https://github.com/example/project/issues/125",
        },
        name="risk-proportional-development",
    )

    assert "使用 skill:implement" in prompt
    assert "按修改的实际风险验证受影响功能" in prompt
    assert "无需每轮固定运行完整测试" in prompt
    assert "影响范围不明或发现具体风险时仍应扩大验证" in prompt
    assert "完整测试失败时先定向定位和修复，再对收口后的代码复验" in prompt
    assert "后续审查和最终完整测试，由另一位验收工程师负责" in prompt
    assert "不把未执行或旧代码的通过当作本次结果" in prompt
    assert "实现、验证和自行检查后" in prompt
    assert "建议安排审查子 Agent" in prompt
    assert "本次开发最多组织一轮" in prompt
    assert "按实际风险安排不同方向的审查子 Agent" in prompt
    assert 'fork_turns: "none"' in prompt
    assert "中立的需求、范围和代码事实，不传递预设结论" in prompt
    assert "包括未提交修改与未跟踪的交付文件" in prompt
    assert "覆盖其默认只比较已提交 HEAD 的做法" in prompt
    assert "处理本轮问题后自行检查和复测，不反复启动通用审查" in prompt
    assert "探索或并行实现等其他协作按任务需要组织" in prompt
    assert "实现本次全部验收条件" in prompt
    assert "处理已知的当前范围问题" in prompt
    assert "你只报告实际开发与自测结果" in prompt
    assert "交付是否通过审查，由验收工程师另行判断" in prompt


@pytest.mark.parametrize(
    ("request_extra", "evidence_marker"),
    [
        (
            {
                "repair_source": "acceptance",
                "acceptance_artifact": {"repair": "ACCEPTANCE_REPAIR"},
            },
            "ACCEPTANCE_REPAIR",
        ),
        (
            {
                "repair_source": "required_checks",
                "ci_evidence": {"repair": "REQUIRED_CHECKS_REPAIR"},
            },
            "REQUIRED_CHECKS_REPAIR",
        ),
        (
            {
                "repair_source": "git_integrity",
                "git_integrity_evidence": {"repair": "GIT_INTEGRITY_REPAIR"},
            },
            "GIT_INTEGRITY_REPAIR",
        ),
        (
            {
                "repair_source": "human_revision",
                "human_feedback": "HUMAN_REVISION_REPAIR",
            },
            "HUMAN_REVISION_REPAIR",
        ),
        (
            {
                "repair_source": "merge_conflict",
                "merge_conflict_evidence": "MERGE_CONFLICT_REPAIR",
            },
            "MERGE_CONFLICT_REPAIR",
        ),
    ],
)
def test_directed_repair_reviews_only_new_critical_design_risks(
    tmp_path: Path,
    monkeypatch: Any,
    request_extra: dict[str, Any],
    evidence_marker: str,
) -> None:
    full_prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {"acceptance_scope": "ticket", **request_extra},
        name=f"directed-repair-{request_extra['repair_source']}",
    )
    compact_prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "thread_id": "existing-development-thread",
            **request_extra,
        },
        name=f"compact-directed-repair-{request_extra['repair_source']}",
    )

    for prompt in (full_prompt, compact_prompt):
        assert evidence_marker in prompt
        assert "实际风险验证受影响功能" in prompt
        assert "默认不再组织独立审查" in prompt
        assert "既改变原方案、又引入此前未覆盖的关键风险" in prompt
        assert "才允许一次针对该风险的审查" in prompt
        assert 'fork_turns: "none"' in prompt
        assert "中立" in prompt
        assert "未跟踪" in prompt and "交付文件" in prompt
        assert "处理结果后自行检查和复测，不反复启动通用审查" in prompt
        assert "Development Preflight Round" not in prompt
        assert "建议安排审查子 Agent" not in prompt
    assert "开始前先读取" in full_prompt
    assert "使用 skill:implement" in compact_prompt
    assert "已掌握的需求" in compact_prompt
    assert "开始前先读取" not in compact_prompt
    assert "Thread" not in compact_prompt
    assert "Resume" not in compact_prompt


@pytest.mark.parametrize(
    ("acceptance_scope", "request_urls", "required_urls"),
    [
        (
            "ticket",
            {
                "parent_issue_url": "DIRECTED_REPAIR_PARENT_URL",
                "task_issue_url": "DIRECTED_REPAIR_TICKET_URL",
            },
            ("DIRECTED_REPAIR_PARENT_URL", "DIRECTED_REPAIR_TICKET_URL"),
        ),
        (
            "parent_only",
            {"parent_issue_url": "DIRECTED_REPAIR_PARENT_ONLY_URL"},
            ("DIRECTED_REPAIR_PARENT_ONLY_URL",),
        ),
        (
            "run",
            {"parent_issue_url": "DIRECTED_REPAIR_RUN_PARENT_URL"},
            ("DIRECTED_REPAIR_RUN_PARENT_URL",),
        ),
    ],
)
def test_directed_repair_keeps_issue_urls_without_mandatory_reread(
    tmp_path: Path,
    monkeypatch: Any,
    acceptance_scope: str,
    request_urls: dict[str, str],
    required_urls: tuple[str, ...],
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": acceptance_scope,
            "thread_id": "existing-development-thread",
            **request_urls,
            "repair_source": "required_checks",
            "ci_evidence": {"check": "DIRECTED_REPAIR_CI_EVIDENCE"},
        },
        name=f"directed-repair-issue-context-{acceptance_scope}",
    )

    for url in required_urls:
        assert url in prompt
    assert "以当前代码、原始证据及已掌握的需求为依据" in prompt
    assert "通过只读 `gh issue view` 回查对应 Issue" in prompt
    assert "不要仅因开始新一轮修复就重复读取未变化的需求" in prompt
    assert "开始前先读取" not in prompt


def test_initial_development_still_requires_reading_issue_contracts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "INITIAL_PARENT_URL",
            "task_issue_url": "INITIAL_TICKET_URL",
        },
        name="initial-development-issue-reading",
    )

    assert "INITIAL_PARENT_URL" in prompt
    assert "INITIAL_TICKET_URL" in prompt
    assert "开始前先读取具体任务，再读取背景" in prompt
    assert "只读 `gh issue view`" in prompt
    assert "读取标题、正文和验收条件" in prompt
    assert "已掌握的需求" not in prompt


def test_ordinary_and_final_ci_fix_sources_use_the_same_agent_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    adapter = TicketDeliveryAdapter(
        git=SimpleNamespace(checkout_head=lambda _checkout: "HEAD_SHA"),
        github=SimpleNamespace(),
    )
    state = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1},
        "ticket_graph": {"tickets": {"3": {"number": 3}}},
    }
    shared_job = {
        "ticket_number": 3,
        "effective_revision": "revision-1",
        "base_sha": "BASE_SHA",
        "repair_source": "required_checks",
        "ci_evidence": {"check": "EXACT_HEAD_CI_EVIDENCE"},
    }
    prompts: list[str] = []

    for attempt_kind in ("ordinary", "final_ci_fix"):
        request = adapter.development_request(
            state,
            {**shared_job, "next_attempt_kind": attempt_kind},
            tmp_path,
        )
        assert "next_attempt_kind" not in request
        prompts.append(
            _capture_public_prompt(
                tmp_path,
                monkeypatch,
                "develop",
                request,
                name=f"required-checks-{attempt_kind}",
            )
        )

    # Checkout paths are task facts; the repair instructions remain identical.
    assert prompts[0].replace("required-checks-ordinary", "CHECKOUT") == prompts[1].replace(
        "required-checks-final_ci_fix", "CHECKOUT"
    )
    assert "下列检查失败对应当前提交" in prompts[0]
    assert "EXACT_HEAD_CI_EVIDENCE" in prompts[0]
    assert "默认不再组织独立审查" in prompts[0]


def test_fresh_acceptance_prompt_keeps_lane_independence_without_fixed_orchestration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/90",
            "task_issue_url": "https://github.com/example/project/issues/125",
        },
        name="fresh-acceptance",
    )

    assert "除 Skill 的 Standards/Spec 审查外，你还负责 E2E 验证" in prompt
    assert "完整测试与必要检查" in prompt
    assert "记录实际对象、验证入口、操作或命令、预期与实际结果、必要环境" in prompt
    assert "退出码" in prompt
    assert "代码、测试、依赖或环境变化后重新判断旧证据是否适用" in prompt
    assert "失败时给出具体证据与复验要求" in prompt
    assert "按当前需求和代码检查本次完整范围，独立建立验收结论" in prompt
    assert "Standards 与 Spec 使用静态证据" in prompt
    assert "不重复相同完整套件，除非具体风险确实需要" in prompt
    assert "使用 skill:code-review" in prompt
    assert "对三个维度的最终结果负责" in prompt
    assert 'fork_turns: "none"' in prompt
    assert "Deferred to #N：…" in prompt
    assert "Non-blocking observation：…" in prompt
    assert "使当前交付不可接受" in prompt
    assert "同一根因合并为一条" in prompt
    assert "无实际价值的轻微意见省略" in prompt
    assert "问题、证据、所需修复及复验方式" in prompt
    assert "findings 中的问题会交回开发修复" in prompt
    assert "二者不进入 findings，不改变状态，不触发自动修复" in prompt
    assert "覆盖 code-review 默认的 Markdown 报告步骤" in prompt
    assert "不输出额外报告或问题处理对照表" in prompt
    assert "必须派发三个不同 subagent" not in prompt
    assert "不得用父 Reviewer 自己的判断替代缺失的独立审查视角" not in prompt
    assert "任一 fail 将回到 Development" not in prompt


def test_review_and_directed_repair_prompts_receive_bounded_budget_context(
    tmp_path: Path, monkeypatch: Any
) -> None:
    review = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "ticket",
            "review_budget_context": {
                "current_review_attempt": 2,
                "remaining_review_attempts": 1,
            },
        },
        name="review-budget-context",
    )
    repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "repair_source": "acceptance",
            "acceptance_artifact": {"finding": "CURRENT_FINDING"},
            "review_budget_context": {
                "completed_review_attempts": 2,
                "remaining_review_attempts": 1,
            },
        },
        name="repair-budget-context",
    )

    assert "这是本任务的第 2 次独立验收" in review
    assert "本轮结束后最多还可启动 1 次独立验收" in review
    assert "本任务已完成 2 次独立验收" in repair
    assert "最多还可启动 1 次独立验收" in repair
    for prompt in (review, repair):
        assert "不改变验收标准" in prompt
        assert "不要隐瞒、降级或放行必须修复的问题" in prompt
        assert "Review Budget Window" not in prompt
        assert "checkpoint" not in prompt


def test_run_repair_review_prompt_describes_the_merge_preview_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "run",
            "repair_scope": "run_repair",
            "parent_issue_url": "https://github.com/example/project/issues/90",
        },
        name="run-repair-review",
    )

    assert "独立验收工程师" in prompt
    assert "最终子任务及依赖" in prompt
    assert "本次修复与默认分支合并后的未提交预览" in prompt
    assert "HEAD 留在默认分支基准是正常情况" in prompt
    assert "不能用单个子任务或局部修复的通过代替整体完成" in prompt
    assert "整体开发基准与实际合并基准可能不同" in prompt


def test_publication_prompt_does_not_turn_nonblocking_evidence_into_delivery(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/90",
            "task_issue_url": "https://github.com/example/project/issues/125",
            "acceptance_artifact": {
                "checks": {
                    lane: {
                        "status": "pass",
                        "evidence": (
                            "Deferred to #117：default branch drift；"
                            "Non-blocking observation：可选重构。"
                        ),
                        "findings": [],
                    }
                    for lane in ("e2e", "standards", "spec")
                }
            },
        },
        name="publication-evidence",
    )

    assert "Deferred to #117：default branch drift" in prompt
    assert "Non-blocking observation：可选重构" in prompt
    assert "延期范围和可选建议不是本次交付成果" in prompt


def test_dynamic_context_matrix_reaches_codex_stdin_without_private_facts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = tmp_path / "PRIVATE_CHECKOUT_SENTINEL"
    checkout.mkdir()
    prompts: dict[str, str] = {}
    active_case = ""
    active_method = ""
    active_thread = ""

    publication_result = {
        "result_kind": "publication",
        "commit_message": "fix(agent): publish validated repair",
        "pr_title": "fix(agent): publish validated repair",
        "pr_body_markdown": (
            "## What Problem This Solves\n\nA blocker stopped delivery.\n\n"
            "## Why This Change Was Made\n\nThe repair restores progress.\n\n"
            "## User Impact\n\nDelivery can continue.\n\n"
            "## Evidence\n\nIndependent validation passed."
        ),
        "human_blockers": None,
    }
    acceptance_result = {
        "checks": {
            lane: {
                "status": "pass",
                "evidence": PASS_EVIDENCE[lane],
                "findings": [],
            }
            for lane in ("e2e", "standards", "spec")
        },
    }
    development_result = {
        "result_kind": "development",
        "summary": "Implemented and verified.",
        "human_blockers": None,
    }

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts[active_case] = str(options["prompt"])
        output_index = arguments.index("--output-last-message") + 1
        if active_method in {"publication", "run_publication"}:
            output = json.dumps(publication_result)
        elif active_method == "review":
            output = json.dumps(acceptance_result)
        else:
            output = json.dumps(development_result)
        Path(arguments[output_index]).write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"type":"thread.started","thread_id":"'
                + active_thread
                + '"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    parent_url = "https://github.com/example/project/issues/101"
    task_url = "https://github.com/example/project/issues/102"
    artifact = {
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "ARTIFACT_SENTINEL",
                "findings": [
                    "问题：the scenario failed；证据：ARTIFACT_SENTINEL；必须修复：repair the scenario；复验：run the scenario",
                ],
            },
            **{
                lane: {
                    "status": "pass",
                    "evidence": PASS_EVIDENCE[lane],
                    "findings": [],
                }
                for lane in ("standards", "spec")
            },
        }
    }
    ci_evidence = {"check": "CI_EVIDENCE_SENTINEL"}
    private = {
        "run_id": "PRIVATE_RUN_SENTINEL",
        "revision": "PRIVATE_REVISION_SENTINEL",
        "content_revision": "PRIVATE_CONTENT_REVISION_SENTINEL",
        "effective_revision": "PRIVATE_EFFECTIVE_REVISION_SENTINEL",
        "base_sha": "PRIVATE_BASE_SENTINEL",
        "candidate_sha": "PRIVATE_CANDIDATE_SENTINEL",
        "head_sha": "PRIVATE_HEAD_SENTINEL",
        "run_head_sha": "PRIVATE_RUN_HEAD_SENTINEL",
        "expected_merge_result": {"marker": "PRIVATE_MERGE_SENTINEL"},
        "ticket_graph": {"marker": "PRIVATE_GRAPH_SENTINEL"},
        "ticket_completion_records": ["PRIVATE_COMPLETION_SENTINEL"],
        "parent": {
            "title": "PRIVATE_PARENT_TITLE_SENTINEL",
            "body": "PRIVATE_PARENT_BODY_SENTINEL",
        },
        "ticket": {
            "title": "PRIVATE_TICKET_TITLE_SENTINEL",
            "body": "PRIVATE_TICKET_BODY_SENTINEL",
        },
        "development_summary": "PRIVATE_SUMMARY_SENTINEL",
        "existing_pr": {"title": "PRIVATE_PR_SENTINEL"},
        "attempt": "PRIVATE_ATTEMPT_SENTINEL",
        "validation_attempts": "PRIVATE_VALIDATION_ATTEMPT_SENTINEL",
    }

    cases: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = [
        (
            "ticket_development",
            "develop",
            {"acceptance_scope": "ticket", "task_issue_url": task_url},
            (parent_url, task_url),
        ),
        (
            "parent_development",
            "develop",
            {"acceptance_scope": "parent_only"},
            (parent_url,),
        ),
        (
            "ticket_acceptance_repair",
            "develop",
            {
                "acceptance_scope": "ticket",
                "task_issue_url": task_url,
                "repair_source": "acceptance",
                "acceptance_artifact": artifact,
            },
            (parent_url, task_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "ticket_checks_repair",
            "develop",
            {
                "acceptance_scope": "ticket",
                "task_issue_url": task_url,
                "repair_source": "required_checks",
                "ci_evidence": ci_evidence,
            },
            (parent_url, task_url, "CI_EVIDENCE_SENTINEL"),
        ),
        (
            "parent_acceptance_repair",
            "develop",
            {
                "acceptance_scope": "parent_only",
                "repair_source": "acceptance",
                "acceptance_artifact": artifact,
            },
            (parent_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "parent_checks_repair",
            "develop",
            {
                "acceptance_scope": "parent_only",
                "repair_source": "required_checks",
                "ci_evidence": ci_evidence,
            },
            (parent_url, "CI_EVIDENCE_SENTINEL"),
        ),
        *(
            (
                f"{scope}_integrity_repair",
                "develop",
                {
                    "acceptance_scope": scope,
                    "repair_source": "git_integrity",
                    "git_integrity_evidence": {"failure": "GIT_INTEGRITY_SENTINEL"},
                    **({"task_issue_url": task_url} if scope == "ticket" else {}),
                    **({"repair_scope": "run_repair"} if scope == "run" else {}),
                },
                (parent_url, "GIT_INTEGRITY_SENTINEL"),
            )
            for scope in ("ticket", "parent_only", "run")
        ),
        (
            "run_acceptance_repair",
            "develop",
            {
                "acceptance_scope": "run",
                "repair_scope": "run_repair",
                "repair_source": "acceptance",
                "acceptance_artifact": artifact,
            },
            (parent_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "run_checks_repair",
            "develop",
            {
                "acceptance_scope": "run",
                "repair_scope": "run_repair",
                "repair_source": "required_checks",
                "ci_evidence": ci_evidence,
            },
            (parent_url, "CI_EVIDENCE_SENTINEL"),
        ),
        (
            "run_human_repair",
            "develop",
            {
                "acceptance_scope": "run",
                "repair_scope": "run_repair",
                "repair_source": "human_revision",
                "human_feedback": "HUMAN_FEEDBACK_SENTINEL",
            },
            (parent_url, "HUMAN_FEEDBACK_SENTINEL"),
        ),
        (
            "parent_human_repair",
            "develop",
            {
                "acceptance_scope": "parent_only",
                "repair_source": "human_revision",
                "human_feedback": "HUMAN_FEEDBACK_SENTINEL",
            },
            (parent_url, "HUMAN_FEEDBACK_SENTINEL"),
        ),
        (
            "run_conflict_repair",
            "develop",
            {
                "acceptance_scope": "run",
                "repair_scope": "run_repair",
                "repair_source": "merge_conflict",
                "merge_conflict_evidence": "MERGE_CONFLICT_SENTINEL",
            },
            (parent_url, "MERGE_CONFLICT_SENTINEL"),
        ),
        (
            "ticket_validation",
            "review",
            {"acceptance_scope": "ticket", "task_issue_url": task_url},
            (parent_url, task_url),
        ),
        (
            "parent_validation",
            "review",
            {"acceptance_scope": "parent_only"},
            (parent_url,),
        ),
        (
            "run_acceptance",
            "review",
            {"acceptance_scope": "run"},
            (parent_url,),
        ),
        (
            "run_repair_acceptance",
            "review",
            {"acceptance_scope": "run", "repair_scope": "run_repair"},
            (parent_url,),
        ),
        (
            "ticket_publication",
            "publication",
            {
                "acceptance_scope": "ticket",
                "task_issue_url": task_url,
                "acceptance_artifact": artifact,
            },
            (parent_url, task_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "parent_publication",
            "publication",
            {
                "acceptance_scope": "parent_only",
                "acceptance_artifact": artifact,
            },
            (parent_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "run_repair_publication",
            "publication",
            {
                "acceptance_scope": "run",
                "acceptance_artifact": artifact,
            },
            (parent_url, "ARTIFACT_SENTINEL"),
        ),
        (
            "final_run_publication",
            "run_publication",
            {"acceptance_artifact": artifact},
            (parent_url, "ARTIFACT_SENTINEL"),
        ),
    ]

    forbidden = (
        "PRIVATE_RUN_SENTINEL",
        "PRIVATE_REVISION_SENTINEL",
        "PRIVATE_CONTENT_REVISION_SENTINEL",
        "PRIVATE_EFFECTIVE_REVISION_SENTINEL",
        "PRIVATE_BASE_SENTINEL",
        "PRIVATE_CANDIDATE_SENTINEL",
        "PRIVATE_HEAD_SENTINEL",
        "PRIVATE_RUN_HEAD_SENTINEL",
        "PRIVATE_MERGE_SENTINEL",
        "PRIVATE_GRAPH_SENTINEL",
        "PRIVATE_COMPLETION_SENTINEL",
        "PRIVATE_PARENT_TITLE_SENTINEL",
        "PRIVATE_PARENT_BODY_SENTINEL",
        "PRIVATE_TICKET_TITLE_SENTINEL",
        "PRIVATE_TICKET_BODY_SENTINEL",
        "PRIVATE_SUMMARY_SENTINEL",
        "PRIVATE_PR_SENTINEL",
        "PRIVATE_THREAD_SENTINEL",
        "PRIVATE_ATTEMPT_SENTINEL",
        "PRIVATE_VALIDATION_ATTEMPT_SENTINEL",
    )
    prior_blockers = ["  PRIOR_BLOCKER_SENTINEL must remain verbatim.  "]

    for name, method, role_request, required in cases:
        for with_human_context in (False, True):
            active_case = f"{name}_{'new_thread_blocker' if with_human_context else 'normal'}"
            active_method = method
            active_thread = (
                f"PRIVATE_THREAD_SENTINEL_{name}"
                if with_human_context
                else f"{name}-thread"
            )
            request = {
                **private,
                **role_request,
                "checkout": str(checkout),
                "parent_issue_url": parent_url,
            }
            if with_human_context:
                request.update(
                    {
                        "_invocation_mode": "new-thread",
                        "prior_human_blockers": prior_blockers,
                        "human_response_history": [
                            {"response": "OLD_RESPONSE_SENTINEL"},
                            {"response": "LATEST_RESPONSE_SENTINEL"},
                        ],
                    }
                )
            getattr(backend, method)(request)
            prompt = prompts[active_case]
            for marker in required:
                assert marker in prompt, (active_case, marker)
            for evidence_key in (
                "acceptance_artifact",
                "ci_evidence",
                "git_integrity_evidence",
            ):
                if evidence_key in role_request:
                    assert json.dumps(
                        role_request[evidence_key],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ) in prompt
            for evidence_key in ("human_feedback", "merge_conflict_evidence"):
                if evidence_key in role_request:
                    assert role_request[evidence_key] in prompt
            if with_human_context:
                assert json.dumps(prior_blockers[0], ensure_ascii=False) in prompt
                assert "回复不等于问题已经解决" in prompt
                assert "Human Blocker 恢复" not in prompt
                assert "LATEST_RESPONSE_SENTINEL" in prompt
                assert "OLD_RESPONSE_SENTINEL" not in prompt
            else:
                assert "PRIOR_BLOCKER_SENTINEL" not in prompt
            assert "PRIVATE_CHECKOUT_SENTINEL" in prompt
            for marker in forbidden:
                assert marker not in prompt, (active_case, marker)
            if method == "develop":
                assert "无需每轮固定运行完整测试" in prompt, active_case
                assert "影响范围不明或发现具体风险时仍应扩大验证" in prompt, active_case
                assert "完整测试失败时先定向定位和修复，再对收口后的代码复验" in prompt, active_case
                assert "后续审查和最终完整测试，由另一位验收工程师负责" in prompt, active_case
                assert "不把未执行或旧代码的通过当作本次结果" in prompt, active_case
                assert "程序会统一提交当前工作区的修改和未被忽略的新增文件" in prompt
                assert "只整理当前工作树" in prompt
                if role_request.get("repair_scope") == "run_repair":
                    assert "多个子任务的整体结果" in prompt, active_case
            else:
                assert "受管开发工作区" not in prompt, active_case
            assert "Controller" not in prompt, active_case
            assert "Publisher" not in prompt, active_case
            assert "读取" in prompt, active_case
            assert "验收条件" in prompt, active_case
            for internal_term in ("Ticket Contract", "Review Boundary", "Delivery Run", "Initial Development"):
                assert internal_term not in prompt, active_case
            if method == "develop":
                assert "检查全部未提交内容" in prompt, active_case
                assert "只有长期可再生且不应版本控制的" in prompt, active_case
                assert "不暂存、commit、改写 Git 历史或执行 GitHub 写入" in prompt, active_case
            elif method == "review":
                assert "除 Skill 的 Standards/Spec 审查外，你还负责 E2E 验证" in prompt, active_case
                assert "完整测试与必要检查" in prompt, active_case
                assert "Standards 与 Spec 使用静态证据" in prompt, active_case
                assert "记录实际对象、验证入口、操作或命令、预期与实际结果、必要环境" in prompt, active_case
                assert "代码、测试、依赖或环境变化后重新判断旧证据是否适用" in prompt, active_case
                assert "整个工作区保持只读" in prompt, active_case
                assert "所需修复及复验方式" in prompt, active_case
                assert "e2e 的 evidence 说明实际操作及结果" in prompt, active_case
                assert "必须严格使用" not in prompt, active_case
                assert "严格采用" not in prompt, active_case
                if name == "run_acceptance":
                    assert "任务之间的配合" in prompt
                    assert "最终用户路径" in prompt
            else:
                assert "What Problem This Solves" in prompt, active_case
                assert "实际测试或检查、结果及覆盖范围" in prompt, active_case
                assert "按变更规模调整篇幅、合并章节" in prompt, active_case
                assert "任务关联、关闭与完成信息由程序填写" in prompt, active_case
                assert "必须有四个非空二级标题" not in prompt, active_case
                assert "内部提交身份和门禁信息不写入产品叙事" in prompt, active_case


def test_run_prompts_describe_run_scope_without_controller_private_records(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checks_repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "run",
            "repair_source": "required_checks",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "ci_evidence": {"check": "failed"},
        },
        name="run-checks-repair",
    )
    run_acceptance = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "run",
            "parent_issue_url": "https://github.com/example/project/issues/1",
        },
        name="run-acceptance",
    )

    assert "下列检查失败对应当前提交" in checks_repair
    assert "多个子任务的整体结果" in checks_repair
    assert "具体任务的标题、正文和验收条件确定本次范围" not in checks_repair
    assert "Completion Record" not in run_acceptance
    assert "Expected Merge Result" not in run_acceptance
    assert "最终子任务" in run_acceptance
    assert "依赖" in run_acceptance
    assert "累计改动" in run_acceptance
    assert "任务之间的配合" in run_acceptance
    assert "最终用户路径" in run_acceptance


@pytest.mark.parametrize(
    ("follow_on_number", "follow_on_note"),
    [
        (
            "117",
            "default branch drift 属于 #117 的最新默认分支重建验收现场",
        ),
        (
            "118",
            "Git conflict 属于 #118 的 Integration-repair Worktree",
        ),
    ],
)
def test_ticket_116_keeps_follow_on_scope_out_of_development_and_findings(
    tmp_path: Path,
    monkeypatch: Any,
    follow_on_number: str,
    follow_on_note: str,
) -> None:
    parent_url = "https://github.com/example/project/issues/115"
    ticket_url = "https://github.com/example/project/issues/116"
    follow_on_url = (
        f"https://github.com/example/project/issues/{follow_on_number}"
    )
    current_finding = (
        "问题：#116 的 Candidate Run Acceptance 未覆盖完整 Parent Spec；"
        "证据：#116 的候选验收缺少完整 Parent、Ticket Set 与预期合并结果；"
        "必须修复：按 #116 合同补齐 Candidate Run Acceptance；"
        "复验：重新执行 #116 的完整 Candidate Run Acceptance。"
    )
    artifact = {
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": f"Deferred to #{follow_on_number}：{follow_on_note}",
                "findings": [current_finding],
            },
            **{
                lane: {
                    "status": "pass",
                    "evidence": PASS_EVIDENCE[lane],
                    "findings": [],
                }
                for lane in ("standards", "spec")
            },
        }
    }

    development = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": parent_url,
            "task_issue_url": ticket_url,
        },
        name=f"ticket-116-development-{follow_on_number}",
    )
    review = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": parent_url,
            "task_issue_url": ticket_url,
        },
        name=f"ticket-116-review-{follow_on_number}",
    )
    repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": parent_url,
            "task_issue_url": ticket_url,
            "repair_source": "acceptance",
            "acceptance_artifact": artifact,
        },
        name=f"ticket-116-repair-{follow_on_number}",
    )

    assert ticket_url in development
    assert ticket_url in review
    assert follow_on_url not in development
    assert follow_on_url not in review
    assert "不自动增加其他子任务的工作" in review
    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in repair
    assert f"Deferred to #{follow_on_number}：{follow_on_note}" in repair
    assert "不是自动修改指令" in repair
    assert current_finding in repair
    assert f"Deferred to #{follow_on_number}：" not in artifact["checks"]["e2e"]["findings"]


@pytest.mark.parametrize(
    ("evidence_key", "sentinel"),
    [
        ("acceptance_artifact", {"evidence": "ACCEPTANCE_EVIDENCE_SENTINEL"}),
        ("ci_evidence", {"check": "CI_EVIDENCE_SENTINEL"}),
        ("human_feedback", "HUMAN_FEEDBACK_SENTINEL"),
        ("merge_conflict_evidence", "MERGE_CONFLICT_SENTINEL"),
    ],
)
def test_candidate_run_review_prompt_excludes_run_repair_evidence(
    tmp_path: Path, monkeypatch: Any, evidence_key: str, sentinel: object
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "run",
            "candidate_acceptance": True,
            "parent_issue_url": "https://github.com/example/project/issues/1",
            evidence_key: sentinel,
        },
        name=f"candidate-run-with-{evidence_key}",
    )

    assert "独立验收工程师" in prompt
    assert "合并" in prompt
    assert "预览" in prompt
    assert "整体结果" in prompt
    assert "代替整体完成" in prompt
    assert "Run Repair Evidence (verbatim)" not in prompt
    assert json.dumps(sentinel, ensure_ascii=False, sort_keys=True) not in prompt
    for private_field in (
        "default_base_sha",
        "candidate_sha",
        "repair_base_run_head_sha",
        "expected_merge_tree",
        "inspection_command",
    ):
        assert private_field not in prompt


def test_human_blocker_resume_context_reaches_original_thread_stdin_verbatim(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        captured["arguments"] = arguments
        captured["prompt"] = str(options["prompt"])
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Access was rechecked; implementation completed.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"original-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    blockers = ["  exact blocker text; keep surrounding spaces  "]

    CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(tmp_path),
            "thread_id": "original-thread",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "prior_human_blockers": blockers,
        }
    )

    assert "resume" in captured["arguments"]
    assert "original-thread" in captured["arguments"]
    assert blockers[0] in captured["prompt"]
    assert "开发工程师" in captured["prompt"]
    assert "继续完成你负责的开发任务" in captured["prompt"]
    assert "Development Brief" not in captured["prompt"]
    assert "完整测试套件" not in captured["prompt"]
    assert "Thread" not in captured["prompt"]
    assert "Resume" not in captured["prompt"]
    assert "execution_failed" not in captured["prompt"]


def test_human_blocker_continuation_uses_only_current_blocker_and_latest_reply(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "thread_id": "blocked-development-thread",
            "prior_human_blockers": ["CURRENT_BLOCKER_SENTINEL"],
            "human_response_history": [
                {"response": "OLD_RESPONSE_SENTINEL"},
                {"response": "LATEST_RESPONSE_SENTINEL"},
            ],
        },
        name="development-human-continuation",
    )

    assert "CURRENT_BLOCKER_SENTINEL" in prompt
    assert "LATEST_RESPONSE_SENTINEL" in prompt
    assert "OLD_RESPONSE_SENTINEL" not in prompt
    assert "Human Blocker 恢复" not in prompt


def test_human_blocker_continuations_keep_each_roles_current_object_facts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shared = {
        "thread_id": "blocked-role-thread",
        "prior_human_blockers": ["CURRENT_BLOCKER_SENTINEL"],
        "human_response_history": [
            {"response": "OLD_RESPONSE_SENTINEL"},
            {"response": "LATEST_RESPONSE_SENTINEL"},
        ],
    }
    development = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            **shared,
            "acceptance_scope": "ticket",
            "parent_issue_url": "DEVELOPMENT_PARENT_SENTINEL",
            "task_issue_url": "DEVELOPMENT_TICKET_SENTINEL",
        },
        name="development-human-object-facts",
    )
    review = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            **shared,
            "acceptance_scope": "ticket",
            "current_review_identity": {
                "reviewed_base_sha": "REVIEW_BASE_SENTINEL",
                "reviewed_candidate_sha": "REVIEW_CANDIDATE_SENTINEL",
                "reviewed_candidate_tree": "REVIEW_TREE_SENTINEL",
            },
        },
        name="review-human-object-facts",
    )
    publication = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            **shared,
            "acceptance_scope": "ticket",
            "parent_issue_url": "PUBLICATION_PARENT_SENTINEL",
            "task_issue_url": "PUBLICATION_TICKET_SENTINEL",
            "acceptance_artifact": {},
        },
        name="publication-human-object-facts",
    )

    for prompt in (development, review, publication):
        assert "CURRENT_BLOCKER_SENTINEL" in prompt
        assert "LATEST_RESPONSE_SENTINEL" in prompt
        assert "OLD_RESPONSE_SENTINEL" not in prompt
        assert "Human Blocker 恢复" not in prompt
        assert "Thread" not in prompt
        assert "Resume" not in prompt
    assert "DEVELOPMENT_PARENT_SENTINEL" in development
    assert "DEVELOPMENT_TICKET_SENTINEL" in development
    assert "REVIEW_BASE_SENTINEL" in review
    assert "REVIEW_CANDIDATE_SENTINEL" in review
    assert "REVIEW_TREE_SENTINEL" in review
    assert "PUBLICATION_PARENT_SENTINEL" in publication
    assert "PUBLICATION_TICKET_SENTINEL" in publication
    assert "当前" in publication
    assert "独立验收" in publication


def test_new_semantic_repair_on_development_thread_uses_compact_repair_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "PARENT_ISSUE_SENTINEL",
            "task_issue_url": "TICKET_ISSUE_SENTINEL",
            "thread_id": "persistent-development-thread",
            "repair_source": "acceptance",
            "acceptance_artifact": {"finding": "REPAIR_SENTINEL"},
        },
        name="new-semantic-repair",
    )

    assert "开发工程师" in prompt
    assert "使用 skill:implement" in prompt
    assert "这是上一次独立审查的完整结果" in prompt
    assert "REPAIR_SENTINEL" in prompt
    assert "PARENT_ISSUE_SENTINEL" in prompt
    assert "TICKET_ISSUE_SENTINEL" in prompt
    assert "具体任务的标题、正文和验收条件确定本次范围" in prompt
    assert "程序会统一提交" in prompt
    assert "默认不再组织独立审查" in prompt
    assert "不要仅因开始新一轮修复就重复读取未变化的需求" in prompt
    assert "开始前先读取" not in prompt
    assert "Development Preflight Round" not in prompt
    assert "使用继承环境中的" not in prompt
    assert "继续完成你负责的修复任务" not in prompt


def test_new_thread_directed_repair_keeps_full_role_contract(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "_invocation_mode": "new-thread",
            "repair_source": "acceptance",
            "acceptance_artifact": {"finding": "NEW_THREAD_REPAIR_SENTINEL"},
        },
        name="new-thread-semantic-repair",
    )

    assert "这是上一次独立审查的完整结果" in prompt
    assert "开始前先读取" in prompt
    assert "保留继承的 PATH 与认证环境" in prompt
    assert "NEW_THREAD_REPAIR_SENTINEL" in prompt
    assert "已掌握的需求" not in prompt
    assert "默认不再组织独立审查" in prompt
    assert "建议安排审查子 Agent" not in prompt


def test_resume_mode_without_a_valid_thread_uses_the_full_role_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "_invocation_mode": "resume",
        },
        name="development-resume-without-thread",
    )

    assert "开始前先读取" in prompt
    assert "实现本次全部验收条件" in prompt
    assert "继续完成你负责的开发任务" not in prompt


def test_role_continuations_use_short_role_specific_prompts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact = {
        "checks": {
            lane: {
                "status": "pass",
                "evidence": PASS_EVIDENCE[lane],
                "findings": [],
            }
            for lane in ("e2e", "standards", "spec")
        }
    }
    development = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "thread_id": "development-thread",
            "_invocation_mode": "resume",
        },
        name="development-execution-continuation",
    )
    repair = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "run",
            "repair_scope": "run_repair",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "repair_source": "required_checks",
            "ci_evidence": {"check": "CURRENT_CI_SENTINEL"},
            "thread_id": "repair-thread",
            "_invocation_mode": "resume",
        },
        name="repair-execution-continuation",
    )
    review = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "acceptance_scope": "run",
            "thread_id": "reviewer-thread",
            "current_review_identity": {
                "default_base_sha": "CURRENT_BASE_SENTINEL",
                "run_head_sha": "CURRENT_HEAD_SENTINEL",
                "expected_merge_tree": "CURRENT_TREE_SENTINEL",
            },
            "previous_acceptance_artifact": {"old": "OLD_ARTIFACT_SENTINEL"},
        },
        name="reviewer-execution-continuation",
    )
    publication = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "acceptance_artifact": artifact,
            "thread_id": "publication-thread",
            "_invocation_mode": "resume",
        },
        name="publication-execution-continuation",
    )
    fallback_publication = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            "acceptance_scope": "ticket",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
            "fallback_publication_context": {
                "current_candidate_identity": {
                    "candidate_sha": "CURRENT_FALLBACK_CANDIDATE_SENTINEL"
                },
                "failure_evidence_source": "CURRENT_FAILURE_EVIDENCE_SOURCE",
            },
            "fallback_receipt": {"private": "PRIVATE_COMPLETE_RECEIPT_SENTINEL"},
            "thread_id": "fallback-publication-thread",
            "_invocation_mode": "resume",
        },
        name="fallback-publication-execution-continuation",
    )

    assert "开发工程师" in development
    assert "继续完成你负责的开发任务" in development
    assert "https://github.com/example/project/issues/1" in development
    assert "https://github.com/example/project/issues/2" in development
    assert "Development Brief" not in development
    assert "开发工程师" in repair
    assert "继续完成你负责的修复任务" in repair
    assert "https://github.com/example/project/issues/1" in repair
    assert "CURRENT_CI_SENTINEL" in repair
    assert "Development Brief" not in repair
    assert "独立验收" in review
    assert "继续完成你负责的独立验收" in review
    assert "CURRENT_BASE_SENTINEL" in review
    assert "CURRENT_HEAD_SENTINEL" in review
    assert "CURRENT_TREE_SENTINEL" in review
    assert "OLD_ARTIFACT_SENTINEL" not in review
    assert "除 Skill 的 Standards/Spec 审查外，你还负责 E2E 验证" not in review
    assert "交付说明" in publication
    assert "继续完成你负责的交付说明" in publication
    assert "https://github.com/example/project/issues/1" in publication
    assert "https://github.com/example/project/issues/2" in publication
    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in publication
    assert "当前修改没有独立验收通过的结论" in fallback_publication
    assert "CURRENT_FALLBACK_CANDIDATE_SENTINEL" in fallback_publication
    assert "CURRENT_FAILURE_EVIDENCE_SOURCE" in fallback_publication
    assert "PRIVATE_COMPLETE_RECEIPT_SENTINEL" not in fallback_publication
    assert "What Problem This Solves" not in publication
    for prompt in (
        development,
        repair,
        review,
        publication,
        fallback_publication,
    ):
        assert "Thread" not in prompt
        assert "Resume" not in prompt
        assert "execution_failed" not in prompt


def test_human_blocker_resume_rejects_a_different_reported_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Access was rechecked; implementation completed.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"different-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    with pytest.raises(CodexProcessError, match="different Thread ID"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
            {
                "checkout": str(tmp_path),
                "thread_id": "original-thread",
                "parent_issue_url": "https://github.com/example/project/issues/1",
                "prior_human_blockers": ["Grant Issue read access."],
            }
        )


@pytest.mark.parametrize("scope", ["ticket", "parent_only", "run"])
@pytest.mark.parametrize("source", [None, "acceptance", "git_integrity", "required_checks", "human_revision", "merge_conflict"])
@pytest.mark.parametrize("thread", [None, "development-thread"])
def test_custom_methods_reach_actual_development_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
    source: str | None, thread: str | None,
) -> None:
    from agent_run.prompt_resources import personal_method_directory, resolve_resources

    directory = personal_method_directory()
    directory.mkdir(parents=True)
    for name, text in (("development", "个人开发正文：检查真实边界，实现本次需求。"),
                       ("repair", "个人修复正文：定位根因。")):
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    resources = resolve_resources()
    request: dict[str, Any] = {
        "_prompt_resources": resources, "acceptance_scope": scope,
        "thread_id": thread, "parent_issue_url": "https://github.com/example/project/issues/1",
        "task_issue_url": "https://github.com/example/project/issues/2",
    }
    if source:
        request["repair_source"] = source
        field = {"acceptance": "acceptance_artifact", "git_integrity": "git_integrity_evidence",
                 "required_checks": "ci_evidence", "human_revision": "human_feedback",
                 "merge_conflict": "merge_conflict_evidence"}[source]
        request[field] = "当前原始失败证据" if source in {"human_revision", "merge_conflict"} else {"raw": "当前原始失败证据"}
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "develop", request, name="custom")
    selected = "methods/repair" if source else "methods/development"
    assert resources[selected] in prompt
    other = "methods/development" if source else "methods/repair"
    assert resources[other] not in prompt
    if source:
        assert "当前原始失败证据" in prompt
    assert "human_blocker" in prompt
    assert "summary" in prompt
    assert "只整理当前工作树，不暂存、commit、改写 Git 历史或执行 GitHub 写入" in prompt
    assert request["parent_issue_url"] in prompt
    assert str(tmp_path / "custom") in prompt


@pytest.mark.parametrize("method,scope", [("review", "ticket"), ("review", "parent_only"), ("review", "run"), ("publication", "ticket"), ("publication", "parent_only"), ("publication", "run"), ("run_publication", "run")])
def test_custom_review_and_publication_methods_reach_actual_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, scope: str,
) -> None:
    from agent_run.prompt_resources import personal_method_directory, resolve_resources

    directory = personal_method_directory()
    directory.mkdir(parents=True)
    for name, text in (("acceptance", "个人验收正文：核对当前真实对象。"),
                       ("publishing", "个人发布正文：说明实际用户变化。")):
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    resources = resolve_resources()
    prompt = _capture_public_prompt(tmp_path, monkeypatch, method, {
        "_prompt_resources": resources, "acceptance_scope": scope,
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "task_issue_url": "https://github.com/example/project/issues/2",
        "current_review_identity": {"reviewed_candidate_sha": "CURRENT_CANDIDATE"},
        "acceptance_artifact": {"raw": "当前原始验收证据"},
    }, name="custom")
    key = "methods/acceptance" if method == "review" else "methods/publishing"
    other = "methods/publishing" if method == "review" else "methods/acceptance"
    assert resources[key] in prompt
    assert resources[other] not in prompt
    assert "https://github.com/example/project/issues/1" in prompt
    assert str(tmp_path / "custom") in prompt
    if method == "review":
        assert "CURRENT_CANDIDATE" in prompt
        assert "整个工作区保持只读" in prompt
        assert all(field in prompt for field in ("e2e", "standards", "spec", "findings"))
    else:
        assert "当前原始验收证据" in prompt
        assert "不修改文件、重新验收或执行 Git/GitHub 写入" in prompt
        assert "human_blocker" in prompt
        assert "commit_message" in prompt
        assert "pr_body_markdown" in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_persisted_resources_ignore_builtin_changes_but_use_current_task_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str,
) -> None:
    import shutil
    from agent_run import prompt_resources, prompt_text_context
    from agent_run.state import StateStore

    root = tmp_path / "builtin"
    shutil.copytree(prompt_resources.RESOURCE_ROOT.parent, root)
    monkeypatch.setattr(prompt_resources, "RESOURCE_ROOT", root / "zh")
    store = StateStore(tmp_path / "state")
    resources = prompt_resources.resolve_resources(language=language)
    store.save_run("frozen", {"prompt_resources": resources, "language": language})
    role = root / language / "methods/development.md"
    role.write_text("新版内置开发角色", encoding="utf-8")
    monkeypatch.setitem(
        prompt_text_context.TEXTS["context/task-url"], language,
        "NEW INTERNAL TASK LABEL: {0}",
    )
    saved = store.load_run("frozen")
    assert saved is not None
    request = {"_prompt_resources": saved["prompt_resources"], "language": saved["language"],
               "task_issue_url": "https://github.com/example/project/issues/999"}
    old = _capture_public_prompt(tmp_path, monkeypatch, "develop", request, name="old")
    fresh = _capture_public_prompt(tmp_path, monkeypatch, "develop", {
        "task_issue_url": request["task_issue_url"], "language": language,
    }, name="new")
    assert "新版内置开发角色" not in old
    assert "新版内置开发角色" in fresh
    assert "NEW INTERNAL TASK LABEL" not in old
    assert "NEW INTERNAL TASK LABEL" in fresh
    assert request["task_issue_url"] in old and request["task_issue_url"] in fresh


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("continuation", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize(
    ("method", "facts", "role_key", "method_keys"),
    [
        ("develop", {}, "development", ("development",)),
        (
            "develop",
            {"repair_source": "required_checks", "ci_evidence": {"log": "原始 raw failure"}},
            "repair", ("repair",),
        ),
        ("review", {"acceptance_scope": "ticket"}, "review", ("acceptance",)),
        ("review", {"acceptance_scope": "run"}, "review", ("acceptance",)),
        (
            "review", {"acceptance_scope": "run", "candidate_acceptance": True,
                       "repair_scope": "run_repair"}, "review", ("acceptance",),
        ),
        ("publication", {"acceptance_scope": "ticket", "acceptance_artifact": {}},
         "publication", ("publishing",)),
        ("publication", {"acceptance_scope": "run", "acceptance_artifact": {}},
         "publication", ("publishing",)),
        ("run_publication", {"acceptance_artifact": {}}, "publication", ("publishing",)),
    ],
)
def test_selected_language_resources_reach_actual_role_calls(
    tmp_path: Path, monkeypatch: Any, language: str, continuation: bool,
    method: str, facts: dict[str, Any], role_key: str, method_keys: tuple[str, ...],
) -> None:
    from agent_run.prompt_resources import resolve_resources

    resources = resolve_resources(language=language)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    request = {**facts, "_prompt_resources": resources,
               "parent_issue_url": "https://github.com/example/project/issues/1",
               "task_issue_url": "https://github.com/example/project/issues/2"}
    if continuation:
        request.update(thread_id="fixed-thread", _invocation_mode="resume")
        if method == "review":
            request["current_review_identity"] = {"reviewed_candidate_sha": "CURRENT_CANDIDATE"}
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, method, request, name="selected-language", worker_calls=calls,
    )

    assert len(calls) == 1
    if continuation:
        resume_key = "development/repair-resume" if role_key == "repair" else f"{role_key}/resume"
        assert resources[resume_key].strip() in prompt
        assert "resume" in calls[0][0]
    else:
        for key in method_keys:
            assert resources[f"methods/{key}"].strip() in prompt
    if "ci_evidence" in facts:
        assert "原始 raw failure" in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("method", ["develop", "review", "publication"])
def test_worker_output_schema_is_separate_from_prompt_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str, method: str,
) -> None:
    from agent_run.prompt_resources import resolve_resources

    calls: list[tuple[list[str], dict[str, Any]]] = []
    prompt = _capture_public_prompt(
        tmp_path, monkeypatch, method,
        {"_prompt_resources": resolve_resources(language=language),
         "acceptance_scope": "ticket", "acceptance_artifact": {}},
        name="schema-contract", worker_calls=calls,
    )
    schema = calls[0][1]["captured_schema"]
    required = schema["required"]
    assert ("checks" if method == "review" else "result_kind") in required
    if method != "review":
        properties = schema["properties"]
        assert properties["result_kind"]["enum"] == [
            "development" if method == "develop" else "publication", "human_blocker"
        ]
        assert properties["human_blockers"]["type"] == ["array", "null"]
        assert "human_blockers" in required
        assert "human_blocker" not in properties
        if method == "publication":
            assert {"commit_message", "pr_title", "pr_body_markdown"} <= set(required)
    # Dynamic evidence may contain JSON, but static output examples must not
    # duplicate the separately supplied machine contract.
    assert '{"result_kind":' not in prompt
    assert '{"checks":{"e2e":' not in prompt
    if method == "review":
        for meaning in ("pass", "fail", "blocked", "findings", "evidence"):
            assert meaning in prompt
    else:
        assert "human_blocker" in prompt
        assert "human_blockers" in prompt
