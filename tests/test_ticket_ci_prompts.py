"""核验实际发给 Worker 的 Ticket CI 定向工作合同。"""
from pathlib import Path
from typing import Any

import pytest

from support.prompt_capture import _capture_public_prompt


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("mode", ["new-thread", "resume"])
@pytest.mark.parametrize("source", ["required_checks", "acceptance"])
def test_ci_development_retains_failure_scope_after_findings_and_resume(
    tmp_path: Path, monkeypatch: Any, language: str, mode: str, source: str,
) -> None:
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "develop", {
        "language": language, "acceptance_scope": "ticket", "work_source": "ci",
        "repair_source": source, "_invocation_mode": mode,
        "ci_evidence": {"head_sha": "FAILED_REVISION", "log": "RAW_RUNNER_FAILURE"},
        "acceptance_artifact": {"finding": "DIRECT_REGRESSION"},
        "thread_id": "ci-thread", "prior_human_blockers": ["missing permission"],
        "human_response_history": [{"response": "PERMISSION_RESTORED"}],
    }, name="ci-development")
    for fact in ("FAILED_REVISION", "RAW_RUNNER_FAILURE", "PERMISSION_RESTORED"):
        assert fact in prompt
    if source == "acceptance":
        assert "DIRECT_REGRESSION" in prompt
    if language == "zh":
        assert "不重新开发整套需求" in prompt
        assert "不预设只能修改代码解决" in prompt
        assert "不得删除有效测试" in prompt
    else:
        assert "do not redevelop the full requirements" in prompt
        assert "do not presume code changes are the only solution" in prompt
        assert "Do not delete valid tests" in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("mode", ["new-thread", "resume"])
def test_ci_review_verifies_repair_and_direct_regressions(
    tmp_path: Path, monkeypatch: Any, language: str, mode: str,
) -> None:
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "review", {
        "language": language, "acceptance_scope": "ticket", "work_source": "ci",
        "_invocation_mode": mode, "thread_id": "ci-review-thread",
        "current_review_identity": {"reviewed_candidate_sha": "CURRENT_REPAIR"},
        "ci_evidence": {"head_sha": "FAILED_REVISION", "log": "RAW_RUNNER_FAILURE"},
    }, name="ci-review")
    assert "FAILED_REVISION" in prompt and "RAW_RUNNER_FAILURE" in prompt
    assert ("不重新审核整套需求" if language == "zh" else "do not review the full requirements again") in prompt
    assert ("直接影响的回归" if language == "zh" else "directly affected regressions") in prompt
    assert ("伪造通过" if language == "zh" else "manufacture a pass") in prompt
    assert ("无需固定运行完整测试" if language == "zh" else "a full suite is not mandatory each round") in prompt
    assert ("按当前需求和代码检查本次完整范围" if language == "zh" else "Inspect the full scope of this task against current requirements") not in prompt
    assert ("检查比较基准到该提交的完整改动" if language == "zh" else "Inspect the complete changes from the comparison base to that commit") not in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_unreviewed_ci_publication_does_not_assume_failure_category_or_resolution(
    tmp_path: Path, monkeypatch: Any, language: str,
) -> None:
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "publication", {
        "language": language, "acceptance_scope": "ticket",
        "ci_repair_publication_context": {
            "failed_checks": {"repairability": "unknown", "log": "RAW_PLATFORM_FAILURE"},
            "current_candidate_has_additional_review": False,
        },
    }, name="ci-publication")
    assert "unknown" in prompt and "RAW_PLATFORM_FAILURE" in prompt
    if language == "zh":
        assert "不预设失败类别或宣称根因已经解决" in prompt
        assert "本次候选修复了该问题" not in prompt
        assert "明确可修复的代码检查失败" not in prompt
    else:
        assert "Do not presume a failure category or claim its root cause is resolved" in prompt
        assert "explicitly repairable code-check failure" not in prompt
        assert "This candidate addresses that failure" not in prompt
