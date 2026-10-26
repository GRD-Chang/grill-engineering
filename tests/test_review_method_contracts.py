"""Paired employee instructions preserve review independence and safe completion."""
import pytest

from agent_run.config.resources import builtin_resources
from agent_run.core.prompts.development import development_prompt
from agent_run.core.prompts.review import review_continuation_prompt, review_prompt
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
