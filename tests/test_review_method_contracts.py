"""Paired employee instructions preserve review independence and safe completion."""
import json

import pytest

from agent_run.config.resources import builtin_resources
from agent_run.contracts.artifact import parse_artifact
from agent_run.core.prompts.development import development_prompt
from agent_run.core.prompts.review import review_continuation_prompt, review_prompt
from agent_run.core.prompts.role import render_prompt
from support.offline import review_result


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("continuation", [False, True])
def test_reviewer_contract_keeps_independent_duties_and_safe_final_response(language, continuation):
    request = {"language": language, "_prompt_resources": builtin_resources(language),
               "acceptance_scope": "ticket", "task_issue_url": "https://example.invalid/issues/2",
               "current_review_identity": {"reviewed_candidate_sha": "fixed-candidate"},
               "review_context": {"facts": {}, "review_range": {
                   "base_sha": "previous-reviewed-commit", "candidate_sha": "fixed-candidate"}}}
    prompt = (review_continuation_prompt if continuation else review_prompt)(request)
    for command in ("kill", "pkill", "killall"):
        assert command in prompt
    assert ("按 PID" if language == "zh" else "by PID") in prompt
    assert ("本轮自建子进程" if language == "zh" else "child processes created by this task") in prompt
    assert ("准确句柄" if language == "zh" else "exact handles") in prompt
    assert "SIGTERM" in prompt
    assert ("有界等待退出" if language == "zh" else "wait for exit within a bound") in prompt
    assert ("祖先进程" if language == "zh" else "ancestors") in prompt
    assert ("身份不符" if language == "zh" else "identity-mismatched") in prompt
    assert ("管理域" if language == "zh" else "management domain") in prompt
    assert ("禁止 kill、pkill、killall、按 PID 清理" if language == "zh"
            else "Do not use kill, pkill or killall, clean up by PID") not in prompt
    assert ("最终结构化响应" if language == "zh" else "final structured response") in prompt
    assert ("不得继续调用工具" if language == "zh" else "do not call tools afterwards") in prompt
    assert "commentary" in prompt
    if not continuation:
        assert ('Spec、Standards、E2E 三个独立子 Agent' if language == 'zh'
                else 'three independent subagents for Spec, Standards and E2E') in prompt
        assert 'fork_turns: "none"' in prompt
        assert ("明确代码因果证据" if language == "zh" else "clear code-causal evidence") in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_review_format_repair_never_reopens_process_cleanup_or_tool_permission(language):
    from agent_run.core.prompts.context import structured_output_repair_prompt

    prompt = structured_output_repair_prompt("Acceptance Artifact", "missing field", request={
        "language": language, "_prompt_resources": builtin_resources(language)})
    assert ("调用工具" if language == "zh" else "call tools") in prompt
    assert ("只修正结果格式" if language == "zh" else "Correct only the result format") in prompt
    assert "SIGTERM" not in prompt
    assert ("本轮自建子进程" if language == "zh" else "child processes created by this task") not in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_directed_repair_allows_one_risk_based_preflight_without_the_old_and_condition(language):
    request = {"language": language, "_prompt_resources": builtin_resources(language),
               "acceptance_scope": "ticket", "repair_source": "acceptance",
               "acceptance_artifact": review_result("fail")}
    prompt = development_prompt(request)
    assert ("最多一轮" if language == "zh" else "at most one round") in prompt
    assert "Standards" in prompt and "Spec" in prompt and 'fork_turns: "none"' in prompt
    assert ("既改变原方案" if language == "zh" else "both changes the original approach") not in prompt
    assert ("自动暂存" if language == "zh" else "stage and commit") in prompt
    assert ("有证据" if language == "zh" else "evidence") in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_ticket_116_scope_keeps_parent_and_follow_on_117_118_as_background(language):
    # #120's original counterexample: #116 promotion is the immediate task;
    # default-branch drift (#117) and semantic conflicts (#118) are follow-ons.
    parent = "https://github.com/GRD-Chang/grill-engineer/issues/115"
    ticket = "https://github.com/GRD-Chang/grill-engineer/issues/116"
    deferred = ("Deferred to #117: default-branch drift and revalidation.\n"
                "Deferred to #118: semantic Git conflict resolution.")
    previous = review_result("pass")
    previous["checks"]["spec"]["evidence"] = deferred
    request = {"language": language, "_prompt_resources": builtin_resources(language),
               "acceptance_scope": "ticket", "task_issue_url": ticket, "parent_issue_url": parent}
    identity = {"reviewed_candidate_sha": "candidate-116"}
    review = {**request, "reviewed_commit": "candidate-116", "review_context": {
        "facts": {"acceptance_scope": "ticket", "current_review_identity": identity,
                  "previous_review_identity": {"reviewed_candidate_sha": "previous-116"},
                  "previous_acceptance_artifact": previous,
                  "requirements": [
                      {"number": 115, "url": parent, "revision": "parent-revision",
                       "responsibility": "background", "title": "Integrated Run delivery",
                       "body": "#116 promotion; #117 default-branch drift; #118 Git conflicts."},
                      {"number": 116, "url": ticket, "revision": "ticket-revision",
                       "responsibility": "requirements", "title": "Promote Candidate Run Acceptance",
                       "body": "Promote only when the accepted candidate and actual result match."}]},
        "review_range": {"base_sha": "previous-116", "candidate_sha": "candidate-116"}}}
    prompts = [render_prompt(request, "development"), render_prompt(review, "review"),
               render_prompt(review, "review", continuation=True)]
    for prompt in prompts:
        assert (("本次负责的具体任务：" if language == "zh" else
                 "Specific task assigned for this work: ") + ticket) in prompt
        assert (("用于理解整体需求的背景：" if language == "zh" else
                 "Background for understanding the overall requirements: ") + parent) in prompt
        assert ("具体任务的标题、正文和验收条件确定本次范围" if language == "zh" else
                "The specific task's title, body and acceptance criteria define this scope") in prompt
        assert ("不自动增加其他子任务的工作" if language == "zh" else
                "it does not automatically add work from other subtasks") in prompt
        assert ("当前修改直接造成的问题也属于本次责任" if language == "zh" else
                "Problems directly caused by the current changes are also your responsibility") in prompt
        assert (("本次负责的完整需求：" if language == "zh" else
                 "Full requirements assigned for this task: ") + parent) not in prompt
    initial_review = prompts[1]
    assert ("只列本次范围内、有实际复现或明确代码因果证据且可定位" if language == "zh" else
            "Include only problems within this task's scope supported by actual reproduction or clear code-causal evidence") in initial_review
    assert ("违反当前需求或硬性工程要求\n或造成具体风险" if language == "zh" else
            "violate current requirements or mandatory engineering requirements or create a concrete risk") in initial_review
    assert ("二者不进入 findings，不改变状态，不触发自动修复" if language == "zh" else
            "Neither belongs in findings, changes status or triggers automatic repair") in initial_review
    for prompt in prompts[1:]:
        assert json.dumps(deferred, ensure_ascii=False) in prompt
        assert '"responsibility": "background"' in prompt
        assert '"responsibility": "requirements"' in prompt
        assert "#117 default-branch drift; #118 Git conflicts" in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("risk", ["correctness", "security", "data-integrity"])
def test_ticket_116_repair_preserves_current_risk_findings_without_promoting_deferred_work(language, risk):
    finding = f"#116 {risk}: the changed promotion path accepts a mismatched candidate; repair this path."
    deferred = ("Deferred to #117: default-branch drift and revalidation.\n"
                "Deferred to #118: semantic Git conflict resolution.")
    artifact = review_result("fail")
    artifact["checks"]["spec"].update(evidence=deferred, findings=[finding])
    original = json.dumps(artifact, ensure_ascii=False).encode()
    request = {"language": language, "_prompt_resources": builtin_resources(language),
               "acceptance_scope": "ticket", "repair_source": "acceptance",
               "task_issue_url": "https://github.com/GRD-Chang/grill-engineer/issues/116",
               "parent_issue_url": "https://github.com/GRD-Chang/grill-engineer/issues/115",
               "acceptance_artifact": parse_artifact("review", original)}
    prompt = render_prompt(request, "development")
    assert ("解决当前范围内全部有证据支持的 findings" if language == "zh" else
            "Resolve all evidence-backed findings within the current scope") in prompt
    assert ("延期说明和可选建议不是自动修改指令" if language == "zh" else
            "optional Non-blocking observation suggestions in evidence are not automatic instructions to modify code") in prompt
    assert json.dumps(finding, ensure_ascii=False) in prompt
    assert json.dumps(deferred, ensure_ascii=False) in prompt
    assert request["acceptance_artifact"] == json.loads(original)
    assert request["acceptance_artifact"]["checks"]["spec"]["status"] == "fail"
    assert request["acceptance_artifact"]["checks"]["spec"]["findings"] == [finding]
