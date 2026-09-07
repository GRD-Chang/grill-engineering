from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.delivery_loop import TicketDeliveryAdapter


PUBLICATION_BLOCKER_SHAPE = (
    '`{"result_kind":"human_blocker","commit_message":null,'
    '"pr_title":null,"pr_body_markdown":null,'
    '"human_blockers":["发生了什么；尝试了什么；人必须做什么"]}`'
)
DEVELOPMENT_BLOCKER_SHAPE = (
    '`{"result_kind":"human_blocker","summary":null,'
    '"human_blockers":["发生了什么；尝试了什么；人必须做什么"]}`'
)
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


def test_publication_prompts_use_flat_human_blocker_wire_shape(
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
        assert PUBLICATION_BLOCKER_SHAPE in prompt
        assert DEVELOPMENT_BLOCKER_SHAPE not in prompt


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
                "Previous reviewed Candidate",
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
                "Previous Run head",
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
                "Previous Repair Candidate",
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
        assert "优先核销上一轮 Findings" in prompt
        assert "repair delta" in prompt
        assert "直接回归" in prompt
        assert "缺少具体风险依据时，避免对未变化代码重复完整扫描" in prompt
        assert "当前证据或实际影响需要时，自主扩大检查范围" in prompt

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
    assert "Previous Run Acceptance Context" in r2_prompt
    assert "R1_DEFAULT_BASE" in r2_prompt
    assert "R1_RUN_HEAD" in r2_prompt
    assert "Previous Repair Candidate" not in r2_prompt


def test_fallback_publication_prompt_receives_only_minimal_projection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "publication",
        {
            "acceptance_scope": "ticket",
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
            },
        },
        name="fallback-minimal-projection",
    )

    assert "SAFE_PREVIOUS_CANDIDATE" in prompt
    assert "SAFE_CURRENT_CANDIDATE" in prompt
    assert "PRIVATE_WINDOW" not in prompt
    assert "PRIVATE_REVIEW_COUNT" not in prompt
    assert "PRIVATE_ARTIFACT" not in prompt
    assert "PRIVATE_CI" not in prompt
    assert "Fallback Publication Context" in prompt
    assert "不证明三个验收 lane 通过" in prompt
    assert "完整独立验收三条 lane 的实际证据" not in prompt
    assert "Candidate delta" in prompt


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

    assert "当前 Ticket Candidate 的独立集成验收工程师" in ticket
    assert "当前 Parent-only Candidate 的独立验收工程师" in parent
    assert "当前 Run Repair PR 的发布叙事工程师" in run_repair
    assert "独立 Fresh Acceptance 验收工程师" not in ticket
    assert "独立 Fresh Acceptance 验收工程师" not in parent


def test_non_publication_prompt_keeps_exact_human_blocker_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    development = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {"acceptance_scope": "ticket"},
        name="development",
    )

    assert DEVELOPMENT_BLOCKER_SHAPE in development
    assert PUBLICATION_BLOCKER_SHAPE not in development


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

    assert "当前 checkout 是受管开发工作区" in prompt
    assert "Git 历史只向前推进" in prompt
    assert "只修改当前 checkout 的文件树" in prompt
    assert "如果先前 Candidate 中有文件改错" in prompt
    assert "不要回退、替换或修改旧 commit" in prompt
    assert "`git log`、`git show`、`git diff` 等只读操作" in prompt
    assert "最终 diff 可以比上一轮更小" in prompt
    assert "当前 checkout 中保留的完整结果是新 Candidate Commit 的唯一内容来源" in prompt
    assert "你只整理 checkout，不创建 Candidate Commit 或执行 Git/GitHub 写入" in prompt
    assert "不得执行暂存、commit、`commit --amend`、`reset`、`rebase`" in prompt
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
    assert "`findings` 是本轮必须处理的问题" in prompt
    assert "Deferred to #N：…" in prompt
    assert "Non-blocking observation：…" in prompt
    assert "不是自动修改指令" in prompt
    assert "逐项解决当前 Review Boundary 内的每个 Finding" in prompt
    assert "按每条 Finding 自带的 `复验` 要求执行验证并取得充分、可复核的证据" in prompt
    assert "不能以一次笼统的风险验证替代逐项复验" in prompt


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

    assert "Git Integrity Repair" in prompt
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
                "Ticket Contract",
                "Parent Context",
                "sibling/follow-on Ticket",
                "task_issue_url",
            ),
            ("完整 Parent Issue；",),
        ),
        (
            "parent_only",
            ("完整 Parent Issue", "Acceptance Criteria"),
            ("Ticket Contract", "task_issue_url"),
        ),
        (
            "run",
            ("完整 Parent、最终 Ticket Set", "跨 Ticket 交互", "预期合并结果"),
            ("Ticket Contract", "task_issue_url"),
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

    assert "最小充分改动" in prompt
    assert "不增加无关行为、状态、依赖、配置、公开入口或抽象层" in prompt
    assert "不要为未来需求、其他 Ticket、假想调用方" in prompt
    assert "根据实际改动风险自主选择最低充分验证" in prompt
    assert "完整测试套件不是每轮默认的固定门槛" in prompt
    assert "共享状态、生命周期、持久化、公共接口、测试基础设施或依赖变化" in prompt
    assert "影响范围不明或具体 Finding 要求时，可以提前运行完整套件" in prompt
    assert "稳定候选是已知实现修改、相关验证及需先处理的问题已经收口" in prompt
    assert "完整套件失败后，先定向诊断、修复并验证受影响路径" in prompt
    assert "独立 Acceptance 的 E2E 负责稳定候选的完整验证" in prompt
    assert "代码、测试、依赖或相关环境变化后，重新判断旧结果的适用性" in prompt
    assert "达到完成条件后停止扩展" in prompt
    assert "先自行检查当前完整工作树、已知风险与未处理问题" in prompt
    assert "低风险局部改动可以直接收口" in prompt
    assert "默认最多进行一个 Development Preflight Round" in prompt
    assert "一轮可以包含多个不同风险方向的审查型 subagent" in prompt
    assert 'fork_turns: "none"' in prompt
    assert "完成审查所需的中立任务事实、当前范围和真实证据" in prompt
    assert "当前未提交工作树及未跟踪的交付内容" in prompt
    assert "不常规启动第二轮内部 Reviewer" in prompt
    assert "内部预检不形成 Acceptance Artifact" in prompt
    assert "根据实际改动和新发现的风险自主选择审查方式与复查强度" not in prompt
    assert "取得有效复查" not in prompt


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
def test_directed_repair_self_checks_without_internal_reviewer(
    tmp_path: Path,
    monkeypatch: Any,
    request_extra: dict[str, Any],
    evidence_marker: str,
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {"acceptance_scope": "ticket", **request_extra},
        name=f"directed-repair-{request_extra['repair_source']}",
    )

    assert evidence_marker in prompt
    assert "自行检查当前工作树并完成与风险相称的验证" in prompt
    assert "本轮不需要启动开发侧 Reviewer" in prompt
    assert "Development Preflight Round" not in prompt
    assert "审查型 subagent" not in prompt
    assert "取得有效复查" not in prompt


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
            **request_urls,
            "repair_source": "required_checks",
            "ci_evidence": {"check": "DIRECTED_REPAIR_CI_EVIDENCE"},
        },
        name=f"directed-repair-issue-context-{acceptance_scope}",
    )

    for url in required_urls:
        assert url in prompt
    assert "当前 Issue URL 用于确认本轮修复对象和需求边界" in prompt
    assert "以本轮原始 Repair Evidence、当前 checkout" in prompt
    assert "再通过只读 `gh issue view` 回查对应 Issue" in prompt
    assert "不要仅因开始本轮修复而重复读取没有变化的需求" in prompt
    assert "开始前必须通过只读 `gh issue view`" not in prompt
    assert "开始前也必须通过只读 `gh issue view`" not in prompt


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
    assert "开始前必须通过只读 `gh issue view`" in prompt
    assert "开始前也必须通过只读 `gh issue view`" in prompt
    assert "当前 Issue URL 用于确认本轮修复对象和需求边界" not in prompt


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

    assert prompts[0] == prompts[1]
    assert "Required-Checks Repair" in prompts[0]
    assert "EXACT_HEAD_CI_EVIDENCE" in prompts[0]
    assert "本轮不需要启动开发侧 Reviewer" in prompts[0]


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

    assert "E2E、Standards 和 Spec 三种独立视角" in prompt
    assert "E2E 负责当前稳定 Candidate 或合并预览的完整测试与必要检查" in prompt
    assert "记录实际验证对象、命令、exit code、结果和相关环境" in prompt
    assert "代码、测试、依赖或相关环境变化后，重新判断旧结果的适用性" in prompt
    assert "完整测试失败时提供具体失败证据和复验要求" in prompt
    assert "没有 Previous Acceptance Context 时，对完整 Review Boundary 建立基线" in prompt
    assert "Standards 与 Spec 默认使用静态证据" in prompt
    assert "Deferred to #N：…" in prompt
    assert "Non-blocking observation：…" in prompt
    assert "一次报告当前 Review Boundary 内已经能够证明的全部必须修复 Finding" in prompt
    assert "问题：…；证据：…；必须修复：…；复验：…" in prompt
    assert "确保 E2E、Standards 和 Spec 三种独立视角均形成可复核结论" in prompt
    assert "避免重复派发同类 Reviewer、嵌套相同 Review" in prompt
    assert "不规定固定 subagent 数量" not in prompt
    assert "必须派发三个不同 subagent" not in prompt
    assert "任一 fail 将回到 Development" not in prompt


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

    assert "独立 Run Repair 验收工程师" in prompt
    assert "Run Repair 的完整 Parent、最终 Ticket Set" in prompt
    assert "repair Candidate 后的无提交合并预览" in prompt
    assert "HEAD 保持 Run Branch base 是正常现象" in prompt
    assert "不得把局部 Repair Candidate 单独通过当作整体验收通过" in prompt


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

    assert "Deferred to #N：…" in prompt
    assert "Non-blocking observation：…" in prompt
    assert "不得描述为当前交付范围的交付成果、已实现能力或 User Impact" in prompt


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
        "PRIVATE_CHECKOUT_SENTINEL",
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
                assert "不表示问题已经解决" in prompt
                assert "Human Blocker 恢复" not in prompt
                assert "LATEST_RESPONSE_SENTINEL" in prompt
                assert "OLD_RESPONSE_SENTINEL" not in prompt
            else:
                assert "PRIOR_BLOCKER_SENTINEL" not in prompt
            for marker in forbidden:
                assert marker not in prompt, (active_case, marker)
            if method == "develop":
                assert "当前 checkout 是受管开发工作区" in prompt
                assert "完整测试套件不是每轮默认的固定门槛" in prompt, active_case
                assert "共享状态、生命周期、持久化、公共接口、测试基础设施或依赖变化" in prompt, active_case
                assert "影响范围不明或具体 Finding 要求时，可以提前运行完整套件" in prompt, active_case
                assert "稳定候选是已知实现修改、相关验证及需先处理的问题已经收口" in prompt, active_case
                assert "完整套件失败后，先定向诊断、修复并验证受影响路径" in prompt, active_case
                assert "独立 Acceptance 的 E2E 负责稳定候选的完整验证" in prompt, active_case
                assert "代码、测试、依赖或相关环境变化后，重新判断旧结果的适用性" in prompt, active_case
                assert "Git 历史只向前推进" in prompt
                assert "当前 checkout 中保留的完整结果是新 Candidate Commit 的唯一内容来源" in prompt
                assert "你只整理 checkout，不创建 Candidate Commit 或执行 Git/GitHub 写入" in prompt
                if role_request.get("repair_scope") == "run_repair":
                    assert "Run Repair 的完整 Parent、最终 Ticket Set" in prompt, active_case
            else:
                assert "受管开发工作区" not in prompt, active_case
            assert "Controller" not in prompt, active_case
            assert "Publisher" not in prompt, active_case
            assert "动态 Context 中的 URL 不是需求摘要" in prompt, active_case
            assert "Acceptance Criteria" in prompt, active_case
            if method == "develop":
                assert "在当前 checkout 中检查全部未提交内容" in prompt, active_case
                assert "仅长期、可再生且不应版本控制的项目产物" in prompt, active_case
                assert (
                    "不得执行暂存、commit、`commit --amend`、`reset`、`rebase`、`revert`、"
                    "`cherry-pick`"
                    in prompt
                ), active_case
            elif method == "review":
                assert "E2E、Standards 和 Spec 三种独立视角" in prompt, active_case
                assert "E2E 负责当前稳定 Candidate 或合并预览的完整测试与必要检查" in prompt, active_case
                assert "Standards 与 Spec 默认使用静态证据" in prompt, active_case
                assert "记录实际验证对象、命令、exit code、结果和相关环境" in prompt, active_case
                assert "代码、测试、依赖或相关环境变化后，重新判断旧结果的适用性" in prompt, active_case
                assert "不得修复源码、测试、配置或 `.gitignore`" in prompt, active_case
                if name == "run_acceptance":
                    assert "跨 Ticket 交互" in prompt
                    assert "预期合并结果" in prompt
            else:
                assert "What Problem This Solves" in prompt, active_case
                assert "场景 → 实际操作或命令 → 可观察结果" in prompt, active_case
                assert "CI、Candidate、SHA、门禁和生命周期" in prompt, active_case


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

    assert "Required-Checks Repair：当前 Delivery Run" in checks_repair
    assert "Required-Checks Repair：当前 Ticket" not in checks_repair
    assert "Completion Record" not in run_acceptance
    assert "Expected Merge Result" not in run_acceptance
    assert "完整 Parent、最终 Ticket Set、依赖关系、累计变更、跨 Ticket 交互和预期合并结果" in run_acceptance


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
    assert "sibling/follow-on Ticket 不会自动进入本轮范围" in review
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
    evidence_key: str, sentinel: object
) -> None:
    prompt = CodexCliBackend._review_prompt(
        {
            "acceptance_scope": "run",
            "candidate_acceptance": True,
            "parent_issue_url": "https://github.com/example/project/issues/1",
            evidence_key: sentinel,
        }
    )

    assert "你是独立 Candidate Run Acceptance 验收工程师。" in prompt
    assert (
        "当前 Validation Checkout 是将本轮 Repair Candidate 应用到当前 "
        "default head 后的预期合并结果"
    ) in prompt
    assert "仍须按完整 Run Review Boundary 验收" in prompt
    assert "不得把局部 Repair Candidate 的 diff 通过当作完整 Run 通过" in prompt
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
    assert "你是当前 Ticket 的开发工程师" in captured["prompt"]
    assert "继续完成你负责的当前开发交付" in captured["prompt"]
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
    assert "完整独立验收证据" in publication


def test_new_semantic_repair_on_development_thread_uses_full_repair_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "develop",
        {
            "acceptance_scope": "ticket",
            "thread_id": "persistent-development-thread",
            "repair_source": "acceptance",
            "acceptance_artifact": {"finding": "REPAIR_SENTINEL"},
        },
        name="new-semantic-repair",
    )

    assert "Acceptance Repair" in prompt
    assert "Development Brief" in prompt
    assert "REPAIR_SENTINEL" in prompt
    assert "继续完成你负责的当前修复交付" not in prompt


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

    assert "Development Brief" in prompt
    assert "以当前 Ticket 和代码事实为依据" in prompt
    assert "继续完成你负责的当前开发交付" not in prompt


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
                "receipt": "UNCHANGED_FALLBACK_EVIDENCE_SENTINEL"
            },
            "thread_id": "fallback-publication-thread",
            "_invocation_mode": "resume",
        },
        name="fallback-publication-execution-continuation",
    )

    assert "你是当前 Ticket 的开发工程师" in development
    assert "继续完成你负责的当前开发交付" in development
    assert "https://github.com/example/project/issues/1" in development
    assert "https://github.com/example/project/issues/2" in development
    assert "Development Brief" not in development
    assert "你是本次 Delivery Run 的修复工程师" in repair
    assert "继续完成你负责的当前修复交付" in repair
    assert "https://github.com/example/project/issues/1" in repair
    assert "CURRENT_CI_SENTINEL" in repair
    assert "Development Brief" not in repair
    assert "你是独立 Run 整体验收工程师" in review
    assert "继续完成你负责的当前独立验收" in review
    assert "CURRENT_BASE_SENTINEL" in review
    assert "CURRENT_HEAD_SENTINEL" in review
    assert "CURRENT_TREE_SENTINEL" in review
    assert "OLD_ARTIFACT_SENTINEL" not in review
    assert "E2E、Standards 和 Spec 三种独立视角" not in review
    assert "你是当前 Ticket PR 的发布叙事工程师" in publication
    assert "继续完成你负责的当前发布叙事" in publication
    assert "https://github.com/example/project/issues/1" in publication
    assert "https://github.com/example/project/issues/2" in publication
    assert "完整独立验收证据" in publication
    assert "最小 Fallback Publication Context" in fallback_publication
    assert "UNCHANGED_FALLBACK_EVIDENCE_SENTINEL" not in fallback_publication
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
