from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError


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
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"public-prompt-test"}\n',
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
                },
            },
            (
                "TICKET_CURRENT_BASE",
                "TICKET_CURRENT_CANDIDATE",
                "TICKET_CURRENT_TREE",
                "TICKET_PREVIOUS_BASE",
                "TICKET_PREVIOUS_CANDIDATE",
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


def test_development_prompt_assigns_candidate_and_publication_authority(
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

    assert "当前 checkout 是程序管理的受管开发工作区" in prompt
    assert "Git 历史只向前推进" in prompt
    assert "只修改当前 checkout 的文件树" in prompt
    assert "如果先前 Candidate 中有文件改错" in prompt
    assert "不要回退、替换或修改旧 commit" in prompt
    assert "`git log`、`git show`、`git diff` 等只读操作" in prompt
    assert "最终 diff 可以比上一轮更小" in prompt
    assert "根据当前 checkout 中保留的完整结果创建新的不可变 Candidate Commit" in prompt
    assert "执行后续 Git/GitHub 交付" in prompt
    assert "你只整理 checkout，不执行这些写入" in prompt
    assert "不得执行暂存、commit、`commit --amend`、`reset`、`rebase`" in prompt


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
    assert "达到完成条件后停止扩展" in prompt
    assert "根据实际改动和新发现的风险自主选择审查方式与复查强度" in prompt
    assert "没有具体风险依据时，避免重复或嵌套相同的 Review" in prompt
    assert "Prompt 只提供判断框架" not in prompt
    assert "两个不同 subagent" not in prompt


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
    assert "E2E 默认负责代码稳定后的广泛运行验证" in prompt
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
        for resumed in (False, True):
            active_case = f"{name}_{'resume' if resumed else 'normal'}"
            active_method = method
            active_thread = (
                f"PRIVATE_THREAD_SENTINEL_{name}"
                if resumed
                else f"{name}-thread"
            )
            request = {
                **private,
                **role_request,
                "checkout": str(checkout),
                "parent_issue_url": parent_url,
            }
            if resumed:
                request.update(
                    {
                        "thread_id": active_thread,
                        "prior_human_blockers": prior_blockers,
                    }
                )
            getattr(backend, method)(request)
            prompt = prompts[active_case]
            for marker in required:
                assert marker in prompt, (active_case, marker)
            for evidence_key in (
                "acceptance_artifact",
                "ci_evidence",
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
            if resumed:
                assert json.dumps(prior_blockers[0], ensure_ascii=False) in prompt
                assert "不表示问题已经解决" in prompt
            else:
                assert "PRIOR_BLOCKER_SENTINEL" not in prompt
            for marker in forbidden:
                assert marker not in prompt, (active_case, marker)
            if method == "develop":
                assert "当前 checkout 是程序管理的受管开发工作区" in prompt
                assert "Git 历史只向前推进" in prompt
                assert "根据当前 checkout 中保留的完整结果创建新的不可变 Candidate Commit" in prompt
                assert "你只整理 checkout，不执行这些写入" in prompt
                if role_request.get("repair_scope") == "run_repair":
                    assert "Run Repair 的完整 Parent、最终 Ticket Set" in prompt, active_case
            else:
                assert "Controller" not in prompt, active_case
                assert "Publisher" not in prompt, active_case
                assert "受管开发工作区" not in prompt, active_case
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
    expected_context = {
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "prior_human_blockers": blockers,
        "task_issue_url": "https://github.com/example/project/issues/2",
    }
    assert json.dumps(
        expected_context, ensure_ascii=False, indent=2, sort_keys=True
    ) in captured["prompt"]
    assert "不表示问题已经解决" in captured["prompt"]
    assert "重新读取权威来源、重新检查受影响工作" in captured["prompt"]


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
