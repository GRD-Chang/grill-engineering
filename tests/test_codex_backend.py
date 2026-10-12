from __future__ import annotations

import base64
import http.client
import http.server
import io
import json
import os
import select
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from support.prompt_capture import _capture_public_prompt
from support.worker_sigint_probe import cleanup_probe_controller
from support.worker_process_timing import (
    AdvancingClock,
    run_worker_expecting_early_failure,
)

import agent_run.codex as codex_module
import agent_run.worker_sandbox as worker_sandbox_module
from agent_run.cli import main
from agent_run.cli_presentation import _next_action
from agent_run.codex import (
    CodexCliBackend,
    CodexProcessError,
    _terminal_error,
)
from agent_run.agents import PublicationResult
from agent_run.agent_invocation import invocation_event_recorder
from agent_run.github_auth import (
    GitHubCredentialError,
    _CancellableHTTPSHandler,
    _GitHubAppCredentialProvider,
    _SigningProcessRegistry,
    _create_app_jwt,
    mint_installation_credential,
)
from agent_run.github_auth_profile import GitHubAppProfile
from agent_run.worker_sandbox import (
    WorkerSandboxError,
    bubblewrap_command,
    run_worker_process,
    worker_environment,
    _BoundedJsonlStream,
)
from agent_run.credential_lifecycle import (
    InitialCredentialUnavailable,
    ReadCredential,
    WorkerCredentialError,
)

from agent_run.semantic_attempt import allocate_semantic_attempt


PASS_EVIDENCE = {
    "e2e": "操作或命令：运行候选公开流程；退出码：0；结果：候选通过端到端复验。",
    "standards": "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：请求中的全部验收标准；覆盖结论：候选完整覆盖。",
}


def passing_acceptance_artifact() -> dict[str, object]:
    return {
        "checks": {
            lane: {
                "status": "pass",
                "evidence": PASS_EVIDENCE[lane],
                "findings": [],
            }
            for lane in ("e2e", "standards", "spec")
        }
    }


def test_initial_credential_failure_does_not_start_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = False

    def unavailable() -> ReadCredential:
        raise RuntimeError("token=provider-secret temporarily unavailable")

    def worker_must_not_start(*_args: object, **_kwargs: object) -> object:
        nonlocal started
        started = True
        raise AssertionError("Worker must not start before the first credential exists")

    monkeypatch.setattr(
        "agent_run.codex.run_worker_process", worker_must_not_start
    )
    backend = CodexCliBackend(credential_provider=unavailable)

    with pytest.raises(InitialCredentialUnavailable, match="credential_unavailable"):
        backend._invoke(  # noqa: SLF001 - initial credential boundary seam
            prompt="controlled initial credential failure",
            checkout=tmp_path,
            thread_id=None,
        )

    assert not started


def test_initial_credential_failure_preserves_only_a_safe_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = False

    def unavailable() -> ReadCredential:
        raise GitHubCredentialError(
            "GitHub refused the token mint", http_status=503
        )

    def worker_must_not_start(*_args: object, **_kwargs: object) -> object:
        nonlocal started
        started = True
        raise AssertionError("Worker must not start before the first credential exists")

    monkeypatch.setattr("agent_run.codex.run_worker_process", worker_must_not_start)
    with pytest.raises(InitialCredentialUnavailable) as raised:
        CodexCliBackend(credential_provider=unavailable)._invoke(  # noqa: SLF001
            prompt="controlled HTTP credential failure",
            checkout=tmp_path,
            thread_id=None,
        )

    assert raised.value.http_status == 503
    assert not started


def test_github_app_token_mint_extracts_only_the_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = tmp_path / "private.pem"
    private_key.write_text("private-key", encoding="utf-8")
    private_key.chmod(0o600)
    profile = GitHubAppProfile("123", "456", private_key)
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *_: "jwt")

    def reject(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://api.github.com/private-response",
            429,
            "rate limited",
            {},
            None,
        )

    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", reject)
    with pytest.raises(GitHubCredentialError) as raised:
        mint_installation_credential(profile)

    assert raised.value.http_status == 429
    assert "private-response" not in str(raised.value)


def failed_acceptance_artifact(
    evidence: str = "Repair this.",
) -> dict[str, object]:
    return {
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": evidence,
                "findings": [
                    f"问题：repair is required；证据：{evidence}；必须修复：repair the candidate；复验：run the affected check",
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


def test_publication_repairs_invalid_output_in_same_thread(
    tmp_path: Path, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts: list[list[str]] = []
    prompts: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        if len(attempts) == 1:
            output.write_text('{"invalid":"publication"}', encoding="utf-8")
        else:
            output.write_text(
                json.dumps(
                    {
                        "result_kind": "publication",
                        "commit_message": "fix(delivery): publish accepted candidate",
                        "pr_title": "fix(delivery): publish accepted candidate",
                        "pr_body_markdown": "body",
                        "human_blockers": None,
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"publication-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(credential_provider=lambda: "reader-secret").publication(
        {
            "checkout": str(tmp_path),
            "acceptance_artifact": {},
            "_execution_role": "publication",
            "_execution_binding": {
                "role": "development",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "profile_revision": 1,
            },
            "_invocation_event": lambda kind, **facts: events.append((kind, facts)),
        }
    )

    assert isinstance(result, PublicationResult)
    assert len(attempts) == 2
    assert "resume" in attempts[1]
    assert "你已完成本次交付说明编写" in prompts[1]
    assert "文案结果 JSON" in prompts[1]
    assert "不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实" in prompts[1]
    assert "上一输出未通过本地 Publication Artifact contract" not in prompts[1]
    assert events[-1] == (
        "completed",
        {"reported_thread_id": "publication-thread", "attempt_count": 2},
    )
    binding_lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("Agent Execution Binding:")
    ]
    assert len(binding_lines) == 2
    assert "role=publication thread=new" in binding_lines[0]
    assert "thread_id=none" in binding_lines[0]
    assert "role=publication thread=resume" in binding_lines[1]
    assert "thread_id=publication-thread" in binding_lines[1]


def test_run_publication_repairs_empty_output_in_same_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts: list[list[str]] = []
    prompts: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        artifact = {
            "result_kind": "publication",
            "commit_message": (
                "   "
                if len(attempts) == 1
                else "fix(run): publish accepted delivery"
            ),
            "pr_title": "fix(run): publish accepted delivery",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe accepted Run needs a review boundary.\n\n"
                "## Why This Change Was Made\n\nIt preserves the explicit approval gate.\n\n"
                "## User Impact\n\nMaintainers can review one final PR.\n\n"
                "## Evidence\n\nFresh Run Acceptance passed."
            ),
            "human_blockers": None,
        }
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"run-publication-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(
        credential_provider=lambda: "reader-secret"
    ).run_publication(
        {
            "checkout": str(tmp_path),
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "acceptance_artifact": passing_acceptance_artifact(),
            "_invocation_event": lambda kind, **facts: events.append((kind, facts)),
        }
    )

    assert result["_thread_id"] == "run-publication-thread"
    assert len(attempts) == 2
    assert "resume" in attempts[1]
    assert "你已完成本次交付说明编写" in prompts[1]
    assert "文案结果 JSON" in prompts[1]
    assert "不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实" in prompts[1]
    assert "Final Run Publication" not in prompts[1]
    assert events[-1] == (
        "completed",
        {"reported_thread_id": "run-publication-thread", "attempt_count": 2},
    )


def test_run_publication_marks_exhausted_empty_output_as_failed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "result_kind": "publication",
                    "commit_message": "   ",
                    "pr_title": "Publish the completed change",
                    "pr_body_markdown": "The change is ready for review.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"run-publication-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    with pytest.raises(CodexProcessError, match="commit_message"):
        CodexCliBackend(
            credential_provider=lambda: "reader-secret"
        ).run_publication(
            {
                "checkout": str(tmp_path),
                "parent_issue_url": "https://github.com/example/project/issues/1",
                "acceptance_artifact": passing_acceptance_artifact(),
                "_invocation_event": lambda kind, **facts: events.append(
                    (kind, facts)
                ),
            }
        )

    assert events[-1][0] == "failed"
    assert events[-1][1]["attempt_count"] == 3


def test_development_repairs_invalid_output_in_same_thread_without_second_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from agent_run.prompt_resources import resolve_resources

    resources = resolve_resources()
    resources["development/output-repair"] += "\n固定格式修复资源"
    attempts: list[list[str]] = []
    prompts: list[str] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {"invalid": "development"}
                if len(attempts) == 1
                else {
                    "result_kind": "development",
                    "summary": "Reformatted the completed development result.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"development-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {"checkout": str(tmp_path), "_prompt_resources": resources}
    )

    assert result.thread_id == "development-thread"
    assert len(attempts) == 2
    assert "固定格式修复资源" in prompts[1]
    assert "固定格式修复资源" not in prompts[0]
    assert "resume" in attempts[1]
    assert "你已完成本次开发或修复" in prompts[1]
    assert "开发结果 JSON" in prompts[1]
    assert "不重新开发、审查、验证、读取项目或调用工具" in prompts[1]
    assert "上一输出未通过本地 Development result contract" not in prompts[1]


def test_reviewer_repairs_invalid_output_with_reviewer_only_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompts: list[str] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {"invalid": "acceptance"}
                if len(prompts) == 1
                else passing_acceptance_artifact()
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"reviewer-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(credential_provider=lambda: "reader-secret").review(
        {"checkout": str(tmp_path), "acceptance_scope": "ticket"}
    )

    assert result.thread_id == "reviewer-thread"
    assert len(prompts) == 2
    assert "你已完成本次独立验收" in prompts[1]
    assert "审查结果 JSON" in prompts[1]
    assert "不重新开发、审查、验证、读取项目或调用工具" in prompts[1]
    assert "上一输出未通过本地 Acceptance Artifact contract" not in prompts[1]


def test_reviewer_accepts_natural_blocker_on_first_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = "发生了什么：缺少测试凭据。已尝试：检查继承环境。需要人工做什么：提供测试访问权限。"
    artifact = passing_acceptance_artifact()
    artifact["checks"]["e2e"] = {
        "status": "blocked", "evidence": evidence, "findings": []
    }
    attempts: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments, 0,
            '{"type":"thread.started","thread_id":"reviewer-thread"}\n', ""
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(credential_provider=lambda: "reader-secret").review(
        {
            "checkout": str(tmp_path),
            "acceptance_scope": "ticket",
            "_invocation_event": lambda kind, **facts: events.append((kind, facts)),
        }
    )

    assert result.artifact == artifact
    assert len(attempts) == 1
    assert events[-1] == (
        "completed", {"reported_thread_id": "reviewer-thread", "attempt_count": 1}
    )


def test_bound_model_and_effort_are_sent_on_fresh_resume_and_output_repair(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts: list[list[str]] = []
    invalid_once = True

    def fake_run(
        arguments: list[str], **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal invalid_once
        attempts.append(arguments)
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        invalid = invalid_once
        invalid_once = False
        output.write_text(
            json.dumps(
                {"invalid": "first attempt"}
                if invalid
                else {
                    "result_kind": "development",
                    "summary": "Bound configuration was preserved.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"bound-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    binding = {
        "model": "bound-model",
        "reasoning_effort": "high",
    }
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    result = backend.develop(
        {"checkout": str(tmp_path), "_execution_binding": binding}
    )
    assert result.thread_id == "bound-thread"
    assert len(attempts) == 2
    fresh_attempts = list(attempts)
    assert "resume" not in fresh_attempts[0]
    assert "resume" in fresh_attempts[1]

    attempts.clear()
    backend.develop(
        {
            "checkout": str(tmp_path),
            "thread_id": "bound-thread",
            "_invocation_mode": "resume",
            "_execution_binding": binding,
        }
    )
    assert len(attempts) == 1
    assert "resume" in attempts[0]
    resume_attempts = list(attempts)
    for arguments in [*fresh_attempts, *resume_attempts]:
        assert _contains_pair(arguments, "--model", "bound-model")
        assert _contains_pair(
            arguments, "--config", 'model_reasoning_effort="high"'
        )


def test_bound_codex_failure_is_preserved_without_model_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts: list[list[str]] = []

    def failed_run(
        arguments: list[str], **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            1,
            '{"type":"turn.failed","error":{"message":"unsupported model"}}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", failed_run)
    with pytest.raises(CodexProcessError, match="unsupported model"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
            {
                "checkout": str(tmp_path),
                "_execution_binding": {
                    "model": "unavailable-model",
                    "reasoning_effort": "ultra",
                },
            }
        )
    assert len(attempts) == 1
    assert _contains_pair(attempts[0], "--model", "unavailable-model")
    assert _contains_pair(
        attempts[0], "--config", 'model_reasoning_effort="ultra"'
    )


def _contains_pair(arguments: list[str], option: str, value: str) -> bool:
    return any(
        arguments[index : index + 2] == [option, value]
        for index in range(len(arguments) - 1)
    )


def test_failure_resume_rechecks_current_workspace_before_development(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompts: list[str] = []

    def fake_run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Rechecked the current workspace.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"developer-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(tmp_path),
            "thread_id": "developer-thread",
            "_invocation_mode": "resume",
        }
    )

    assert "因前次调用失败而继续的同 Thread Resume" not in prompts[0]
    assert "继续完成你负责的开发任务" in prompts[0]
    assert "Development Brief:" not in prompts[0]


def test_failure_resume_rechecks_current_workspace_before_publication(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompts: list[str] = []

    def fake_run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"publication-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    CodexCliBackend(credential_provider=lambda: "reader-secret")._invoke_structured_output(
        request={"_invocation_mode": "resume"},
        prompt="Publication stage prompt",
        checkout=tmp_path,
        thread_id="publication-thread",
        schema={},
        output_name="Publication Artifact",
        validate=lambda _value: None,
        initial_writable_checkout=False,
    )

    assert "因前次调用失败而继续的同 Thread Resume" not in prompts[0]
    assert prompts[0] == "Publication stage prompt"


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (
            '{"type":"error","error":{"message":"api_key=key-value\\u0000 failed"}}\n',
            "less useful stderr",
            "api_key=[REDACTED] failed",
        ),
        (
            '{"type":"turn.failed","error":{"message":"password=bad-value denied"}}\n',
            "less useful stderr",
            "password=[REDACTED] denied",
        ),
        (
            '{"type":"turn.failed","error":"authorization=bad-value denied"}\n',
            "less useful stderr",
            "authorization=[REDACTED] denied",
        ),
        (
            '{malformed jsonl}\nnot-json\n',
            "secret=stderr-value failed",
            "secret=[REDACTED] failed",
        ),
    ],
)
def test_terminal_error_extracts_real_jsonl_failure_shapes(
    stdout: str, stderr: str, expected: str
) -> None:
    assert _terminal_error(stdout, stderr) == expected


def test_terminal_error_prefers_task_complete_then_top_level_error_then_turn_failed() -> None:
    stdout = "\n".join(
        (
            '{"type":"turn.failed","error":{"message":"turn failed"}}',
            '{"type":"error","error":{"message":"top-level error"}}',
            '{"type":"task_complete","error":{"message":"task complete error"}}',
        )
    )

    assert _terminal_error(stdout, "stderr error") == "task complete error"
    assert _terminal_error(
        "\n".join(stdout.splitlines()[:2]), "stderr error"
    ) == "top-level error"


def test_terminal_error_strips_controls_redacts_and_bounds_utf8() -> None:
    assert _terminal_error(
        '{"type":"task_complete","error":{"message":"token=secret-value\\u0000 failed"}}\n',
        "less useful stderr",
    ) == "token=[REDACTED] failed"
    assert len(_terminal_error("", "密" * 9000).encode()) <= 8192


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Authorization: Bearer sk-live-secret",
            "Authorization: [REDACTED]",
        ),
        (
            "proxy-authorization=Basic dXNlcjpwYXNzd29yZA==",
            "proxy-authorization=[REDACTED]",
        ),
        (
            '{"authorization":"Bearer sk-json-secret"}',
            '{"authorization":"[REDACTED]"}',
        ),
    ],
)
def test_terminal_error_redacts_complete_authorization_credentials(
    message: str, expected: str
) -> None:
    assert _terminal_error("", message) == expected


def test_publication_failure_event_never_exposes_authorization_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout='{"type":"thread.started","thread_id":"publication-thread"}\n',
            stderr="Authorization: Bearer sk-persisted-secret",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    with pytest.raises(CodexProcessError, match=r"Authorization: \[REDACTED\]"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").publication(
            {
                "checkout": str(tmp_path),
                "acceptance_artifact": {},
                "_invocation_event": lambda kind, **facts: events.append(
                    (kind, facts)
                ),
            }
        )

    failed = events[-1]
    assert failed[0] == "failed"
    assert failed[1]["error"] == "Authorization: [REDACTED]"
    assert "sk-persisted-secret" not in repr(events)


def test_terminal_error_projects_embedded_api_error_json() -> None:
    message = (
        "API request failed: "
        '{"request_id":"req-sensitive","status":429,'
        '"error":{"type":"invalid_request_error","code":"invalid_api_key",'
        '"message":"api_key=secret-value denied","param":null,'
        '"headers":{"authorization":"Bearer sensitive"},'
        '"response_body":{"customer":"private"}}}'
        "; contact upstream support"
    )
    stdout = json.dumps(
        {"type": "turn.failed", "error": {"message": message}}
    )

    assert _terminal_error(stdout, "less useful stderr") == (
        '{"type":"invalid_request_error","code":"invalid_api_key",'
        '"status":429,"message":"api_key=[REDACTED] denied","param":null}'
    )


def test_terminal_error_keeps_plain_text_message() -> None:
    message = "plain upstream failure without an API response"
    stdout = json.dumps(
        {"type": "task_complete", "error": {"message": message}}
    )

    assert _terminal_error(stdout, "less useful stderr") == message


def test_codex_worker_environment_excludes_publisher_credentials(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    captured: dict[str, str] = {}
    captured_arguments: list[str] = []
    gh_config: Path | None = None

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal gh_config
        captured_arguments.extend(arguments)
        environment = options["environment"]
        captured.update(environment)
        gh_config = Path(environment["GH_CONFIG_DIR"])
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Implemented and tested.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-1"}\n',
            stderr="",
        )

    monkeypatch.setenv("GH_TOKEN", "publisher-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "publisher-secret-2")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "publisher-enterprise-secret")
    monkeypatch.setenv(
        "GITHUB_ENTERPRISE_TOKEN", "publisher-enterprise-secret-2"
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/publisher-agent")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "publisher-key")
    monkeypatch.setenv("AGENT_RUN_GITHUB_READ_TOKEN", "legacy-secret")
    monkeypatch.setenv(
        "AGENT_RUN_GITHUB_READ_PERMISSIONS", '{"issues":"write"}'
    )
    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    result = CodexCliBackend(
        credential_provider=lambda: "reader-secret",
    ).develop(
        {
            "checkout": str(checkout),
            "thread_id": None,
            "ticket": {"number": 3},
        }
    )

    assert result.thread_id == "thread-1"
    assert "GH_TOKEN" not in captured
    assert "GITHUB_TOKEN" not in captured
    assert "GH_ENTERPRISE_TOKEN" not in captured
    assert "GITHUB_ENTERPRISE_TOKEN" not in captured
    assert "SSH_AUTH_SOCK" not in captured
    assert "AGENT_RUN_GITHUB_APP_PRIVATE_KEY" not in captured
    assert "AGENT_RUN_GITHUB_READ_TOKEN" not in captured
    assert "AGENT_RUN_GITHUB_READ_PERMISSIONS" not in captured
    assert captured["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert not any(
        Path(entry).name == "gh-adapter"
        for entry in captured["PATH"].split(os.pathsep)
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in captured_arguments
    assert "--sandbox" not in captured_arguments
    assert gh_config is not None and not gh_config.exists()
    assert os.environ["GH_TOKEN"] == "publisher-secret"


def test_non_gh_bwrap_failure_stays_execution_failure(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_directory = tmp_path / "gh-bin"
    target_directory.mkdir()
    target = target_directory / "gh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    worker = tmp_path / "codex-worker"
    marker = tmp_path / "payload-started"
    worker.write_text(
        f"#!/bin/sh\ntouch {marker}\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    bwrap_directory = tmp_path / "bwrap-bin"
    bwrap_directory.mkdir()
    bwrap_wrapper = bwrap_directory / "bwrap"
    real_bwrap = shutil.which("bwrap")
    assert real_bwrap is not None
    bwrap_wrapper.write_text(
        "#!/bin/sh\n"
        "mv \"$TEST_BWRAP_SOURCE\" \"$TEST_BWRAP_BACKUP\"\n"
        "\"$TEST_BWRAP_REAL\" \"$@\"\n"
        "status=$?\n"
        "mv \"$TEST_BWRAP_BACKUP\" \"$TEST_BWRAP_SOURCE\"\n"
        "exit $status\n",
        encoding="utf-8",
    )
    bwrap_wrapper.chmod(0o700)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    codex_home_backup = tmp_path / "codex-home-backup"
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(bwrap_directory), str(target_directory), os.defpath)),
    )
    monkeypatch.setenv("CODEX_INSTALL_DIR", str(tmp_path / "codex-install"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TEST_BWRAP_SOURCE", str(codex_home))
    monkeypatch.setenv("TEST_BWRAP_BACKUP", str(codex_home_backup))
    monkeypatch.setenv("TEST_BWRAP_REAL", real_bwrap)

    with pytest.raises(CodexProcessError) as raised:
        CodexCliBackend(executable=str(worker), credential_provider=lambda: "reader")._invoke(
            prompt="non-gh binding failure",
            checkout=git_repo,
            thread_id=None,
        )

    assert "worker_gh_binding_failed" not in str(raised.value)
    assert "bwrap" in str(raised.value).casefold()
    assert codex_home.is_dir()
    assert not codex_home_backup.exists()
    assert not marker.exists()


def test_codex_worker_uses_development_invocation_deadline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    timeouts: list[float] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(options["timeout"])
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Implemented and tested.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"thread-1"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {"checkout": str(checkout), "ticket": {"number": 3}}
    )

    assert timeouts == [pytest.approx(5 * 60 * 60, abs=1)]


def test_deadline_exhausted_before_output_repair_does_not_count_unstarted_process(
    tmp_path: Path, monkeypatch: Any
) -> None:
    clock = [0.0]
    events: list[tuple[str, dict[str, object]]] = []
    process_count = 0

    def fake_run(
        arguments: list[str], **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal process_count
        process_count += 1
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text('{"invalid": true}', encoding="utf-8")
        clock[0] = 9.0
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"deadline-thread"}\n',
            "",
        )

    def expire_during_validation(_output: object) -> None:
        clock[0] = 11.0
        raise ValueError("invalid structured output")

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: clock[0])

    with pytest.raises(CodexProcessError, match="Invocation Deadline"):
        CodexCliBackend(credential_provider=lambda: "reader-secret")._invoke_structured_output(
            request={
                "_invocation_deadline_seconds": 10,
                "_invocation_event": lambda kind, **facts: events.append(
                    (kind, facts)
                ),
            },
            prompt="produce structured output",
            checkout=tmp_path,
            thread_id=None,
            schema={"type": "object"},
            output_name="Test output",
            validate=expire_during_validation,
            initial_writable_checkout=False,
        )

    assert process_count == 1
    assert events[-1] == (
        "failed",
        {
            "attempt_count": 1,
            "error": "Test output exceeded its Invocation Deadline",
            "return_code": None,
            "signal": None,
            "execution_not_started": False,
        },
    )


def test_output_repair_receives_only_the_remaining_invocation_deadline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    clock = [0.0]
    timeouts: list[float] = []
    absolute_deadlines: list[float] = []
    real_bubblewrap_command = codex_module.bubblewrap_command

    def setup_worker(*args: Any, **kwargs: Any) -> list[str]:
        clock[0] += 2.0
        return real_bubblewrap_command(*args, **kwargs)

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(options["timeout"])
        absolute_deadlines.append(options["deadline_at_monotonic"])
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {"invalid": "first output"}
                if len(timeouts) == 1
                else {
                    "result_kind": "development",
                    "summary": "Repaired within the original deadline.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        clock[0] += 4.0 if len(timeouts) == 1 else 1.0
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"type":"thread.started","thread_id":"deadline-thread"}\n',
            "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent_run.codex.bubblewrap_command", setup_worker)

    result = CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(tmp_path),
            "_invocation_deadline_seconds": 10,
        }
    )

    assert result.summary == "Repaired within the original deadline."
    assert timeouts == [8, 2]
    assert absolute_deadlines == [10, 10]


def test_deadline_exhausted_during_worker_setup_does_not_start_output_attempt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    clock = [0.0]
    events: list[tuple[str, dict[str, object]]] = []
    process_count = 0
    real_bubblewrap_command = codex_module.bubblewrap_command

    def expire_during_setup(*args: Any, **kwargs: Any) -> list[str]:
        clock[0] = 11.0
        return real_bubblewrap_command(*args, **kwargs)

    def fake_run(
        _arguments: list[str], **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal process_count
        process_count += 1
        raise AssertionError("expired Invocation must not start a Worker process")

    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent_run.codex.bubblewrap_command", expire_during_setup)
    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    with pytest.raises(CodexProcessError, match="Invocation Deadline"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_deadline_seconds": 10,
                "_invocation_event": lambda kind, **facts: events.append(
                    (kind, facts)
                ),
            }
        )

    assert process_count == 0
    assert events[-1] == (
        "failed",
        {
            "attempt_count": 0,
            "error": "Development result exceeded its Invocation Deadline",
            "return_code": None,
            "signal": None,
            "execution_not_started": True,
        },
    )


def test_deadline_expiry_persists_execution_failure_and_resume_action(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "blocking-codex"
    worker.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    worker.chmod(0o700)
    job: dict[str, Any] = {}
    semantic_attempt = allocate_semantic_attempt(
        job,
        role="development",
        work_subject="ticket:3",
        generation=1,
        currentness_boundary={"base_sha": "base"},
        ordinal=1,
        budget_window=1,
    )
    state: dict[str, Any] = {
        "run_id": "run-deadline",
        "ticket_jobs": {"3": job},
        "agent_invocation_history": [],
        "diagnostics": [],
        "status": "active",
    }
    saved: list[dict[str, Any]] = []
    record = invocation_event_recorder(
        state,
        role="development",
        phase="developing",
        work_subject="ticket:3",
        generation=1,
        invocation_input={"checkout": str(tmp_path)},
        currentness_boundary={"base_sha": "base"},
        semantic_attempt=semantic_attempt,
        save=lambda value: saved.append(json.loads(json.dumps(value))),
        invocation_deadline_seconds=0.1,
    )

    with pytest.raises(CodexProcessError, match="Invocation Deadline"):
        CodexCliBackend(
            executable=str(worker), credential_provider=lambda: "reader-secret"
        ).develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": record,
            }
        )

    invocation = state["active_agent_invocation"]
    assert state["status"] == "execution_failed"
    assert state["terminal_kind"] == "execution_failed"
    assert invocation["status"] == "failed"
    assert invocation["error"] == (
        "Development result exceeded its Invocation Deadline"
    )
    assert invocation["deadline_seconds"] == 0.1
    assert invocation["deadline_at"]
    assert saved[-1]["active_agent_invocation"] == invocation
    assert _next_action(state) == "agent-run resume run-deadline"


def test_codex_prompts_require_independent_development_and_acceptance_lanes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    prompts: list[str] = []
    schemas: list[dict[str, Any]] = []

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        output = json.dumps(
            {
                "result_kind": "development",
                "summary": "Implemented and tested.",
                "human_blockers": None,
            }
        )
        if "--output-schema" in arguments:
            schema_index = arguments.index("--output-schema") + 1
            schema = json.loads(Path(arguments[schema_index]).read_text(encoding="utf-8"))
            schemas.append(schema)
            if "checks" in schema["properties"]:
                output = json.dumps(passing_acceptance_artifact())
        Path(arguments[output_index]).write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"fresh-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")

    backend.develop(
        {
            "checkout": str(checkout),
            "thread_id": None,
            "ticket": {"number": 3},
        }
    )
    backend.review(
        {
            "checkout": str(checkout),
            "ticket": {"number": 3},
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
        }
    )

    development, acceptance = prompts
    assert "skill:implement" in development
    assert "skill:code-review" in development
    assert "建议安排审查子 Agent" in development
    assert "本次开发最多组织一轮" in development
    assert 'fork_turns: "none"' in development
    assert "不反复启动通用审查" in development
    assert "Prompt 只提供判断框架" not in development
    assert "除 Skill 的 Standards/Spec 审查外，你还负责 E2E 验证" in acceptance
    assert "完整测试与必要检查" in acceptance
    assert "Standards 与 Spec 使用静态证据" in acceptance
    assert "skill:code-review" in acceptance
    assert "对三个维度的最终结果负责" in acceptance
    assert 'fork_turns: "none"' in acceptance
    assert "使当前交付不可接受" in acceptance
    assert "同一根因合并为一条" in acceptance
    assert "不得用父 Reviewer 自己的判断替代缺失的独立审查视角" not in acceptance
    assert "问题、证据、所需修复及复验方式" in acceptance
    assert "不跨维度重复报告" in acceptance
    assert "pass 与 blocked 的 findings 为空" in acceptance
    assert "在 evidence 中说明原因、已尝试的办法和人必须做什么" in acceptance
    assert "\"verdict\"" not in acceptance
    assert "\"human_blockers\"" not in acceptance
    assert len(schemas) == 2
    assert all("allOf" not in schema for schema in schemas)
    assert schemas[0]["required"] == ["result_kind", "summary", "human_blockers"]


def test_publication_prompts_require_semantic_titles(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompts: list[str] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"publication-test"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    ticket_checkout = tmp_path / "ticket-publication"
    run_checkout = tmp_path / "run-publication"
    ticket_checkout.mkdir()
    run_checkout.mkdir()
    backend.publication(
        {
            "checkout": str(ticket_checkout),
            "acceptance_scope": "ticket",
            "acceptance_artifact": {},
        }
    )
    backend.publication(
        {
            "checkout": str(run_checkout),
            "acceptance_scope": "run",
            "acceptance_artifact": {},
        }
    )
    ticket_prompt, run_prompt = prompts

    assert "提交说明和 PR 标题默认使用 Conventional Commit 形式" in ticket_prompt
    assert "提交说明和 PR 标题默认使用 Conventional Commit 形式" in run_prompt
    assert "`Primary Ticket: #" not in ticket_prompt
    assert "内部提交身份和门禁信息不写入产品叙事" in run_prompt


def test_run_publication_prompt_reserves_identity_for_publisher(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    prompts: list[str] = []

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "publication",
                    "commit_message": "feat: publish validated run",
                    "pr_title": "feat: publish validated run",
                    "pr_body_markdown": (
                        "## What Problem This Solves\n\nA complete Run needs a review boundary.\n\n"
                        "## Why This Change Was Made\n\nIt preserves the explicit approval gate.\n\n"
                        "## User Impact\n\nMaintainers can review one final PR.\n\n"
                        "## Evidence\n\nFresh Run Acceptance passed."
                    ),
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"run-publication"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    CodexCliBackend(credential_provider=lambda: "reader-secret").run_publication(
        {
            "checkout": str(tmp_path),
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "base_sha": "base-sha",
            "run_head_sha": "run-head-sha",
            "acceptance_artifact": passing_acceptance_artifact(),
        }
    )

    assert "本次负责的完整需求" in prompts[0]
    assert "https://github.com/example/project/issues/1" in prompts[0]
    assert "base-sha" not in prompts[0]
    assert "run-head-sha" not in prompts[0]
    assert "一次性的" not in prompts[0]
    assert "Codex" not in prompts[0]
    assert "Worker" not in prompts[0]


@pytest.mark.parametrize(
    ("request_extra", "mode_text", "evidence"),
    [
        ({}, "实现本次全部验收条件", None),
        (
            {
                "repair_source": "acceptance",
                "acceptance_artifact": failed_acceptance_artifact(
                    "Preserve this exact acceptance evidence."
                ),
            },
            "这是上一次独立审查的完整结果",
            "Preserve this exact acceptance evidence.",
        ),
        (
            {
                "repair_source": "required_checks",
                "ci_evidence": {
                    "check": "required-check-verbatim",
                    "log": "Preserve this exact CI evidence.",
                },
            },
            "准确失败版本的 CI 问题",
            "Preserve this exact CI evidence.",
        ),
    ],
)
def test_development_prompt_matches_normal_and_repair_contracts(
    tmp_path: Path,
    monkeypatch: Any,
    request_extra: dict[str, Any],
    mode_text: str,
    evidence: str | None,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    prompts: list[str] = []

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Implemented and tested.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"developer-1"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    request = {
        "checkout": str(checkout),
        "thread_id": None,
        "ticket": {"number": 3},
        **request_extra,
    }

    CodexCliBackend(
        credential_provider=lambda: "reader-secret",
    ).develop(request)

    prompt = prompts[0]
    assert mode_text in prompt
    assert "实际风险验证受影响功能" in prompt
    assert "影响范围不明或发现具体风险时仍应扩大验证" in prompt
    if request_extra:
        assert "直接影响的同类场景和本次修复可能造成的回归" in prompt
        assert "默认不再组织独立审查" in prompt
        assert "既改变原方案、又引入此前未覆盖的关键风险" in prompt
        assert "才允许一次针对该风险的审查" in prompt
        assert "Development Preflight Round" not in prompt
    else:
        assert "建议安排审查子 Agent" in prompt
        assert "本次开发最多组织一轮" in prompt
        assert 'fork_turns: "none"' in prompt
    if evidence is not None:
        assert evidence in prompt
        source = (
            request_extra["acceptance_artifact"]
            if "acceptance_artifact" in request_extra
            else request_extra["ci_evidence"]
        )
        assert json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) in (
            prompt
        )


def test_merge_conflict_prompt_excludes_unresolved_acceptance_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    artifact = failed_acceptance_artifact("Preserve this Candidate finding.")
    prompts: list[str] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Resolved the merge conflict.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"developer-1"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(checkout),
            "acceptance_scope": "run",
            "repair_scope": "run_repair",
            "repair_source": "merge_conflict",
            "merge_conflict_evidence": "Unresolved paths:\nshared.txt",
            "acceptance_artifact": artifact,
            "parent_issue_url": "https://github.com/example/project/issues/1",
        }
    )

    prompt = prompts[0]
    assert "解决当前工作树中的真实冲突" in prompt
    assert "Unresolved paths:\nshared.txt" in prompt
    assert "Preserve this Candidate finding." not in prompt
    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) not in prompt


def test_top_level_prompts_allow_only_issue_urls_and_original_evidence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact = failed_acceptance_artifact("original")
    internal = {
        "checkout": "/private/checkout",
        "thread_id": "private-thread",
        "run_id": "private-run",
        "base_sha": "private-base-sha",
        "candidate_sha": "private-candidate-sha",
        "ticket_graph": {"private": "graph"},
        "parent": {"body": "private parent body"},
        "ticket": {"body": "private ticket body"},
    }
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    request = {
        **internal,
        "checkout": str(checkout),
        "acceptance_scope": "ticket",
        "repair_source": "acceptance",
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "task_issue_url": "https://github.com/example/project/issues/2",
        "acceptance_artifact": artifact,
    }

    prompts: list[str] = []
    outputs = [
        {
            "result_kind": "development",
            "summary": "Implemented and verified.",
            "human_blockers": None,
        },
        {
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
        },
        passing_acceptance_artifact(),
    ]

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(str(options["prompt"]))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(outputs[len(prompts) - 1]), encoding="utf-8"
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"private-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    backend.develop(request)
    backend.publication(request)
    backend.review(request)
    development, publication, review = prompts

    for prompt in (development, publication, review):
        assert "https://github.com/example/project/issues/1" in prompt
        assert "private parent body" not in prompt
        assert "private ticket body" not in prompt
        assert "private-base-sha" not in prompt
        assert "private-candidate-sha" not in prompt
        assert "private-run" not in prompt
        assert "private-thread" not in prompt
    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in development
    assert json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) in publication


def test_development_human_blocker_is_an_exact_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "human_blocker",
                    "summary": None,
                    "human_blockers": [
                        "GitHub denied Issue read; tried gh issue view; grant read access."
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"blocked-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(tmp_path),
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
        }
    )

    from agent_run.agents import HumanBlockerResult

    assert isinstance(result, HumanBlockerResult)
    assert result.thread_id == "blocked-thread"
    assert result.human_blockers == (
        "GitHub denied Issue read; tried gh issue view; grant read access.",
    )


@pytest.mark.parametrize(
    "malformed",
    [
        {
            "human_blockers": ["Grant Issue read access."],
            "reason": "GitHub rejected the request.",
        },
        {"status": "blocked"},
        ["Grant Issue read access."],
    ],
)
def test_development_rejects_non_exact_structured_results(
    tmp_path: Path, monkeypatch: Any, malformed: object
) -> None:
    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(malformed),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"blocked-thread"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    with pytest.raises(CodexProcessError, match="Development result|development result"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
            {
                "checkout": str(tmp_path),
                "parent_issue_url": "https://github.com/example/project/issues/1",
                "task_issue_url": "https://github.com/example/project/issues/2",
            }
        )


def test_development_resume_failure_stops_without_replacement_thread(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    invocations: list[tuple[list[str], str]] = []

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        invocations.append((arguments, str(options["prompt"])))
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr="resume target no longer exists",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    with pytest.raises(CodexProcessError, match="resume target no longer exists"):
        CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
            {
                "checkout": str(checkout),
                "thread_id": "developer-1",
                "parent_issue_url": "https://github.com/example/project/issues/1",
                "task_issue_url": "https://github.com/example/project/issues/3",
                "acceptance_artifact": failed_acceptance_artifact(),
                "repair_source": "acceptance",
            }
        )

    assert len(invocations) == 1
    assert "resume" in invocations[0][0]


def test_development_avoids_streaming_callback_for_legacy_worker_shim(
    tmp_path: Path, monkeypatch: Any
) -> None:
    callback_options: list[object] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        callback_options.append(options.get("on_stdout_line"))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "result_kind": "development",
                    "summary": "Development completed.",
                    "human_blockers": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"developer-1"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)

    CodexCliBackend(credential_provider=lambda: "reader-secret").develop(
        {
            "checkout": str(tmp_path),
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/3",
        }
    )

    assert callback_options == [None]


def test_publication_resume_failure_does_not_replace_thread(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    invocations: list[tuple[list[str], str]] = []

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        invocations.append((arguments, str(options["prompt"])))
        if len(invocations) == 1:
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout="",
                stderr="resume target no longer exists",
            )
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(
                {
                    "commit_message": "fix(delivery): repair publication",
                    "pr_title": "fix(delivery): repair publication",
                    "pr_body_markdown": (
                        "## What Problem This Solves\n\nLost publication.\n\n"
                        "## Why This Change Was Made\n\nRecover the thread.\n\n"
                        "## User Impact\n\nDelivery continues.\n\n"
                        "## Evidence\n\nThe candidate was preserved."
                    ),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"developer-2"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    with pytest.raises(CodexProcessError, match="resume target no longer exists"):
        CodexCliBackend(
            credential_provider=lambda: "reader-secret",
        ).publication(
            {
                "checkout": str(checkout),
                "thread_id": "developer-1",
                "parent_issue_url": "https://github.com/example/project/issues/1",
                "task_issue_url": "https://github.com/example/project/issues/3",
                "acceptance_artifact": passing_acceptance_artifact(),
            }
        )

    assert len(invocations) == 1
    assert "resume" in invocations[0][0]


def test_publication_uses_a_read_only_checkout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls: list[dict[str, Any]] = []

    def fake_invoke(self: CodexCliBackend, **options: Any) -> tuple[str, str]:
        del self
        calls.append(options)
        return (
            json.dumps(
                {
                    "result_kind": "publication",
                    "commit_message": "fix(delivery): publish accepted candidate",
                    "pr_title": "fix(delivery): publish accepted candidate",
                    "pr_body_markdown": (
                        "## What Problem This Solves\n\nPublication needs an immutable candidate.\n\n"
                        "## Why This Change Was Made\n\nThe Publisher owns release writes.\n\n"
                        "## User Impact\n\nThe accepted change stays stable.\n\n"
                        "## Evidence\n\nFresh validation passed."
                    ),
                    "human_blockers": None,
                }
            ),
            "development-thread",
        )

    monkeypatch.setattr(CodexCliBackend, "_invoke", fake_invoke)
    CodexCliBackend(credential_provider=lambda: "reader-secret").publication(
        {
            "checkout": str(checkout),
            "thread_id": "development-thread",
            "acceptance_artifact": {},
        }
    )

    assert len(calls) == 1
    assert calls[0]["writable_checkout"] is False


def test_reviewer_uses_a_read_only_checkout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls: list[dict[str, Any]] = []

    def fake_invoke(self: CodexCliBackend, **options: Any) -> tuple[str, str]:
        del self
        calls.append(options)
        return json.dumps(passing_acceptance_artifact()), "reviewer-thread"

    monkeypatch.setattr(CodexCliBackend, "_invoke", fake_invoke)
    CodexCliBackend(credential_provider=lambda: "reader-secret").review(
        {
            "checkout": str(checkout),
            "ticket": {"number": 3},
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
        }
    )

    assert len(calls) == 1
    assert calls[0]["writable_checkout"] is False


def test_reviewer_prompt_requires_external_temporary_paths_for_writes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt = _capture_public_prompt(
        tmp_path,
        monkeypatch,
        "review",
        {
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "task_issue_url": "https://github.com/example/project/issues/2",
        },
        name="reviewer-external-temporary-paths",
    )

    assert "整个工作区保持只读" in prompt
    assert "工作区外仅服务本轮的可定位临时路径" in prompt
    assert "结束前关闭本轮启动的服务和浏览器" in prompt
    assert "清理临时副本及其他本轮临时产物" in prompt


def test_production_reviewer_cannot_create_modify_or_delete_checkout_files(
    git_repo: Path, tmp_path: Path
) -> None:
    worker = tmp_path / "reviewer-worker"
    worker.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

failures = []
for name, operation in (
    ("create", lambda: pathlib.Path("reviewer-mutated.txt").write_text("created\\n")),
    ("modify", lambda: pathlib.Path("README.md").write_text("modified\\n")),
    ("delete", lambda: pathlib.Path("README.md").unlink()),
):
    try:
        operation()
    except OSError:
        failures.append(name)
if failures != ["create", "modify", "delete"]:
    raise RuntimeError("Reviewer changed its Validation Checkout: " + repr(failures))
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps({
    "checks": {
        "e2e": {"status": "pass", "evidence": "操作或命令：真实只读探测；退出码：0；结果：三类写入均被拒绝。", "findings": []},
        "standards": {"status": "pass", "evidence": "审查范围或基线：生产 Reviewer 沙箱；结论：未发现违反项。", "findings": []},
        "spec": {"status": "pass", "evidence": "已核对的验收标准：只读 Validation Checkout；覆盖结论：创建、修改、删除均不可用。", "findings": []}
    }
}), encoding="utf-8")
print('{"type":"thread.started","thread_id":"read-only-reviewer"}')
""",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    candidate_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    result = CodexCliBackend(
        executable=str(worker), credential_provider=lambda: "reader-secret"
    ).review(
        {
            "checkout": str(git_repo),
            "ticket": {"number": 3},
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
        }
    )

    assert result.thread_id == "read-only-reviewer"
    assert not (git_repo / "reviewer-mutated.txt").exists()
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "# fixture\n"
    assert subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == candidate_tree
    assert subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""


def test_live_codex_backend_rejects_empty_minted_token(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(CodexProcessError, match="invalid credential"):
        CodexCliBackend(credential_provider=lambda: "").develop(
            {
                "checkout": str(checkout),
                "thread_id": None,
                "ticket": {"number": 3},
            }
        )


def test_live_codex_backend_reports_token_mint_failure(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def reject() -> str:
        raise GitHubCredentialError("GitHub did not honor the repository credential scope")

    with pytest.raises(CodexProcessError, match="invalid credential"):
        CodexCliBackend(credential_provider=reject).develop(
            {
                "checkout": str(checkout),
                "thread_id": None,
                "ticket": {"number": 3},
            }
        )


def test_app_jwt_is_rs256_and_cleans_temporary_key(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    before = set(Path(tempfile.gettempdir()).glob("agent-run-app-key-*"))
    monkeypatch.setattr("agent_run.github_auth.time.time", lambda: 1_800_000_000)

    token = _create_app_jwt("123", private_key.read_text(encoding="utf-8"))

    header_part, payload_part, signature_part = token.split(".")
    header = json.loads(_decode_base64url(header_part))
    payload = json.loads(_decode_base64url(payload_part))
    signature = tmp_path / "signature.bin"
    signature.write_bytes(_decode_base64url(signature_part))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
        ],
        input=f"{header_part}.{payload_part}".encode(),
        capture_output=True,
        check=False,
    )

    assert verified.returncode == 0
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload == {
        "exp": 1_800_000_540,
        "iat": 1_799_999_940,
        "iss": "123",
    }
    assert set(Path(tempfile.gettempdir()).glob("agent-run-app-key-*")) == before


def test_app_jwt_signing_failure_does_not_expose_openssl_stderr(
    monkeypatch: Any,
) -> None:
    def failed_signing(*arguments: Any, **options: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            list(arguments[0]),
            1,
            b"",
            b"Provider routines::bad decrypt private-key-material",
        )

    monkeypatch.setattr("agent_run.github_auth.subprocess.run", failed_signing)

    with pytest.raises(GitHubCredentialError, match="could not sign GitHub App JWT") as error:
        _create_app_jwt("123", "private-key")

    assert "private-key-material" not in str(error.value)


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.mark.parametrize(
    ("config_key", "credentialed_url"),
    [
        (
            "remote.origin.url",
            "https://publisher-secret@github.com/example/project.git",
        ),
        (
            "remote.origin.pushurl",
            "http://publisher:publisher-secret@example.test/project.git",
        ),
    ],
)
def test_worker_boundary_rejects_credentialed_http_remote_without_leaking_it(
    git_repo: Path,
    tmp_path: Path,
    config_key: str,
    credentialed_url: str,
) -> None:
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/example/project.git",
        ],
        cwd=git_repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--add", config_key, credentialed_url],
        cwd=git_repo,
        check=True,
    )
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
    git_config = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "config"],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    config_before = git_config.read_bytes()

    with pytest.raises(WorkerSandboxError) as raised:
        bubblewrap_command(
            ["git", "log", "-1", "--format=%s"],
            checkout=git_repo,
            temporary=temporary,
            writable_checkout=True,
            environment=environment,
        )

    message = str(raised.value)
    assert "credential-bearing HTTP(S) Git remote" in message
    assert "publisher-secret" not in message
    assert credentialed_url not in message
    assert git_config.read_bytes() == config_before


def test_worker_boundary_allows_credential_free_remote_forms_and_git_reads(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote_urls = [
        "https://github.com/example/project.git",
        "ssh://git@github.com/example/project.git",
        "git@github.com:example/project.git",
        "../local-project.git",
    ]
    subprocess.run(
        ["git", "remote", "add", "origin", remote_urls[0]],
        cwd=git_repo,
        check=True,
    )
    for remote_url in remote_urls[1:]:
        subprocess.run(
            ["git", "config", "--add", "remote.origin.url", remote_url],
            cwd=git_repo,
            check=True,
        )
    subprocess.run(
        [
            "git",
            "config",
            "--add",
            "remote.origin.pushurl",
            "ssh://git@github.com/example/project.git",
        ],
        cwd=git_repo,
        check=True,
    )
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
    command = bubblewrap_command(
        [
            "sh",
            "-c",
            "git remote get-url --all origin && git log -1 --format=%s",
        ],
        checkout=git_repo,
        temporary=temporary,
        writable_checkout=True,
        environment=environment,
    )

    attempted = run_worker_process(
        command,
        cwd=git_repo,
        prompt="",
        environment=environment,
        timeout=5,
    )

    assert attempted.returncode == 0, attempted.stderr
    assert attempted.stdout.splitlines() == [*remote_urls, "initial"]


def test_worker_boundary_checks_linked_worktree_remote_config(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=git_repo,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=git_repo,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "ticket", str(checkout), "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    credentialed_url = (
        "https://linked-worktree-secret@github.com/example/repo.git"
    )
    subprocess.run(
        [
            "git",
            "config",
            "--worktree",
            "remote.origin.url",
            credentialed_url,
        ],
        cwd=checkout,
        check=True,
    )
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")

    with pytest.raises(WorkerSandboxError) as raised:
        bubblewrap_command(
            ["git", "remote", "get-url", "origin"],
            checkout=checkout,
            temporary=temporary,
            writable_checkout=True,
            environment=environment,
        )

    assert "linked-worktree-secret" not in str(raised.value)


def test_worker_boundary_checks_effective_instead_of_remote_url(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", "example:project.git"],
        cwd=git_repo,
        check=True,
    )
    credentialed_base = "https://rewrite-secret@github.com/"
    subprocess.run(
        [
            "git",
            "config",
            f"url.{credentialed_base}.insteadOf",
            "example:",
        ],
        cwd=git_repo,
        check=True,
    )
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")

    with pytest.raises(WorkerSandboxError) as raised:
        bubblewrap_command(
            ["git", "remote", "get-url", "origin"],
            checkout=git_repo,
            temporary=temporary,
            writable_checkout=True,
            environment=environment,
        )

    assert "rewrite-secret" not in str(raised.value)


def test_worker_can_use_real_git_and_edit_host_but_cannot_commit_linked_worktree(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    host_output = tmp_path / "host-output.txt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "ticket", str(checkout), "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git_paths = subprocess.run(
        [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    git_directory, common_directory = map(Path, git_paths)
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
    command = bubblewrap_command(
        [
            "sh",
            "-c",
            (
                "! printf 'unauthorized\\n' >> \"$2\" && "
                "! printf 'unauthorized\\n' >> \"$3/HEAD\" && "
                "! printf 'unauthorized\\n' >> \"$4/config\" && "
                "printf 'worker edit\\n' >> README.md && "
                "printf 'host edit\\n' > \"$1\" && "
                "git status --short && "
                "git diff -- README.md && "
                "git log -1 --format=%s && "
                "git add README.md && "
                "git commit -m 'worker unauthorized commit'"
            ),
            "worker-script",
            str(host_output),
            str(checkout / ".git"),
            str(git_directory),
            str(common_directory),
        ],
        checkout=checkout,
        temporary=temporary,
        writable_checkout=True,
        environment=environment,
    )

    attempted = subprocess.run(
        command,
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert attempted.returncode != 0
    assert (
        checkout / "README.md"
    ).read_text(encoding="utf-8") == "# fixture\nworker edit\n"
    assert host_output.read_text(encoding="utf-8") == "host edit\n"
    assert " M README.md" in attempted.stdout
    assert "+worker edit" in attempted.stdout
    assert "initial" in attempted.stdout
    assert "worker Git CLI is disabled" not in attempted.stderr
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert after == before


def test_worker_can_inspect_but_cannot_commit_checkout_with_git_directory(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
    command = bubblewrap_command(
        [
            "sh",
            "-c",
            (
                "printf 'worker edit\\n' >> README.md && "
                "git status --short && "
                "git diff -- README.md && "
                "git log -1 --format=%s && "
                "git -c user.name=worker "
                "-c user.email=worker@example.invalid "
                "commit -am 'worker unauthorized commit'"
            ),
        ],
        checkout=git_repo,
        temporary=temporary,
        writable_checkout=True,
        environment=environment,
    )

    attempted = run_worker_process(
        command,
        cwd=git_repo,
        prompt="",
        environment=environment,
        timeout=5,
    )

    assert attempted.returncode != 0
    assert " M README.md" in attempted.stdout
    assert "+worker edit" in attempted.stdout
    assert "initial" in attempted.stdout
    assert "worker Git CLI is disabled" not in attempted.stderr
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert after == before


def _install_local_gh_test_boundary(root: Path) -> tuple[Path, Path]:
    """Provide a test-owned gh executable for direct bubblewrap tests."""

    adapter = root / "local-gh-adapter"
    target_directory = root / "local-gh-bin"
    target_directory.mkdir()
    target = target_directory / "gh"
    script = """#!/usr/bin/env python3
import json
import os
import ssl
import sys
import urllib.request

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("gh version 2.0.0")
    raise SystemExit(0)
if arguments == ["auth", "token"]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GH_ENTERPRISE_TOKEN")
    if not token:
        raise SystemExit(1)
    print(token)
    raise SystemExit(0)
if len(arguments) != 4 or arguments[0] != "api" or arguments[2:] != ["--jq", ".number"]:
    raise SystemExit("unsupported test gh invocation")
host = os.environ.get("GH_HOST", "")
if not host.startswith("localhost:"):
    raise SystemExit("test gh must use the local HTTPS server")
token = os.environ.get("GH_TOKEN") or os.environ.get("GH_ENTERPRISE_TOKEN")
if not token:
    raise SystemExit("test gh received no token")
request = urllib.request.Request(
    f"https://{host}/api/v3/{arguments[1]}",
    headers={"Authorization": f"token {token}"},
)
context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=context),
)
with opener.open(request, timeout=5) as response:
    print(json.load(response)["number"])
"""
    for path in (adapter, target):
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)
    return adapter, target


def test_worker_has_real_git_helpers_and_native_gh_auth(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
    gh_adapter, gh_target = _install_local_gh_test_boundary(tmp_path)
    environment["PATH"] = (
        f"{gh_target.parent}{os.pathsep}{environment['PATH']}"
    )
    command = bubblewrap_command(
        [
            "sh",
            "-c",
            (
                "test -x \"$(git --exec-path)/git-commit\" && "
                "gh --version >/dev/null && "
                "test \"$(gh auth token)\" = reader-secret && "
                "/usr/bin/git status --short && "
                "/usr/bin/git log -1 --format=%s"
            ),
        ],
        checkout=git_repo,
        temporary=temporary,
        writable_checkout=True,
        environment=environment,
    )

    attempted = run_worker_process(
        command,
        cwd=git_repo,
        prompt="",
        environment=environment,
        timeout=5,
    )

    assert attempted.returncode == 0
    assert attempted.stdout.strip() == "initial"
    assert "AGENT_RUN_REAL_GIT" not in environment


def test_worker_uses_gh_for_authenticated_remote_read(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    certificate = tmp_path / "localhost.crt"
    private_key = tmp_path / "localhost.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    received: dict[str, str] = {}

    class IssueHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization", "")
            if received["authorization"] != "token reader-secret":
                self.send_response(403)
                self.end_headers()
                return
            body = json.dumps({"number": 3}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IssueHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        temporary = tmp_path / "worker"
        temporary.mkdir()
        environment = worker_environment(temporary / "gh", "reader-secret")
        gh_adapter, gh_target = _install_local_gh_test_boundary(tmp_path)
        environment["PATH"] = (
            f"{gh_target.parent}{os.pathsep}{environment['PATH']}"
        )
        environment.pop("GH_TOKEN")
        environment.update(
            {
                "GH_ENTERPRISE_TOKEN": "reader-secret",
                "GH_HOST": f"localhost:{server.server_port}",
                "SSL_CERT_FILE": str(certificate),
            }
        )
        command = bubblewrap_command(
            [
                "gh",
                "api",
                "repos/example/project/issues/3",
                "--jq",
                ".number",
            ],
            checkout=git_repo,
            temporary=temporary,
            writable_checkout=True,
            environment=environment,
        )

        attempted = run_worker_process(
            command,
            cwd=git_repo,
            prompt="",
            environment=environment,
            timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert attempted.returncode == 0, attempted.stderr
    assert attempted.stdout.strip() == "3"
    assert received == {
        "path": "/api/v3/repos/example/project/issues/3",
        "authorization": "token reader-secret",
    }


@pytest.mark.parametrize("source", ["host", "pat"])
def test_backend_native_gh_receives_identity_and_unrestricted_arguments(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    calls = tmp_path / "gh-calls"
    host_config = tmp_path / "host-gh-config"
    host_config.mkdir()
    (host_config / "hosts.yml").write_text("host login")
    host_gh = tmp_path / "gh"
    host_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps({'args': sys.argv[1:], 'token': os.environ.get('GH_TOKEN'), "
        "'config': os.environ.get('GH_CONFIG_DIR'), 'alternate': os.environ.get('GITHUB_TOKEN')}) + '\\n')\n"
    )
    host_gh.chmod(0o700)
    worker = tmp_path / "codex-worker"
    native_arguments = ["api", "repos/other/project/issues", "--method", "POST", "-f", "title=native"]
    worker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        f"subprocess.run(['gh', *{native_arguments!r}], check=True)\n"
        "assert not any(k.startswith('AGENT_RUN_INTERNAL_') for k in os.environ)\n"
        f"assert ('SSH_AUTH_SOCK' in os.environ) == {source == 'host'!r}\n"
        "assert subprocess.run(['git', 'config', '--local', 'test.worker-write', 'blocked'], capture_output=True).returncode != 0\n"
        "output = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "with open(output, 'w') as result:\n"
        "    json.dump({'result_kind': 'development', 'summary': 'ok', 'human_blockers': None}, result)\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'native-thread'}))\n"
    )
    worker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_CONFIG_DIR", str(host_config))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "host-agent.sock"))
    monkeypatch.setenv("GH_TOKEN", "host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "alternate-host-token")
    monkeypatch.setenv("WORKER_TEST_PAT", "selected-pat")
    monkeypatch.setenv("AGENT_RUN_INTERNAL_TEST", "control-secret")
    identity = {"source": source}
    if source == "pat":
        identity["pat"] = {"env": "WORKER_TEST_PAT"}
    output, thread_id = CodexCliBackend(
        executable=str(worker), worker_identity=identity
    )._invoke(prompt="native identity", checkout=git_repo, thread_id=None, repository="example/project")
    assert json.loads(output)["result_kind"] == "development"
    assert thread_id == "native-thread"
    call = next(
        row for row in map(json.loads, calls.read_text().splitlines())
        if row["args"] == native_arguments
    )
    assert call["args"] == native_arguments
    assert call["token"] == ("host-token" if source == "host" else "selected-pat")
    if source == "host":
        assert call["config"] == str(host_config)
    else:
        assert call["alternate"] is None
        assert call["config"] != str(host_config)
        assert not Path(call["config"]).exists(), "invocation temporary credentials must be cleaned"


def test_native_pat_unavailable_is_execution_failure_and_reloads_same_reference(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "worker-token"
    backend = CodexCliBackend(worker_identity={"source": "pat", "pat": {"file": str(reference)}})
    monkeypatch.setattr(codex_module, "validate_worker_auth", lambda identity, **kwargs: None)
    received: list[str] = []
    def run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        received.append(options["environment"]["GH_TOKEN"])
        assert "gh-adapter" not in options["environment"]["PATH"]
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text("ok")
        return subprocess.CompletedProcess(arguments, 0, '{"type":"thread.started","thread_id":"fixed-thread"}', "")
    monkeypatch.setattr(codex_module, "run_worker_process", run)
    with pytest.raises(CodexProcessError) as failure:
        backend._invoke(prompt="test", checkout=git_repo, thread_id=None)
    assert failure.value.machine_error == "credential_unavailable"
    assert failure.value.recoverable
    assert not received
    reference.write_text("first-token")
    backend._invoke(prompt="test", checkout=git_repo, thread_id=None)
    reference.write_text("replacement-token")
    backend._invoke(prompt="test", checkout=git_repo, thread_id="fixed-thread")
    assert received == ["first-token", "replacement-token"]


def test_native_auth_failure_after_worker_result_is_execution_failure(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "worker-token"
    reference.write_text("invocation-token")
    identity = {"source": "pat", "pat": {"file": str(reference)}}
    backend = CodexCliBackend(worker_identity=identity)
    validated: list[str] = []
    def validate(selected: dict[str, Any], environment: dict[str, str] | None = None) -> None:
        if environment is None:
            return
        validated.append(codex_module.native_github_environment(selected, environment)["GH_TOKEN"])
        raise codex_module.GitHubIdentityError("PAT authentication failed; repair the same reference and resume")
    def run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        # Changing the reference must not hide failure of the credential the Worker used.
        reference.write_text("replacement-token")
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text('{"decision":"changes_requested","findings":["authentication failed"]}')
        return subprocess.CompletedProcess(arguments, 0, '{"type":"thread.started","thread_id":"review-thread"}', "")
    monkeypatch.setattr(codex_module, "validate_worker_auth", validate)
    monkeypatch.setattr(codex_module, "run_worker_process", run)
    with pytest.raises(CodexProcessError) as failure:
        backend._invoke(prompt="review", checkout=git_repo, thread_id=None)
    assert failure.value.machine_error == "credential_unavailable"
    assert failure.value.recoverable
    assert validated == ["invocation-token"]


def test_signing_registry_reclaims_resources_registered_after_cancel() -> None:
    class Transport:
        shutdown_called = False
        close_called = False

        def shutdown(self, _how: int) -> None:
            self.shutdown_called = True

        def close(self) -> None:
            self.close_called = True

    class Response:
        def __init__(self, transport: Transport) -> None:
            self.fp = type(
                "FileObject",
                (),
                {"raw": type("RawSocket", (), {"_sock": transport})()},
            )()
            self.closed = False

        def close(self) -> None:
            assert self.fp.raw._sock.shutdown_called
            self.closed = True

    registry = _SigningProcessRegistry()
    registry.cancel()
    transport = Transport()
    response = Response(transport)

    registry.register_response(response)

    assert transport.shutdown_called
    assert transport.close_called
    assert response.closed


def test_signing_registry_cancels_before_https_connection_can_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    connection_seen = threading.Event()
    stop_accept = threading.Event()

    def accept_connection() -> None:
        listener.settimeout(0.05)
        while not stop_accept.is_set():
            try:
                accepted, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection_seen.set()
            accepted.close()
            return

    accept_thread = threading.Thread(target=accept_connection)
    accept_thread.start()
    registry = _SigningProcessRegistry()
    handler = _CancellableHTTPSHandler(registry)
    connection_ready = threading.Event()
    release_connection = threading.Event()
    original_open_connection = handler._open_connection

    def paused_open_connection(host: str, **options: Any) -> Any:
        connection = original_open_connection(host, **options)
        connection_ready.set()
        assert release_connection.wait(timeout=2)
        return connection

    monkeypatch.setattr(handler, "_open_connection", paused_open_connection)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
    request = urllib.request.Request(
        f"https://127.0.0.1:{listener.getsockname()[1]}/token",
        data=b"{}",
        method="POST",
    )
    errors: list[BaseException] = []

    def open_request() -> None:
        try:
            opener.open(request, timeout=15)
        except BaseException as error:
            errors.append(error)

    request_thread = threading.Thread(target=open_request)
    request_thread.start()
    try:
        assert connection_ready.wait(timeout=2)
        cancel_started = time.monotonic()
        registry.cancel()
        release_connection.set()
        request_thread.join(timeout=2)
        assert time.monotonic() - cancel_started < 3
        assert not request_thread.is_alive()
        assert errors
        assert not connection_seen.is_set()
    finally:
        release_connection.set()
        registry.cancel()
        request_thread.join(timeout=2)
        stop_accept.set()
        listener.close()
        accept_thread.join(timeout=2)

    assert not accept_thread.is_alive()


def test_sigint_terminates_worker_process_group(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    child_path = tmp_path / "child.pid"
    reaped_path = tmp_path / "child-reaped"
    group_path = tmp_path / "worker.pgid"
    # Group cleanup may kill the shell before it can reap its child. Own orphan
    # reaping in this isolated controller; kill(pid, 0) alone also sees zombies.
    code = f"""
from pathlib import Path
from agent_run.worker_sandbox import run_worker_process
from support.worker_sigint_probe import expect_sigint_cleanup
with expect_sigint_cleanup(Path("child.pid"), Path("child-reaped")):
    run_worker_process(
        ["sh", "-c", "sleep 60 & echo $! > child.pid; wait"],
        cwd=Path({str(tmp_path)!r}),
        prompt="",
        environment={{"PATH": "/usr/bin:/bin"}},
        timeout=120,
        on_process_started=lambda pid: Path("worker.pgid").write_text(str(pid)),
    )
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root / "src"), str(project_root / "tests")]
    )
    controller = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child_path.exists() and child_path.read_text().strip():
                break
            time.sleep(0.02)
        assert child_path.exists()
        child_pid = int(child_path.read_text(encoding="utf-8").strip())

        os.kill(controller.pid, signal.SIGINT)
        controller.wait(timeout=5)

        stdout, stderr = controller.communicate(timeout=5)
        assert controller.returncode == 130, (stdout, stderr)
        assert reaped_path.read_text() == "SIGINT cleanup passed"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("background Worker process survived SIGINT cleanup")
    finally:
        cleanup_probe_controller(controller, group_path)


@pytest.mark.parametrize(
    "sigint_phase",
    [
        "before_construction",
        "partial_construction",
        "before_start",
        "partial_start",
        "normal_wait",
    ],
)
def test_sigint_cleans_worker_during_thread_startup_and_wait(
    tmp_path: Path, sigint_phase: str
) -> None:
    project_root = Path(__file__).parents[1]
    child_path = tmp_path / "child.pid"
    marker_path = tmp_path / "thread-start.marker"
    reaped_path = tmp_path / "child-reaped"
    group_path = tmp_path / "worker.pgid"
    target_start = {"before_start": 1, "partial_start": 2}.get(sigint_phase)
    code = f"""
import os
import signal
import threading
from pathlib import Path
from agent_run.worker_sandbox import run_worker_process
from support.worker_sigint_probe import expect_sigint_cleanup

phase = {sigint_phase!r}
marker = Path({str(marker_path)!r})
original_init = threading.Thread.__init__
original_start = threading.Thread.start
construction_count = 0
start_count = 0

def controlled_init(thread, *args, **kwargs):
    global construction_count
    construction_count += 1
    if phase in {{"before_construction", "partial_construction"}}:
        target = {{"before_construction": 1, "partial_construction": 2}}[phase]
        if construction_count == target:
            marker.write_text(str(construction_count), encoding="utf-8")
            signal.pause()
    return original_init(thread, *args, **kwargs)

def controlled_start(thread, *args, **kwargs):
    global start_count
    start_count += 1
    if phase != "normal_wait" and start_count == {target_start!r}:
        marker.write_text(str(start_count), encoding="utf-8")
        signal.pause()
    return original_start(thread, *args, **kwargs)

threading.Thread.__init__ = controlled_init
threading.Thread.start = controlled_start

def interrupt_when_ready(line):
    if line == "ready":
        os.kill(os.getpid(), signal.SIGINT)

with expect_sigint_cleanup(Path("child.pid"), Path("child-reaped")):
    run_worker_process(
        ["sh", "-c", "sleep 60 & echo $! > child.pid; echo ready; wait"],
        cwd=Path({str(tmp_path)!r}),
        prompt="",
        environment={{"PATH": "/usr/bin:/bin"}},
        timeout=120,
        on_process_started=lambda pid: Path("worker.pgid").write_text(str(pid)),
        on_stdout_line=interrupt_when_ready if phase == "normal_wait" else None,
    )
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root / "src"), str(project_root / "tests")]
    )
    controller = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        # Redirection creates the file before echo writes the child PID.
        # Interrupting that gap prevents both the assertion and orphan reaping.
        while time.monotonic() < deadline:
            if child_path.exists() and child_path.read_text().strip():
                break
            time.sleep(0.02)
        assert child_path.exists() and child_path.read_text().strip()
        if sigint_phase != "normal_wait":
            while not marker_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker_path.exists()
            os.kill(controller.pid, signal.SIGINT)
        controller.wait(timeout=5)

        stdout, stderr = controller.communicate(timeout=5)
        assert controller.returncode == 130, (stdout, stderr)
        assert reaped_path.read_text() == "SIGINT cleanup passed"
        child_pid = int(child_path.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"background Worker process survived {sigint_phase} cleanup")
    finally:
        cleanup_probe_controller(controller, group_path)


def test_successful_worker_cleans_background_processes(
    tmp_path: Path,
) -> None:
    # Own orphan reaping in an isolated probe; kill(pid, 0) also succeeds for
    # dead zombies and otherwise depends on the CI host's init process.
    code = r'''
import ctypes, os, signal, time
from pathlib import Path
from agent_run.worker_sandbox import run_worker_process
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
child = None
reaped = False
try:
    result = run_worker_process(
        ["sh", "-c", "sleep 60 </dev/null >/dev/null 2>&1 & echo $! > child.pid"],
        cwd=Path.cwd(), prompt="", environment={"PATH": "/usr/bin:/bin"}, timeout=5,
    )
    assert result.returncode == 0
    child = int(Path("child.pid").read_text().strip())
    deadline = time.monotonic() + 1
    while os.waitpid(child, os.WNOHANG)[0] != child:
        assert time.monotonic() < deadline, "background Worker survived cleanup"
        time.sleep(0.01)
    reaped = True
    child = None
finally:
    if not reaped and child is None and Path("child.pid").exists():
        child = int(Path("child.pid").read_text().strip())
    if child is not None:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stdout_callback_error_does_not_stop_pipe_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls = 0
    ready = threading.Event()

    def fail_first_line(_line: str) -> None:
        nonlocal callback_calls
        callback_calls += 1
        ready.set()
        raise CodexProcessError("reported Thread mismatch")

    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "print('first'); "
            "sys.stdout.write('x' * (1024 * 1024)); "
            "sys.stdout.flush()"
        ),
    ]

    with pytest.raises(CodexProcessError, match="Thread mismatch"):
        run_worker_expecting_early_failure(
            command, cwd=tmp_path, ready=ready, monkeypatch=monkeypatch,
            on_stdout_line=fail_first_line,
        )

    assert callback_calls == 1


def test_stdout_callback_error_terminates_hanging_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = threading.Event()

    def reject_thread(_line: str) -> None:
        ready.set()
        raise CodexProcessError("reported Thread mismatch")

    with pytest.raises(CodexProcessError, match="Thread mismatch"):
        run_worker_expecting_early_failure(
            ["sh", "-c", "printf 'thread.started\\n'; sleep 60"],
            cwd=tmp_path, ready=ready, monkeypatch=monkeypatch,
            on_stdout_line=reject_thread,
        )


def test_streaming_timeout_joins_reader_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_ids: list[int] = []
    child_pid_file = tmp_path / "child.pid"
    clock = AdvancingClock()
    ready = threading.Event()

    def expire_after_child_is_ready(line: str) -> None:
        assert line == "ready"
        assert child_pid_file.read_text(encoding="utf-8").strip()
        clock.offset += 30
        ready.set()

    monkeypatch.setattr(worker_sandbox_module, "time", clock)

    deadline_at = clock.monotonic() + 10
    with pytest.raises(WorkerSandboxError, match="timed out"):
        run_worker_process(
            [
                "sh",
                "-c",
                'sleep 60 & echo "$!" > "$1"; printf "ready\\n"; wait',
                "sh",
                str(child_pid_file),
            ],
            cwd=tmp_path,
            prompt="",
            environment={"PATH": "/usr/bin:/bin"},
            timeout=10,
            deadline_at_monotonic=deadline_at,
            on_stdout_line=expire_after_child_is_ready,
            on_process_started=process_ids.append,
        )

    assert ready.is_set()
    assert len(process_ids) == 1
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    reader_threads = {
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(f"agent-run-worker-{process_ids[0]}-")
    }
    assert not reader_threads


def test_absolute_deadline_includes_popen_startup_and_prevents_worker_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    child_pid_file = tmp_path / "delayed-start-child.pid"
    process_ids: list[int] = []
    process_group_ids: list[int] = []
    timed_waits: list[float] = []
    clock = AdvancingClock()
    real_popen = worker_sandbox_module.subprocess.Popen
    real_wait = real_popen.wait

    def delayed_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process = real_popen(*args, **kwargs)
        process_group_ids.append(os.getpgid(process.pid))
        clock.offset += 30
        return process

    def record_wait(
        process: subprocess.Popen[str], timeout: float | None = None
    ) -> int:
        if timeout is not None:
            timed_waits.append(timeout)
        return real_wait(process, timeout=timeout)

    monkeypatch.setattr(worker_sandbox_module.subprocess, "Popen", delayed_popen)
    monkeypatch.setattr(real_popen, "wait", record_wait)
    monkeypatch.setattr(worker_sandbox_module, "time", clock)
    deadline_at = clock.monotonic() + 10

    with pytest.raises(WorkerSandboxError, match="timed out"):
        run_worker_process(
            [
                "sh",
                "-c",
                'sleep 60 & echo "$!" > "$1"; wait',
                "sh",
                str(child_pid_file),
            ],
            cwd=tmp_path,
            prompt="",
            environment={"PATH": "/usr/bin:/bin"},
            timeout=10,
            deadline_at_monotonic=deadline_at,
            on_process_started=process_ids.append,
        )

    assert len(process_ids) == 1
    assert process_group_ids == process_ids
    assert timed_waits == []
    assert not child_pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(process_ids[0], 0)


def test_bootstrap_rechecks_absolute_deadline_at_gate_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    side_effect = tmp_path / "worker-started"
    processes: list[subprocess.Popen[bytes]] = []
    bootstrap_return_codes: list[int] = []
    process_ids: list[int] = []
    clock = AdvancingClock()
    deadline_at = clock.monotonic() + 10
    child_clock = tmp_path / "bootstrap-clock"
    child_clock.write_text(str(deadline_at - 1), encoding="utf-8")
    ready_read, ready_write = os.pipe()
    real_popen = worker_sandbox_module.subprocess.Popen
    real_write = worker_sandbox_module.os.write

    def capture_popen(arguments: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        arguments = list(arguments)
        # Instrument the real bootstrap's clock and announce entry to its gate
        # read. Advancing time only after that signal also detects a deadline
        # check incorrectly moved before the gate or a cached pre-gate clock.
        arguments[2] = (
            "import os, time\n"
            "from pathlib import Path\n"
            f"time.monotonic = lambda: float(Path({str(child_clock)!r}).read_text())\n"
            "_real_read = os.read\n"
            "def _announce_gate_read(fd, size):\n"
            f"    if fd == {int(arguments[3])}:\n"
            f"        os.write({ready_write}, b'r')\n"
            "    return _real_read(fd, size)\n"
            "os.read = _announce_gate_read\n"
            + arguments[2]
        )
        kwargs["pass_fds"] = (*kwargs["pass_fds"], ready_write)
        process = real_popen(arguments, **kwargs)
        processes.append(process)
        return process

    def write_after_deadline(fd: int, data: bytes) -> int:
        if data == b"1":
            readable, _, _ = select.select([ready_read], [], [], 5)
            assert readable, "bootstrap did not enter its gate read"
            assert os.read(ready_read, 1) == b"r"
            child_clock.write_text(str(deadline_at + 1), encoding="utf-8")
            clock.offset += 30
            written = real_write(fd, data)
            bootstrap_return_codes.append(processes[0].wait(timeout=5))
            return written
        return real_write(fd, data)

    monkeypatch.setattr(worker_sandbox_module, "time", clock)
    monkeypatch.setattr(worker_sandbox_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(worker_sandbox_module.os, "write", write_after_deadline)

    try:
        with pytest.raises(WorkerSandboxError, match="timed out"):
            run_worker_process(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(side_effect)!r}).touch()",
                ],
                cwd=tmp_path,
                prompt="",
                environment=os.environ.copy(),
                timeout=10,
                deadline_at_monotonic=deadline_at,
                on_process_started=process_ids.append,
            )

        assert bootstrap_return_codes == [124]
        assert len(process_ids) == 1
        assert not side_effect.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(process_ids[0], 0)
    finally:
        os.close(ready_read)
        os.close(ready_write)


def test_non_streaming_worker_stops_when_credential_renewal_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    abort_event = threading.Event()
    stop_observer = threading.Event()
    process_ids: list[int] = []
    ready_fifo = tmp_path / "worker-ready"
    os.mkfifo(ready_fifo)
    ready_fd = os.open(ready_fifo, os.O_RDWR | os.O_NONBLOCK)

    def exhaust_credentials() -> None:
        while not stop_observer.is_set():
            readable, _, _ = select.select([ready_fd], [], [], 0.1)
            if readable and os.read(ready_fd, 5) == b"ready":
                abort_event.set()
                return

    observer = threading.Thread(target=exhaust_credentials)
    observer.start()
    try:
        with pytest.raises(WorkerSandboxError, match="credential renewal failed"):
            run_worker_expecting_early_failure(
                ["sh", "-c", 'printf ready > "$1"; sleep 60', "sh", str(ready_fifo)],
                cwd=tmp_path,
                ready=abort_event,
                monkeypatch=monkeypatch,
                abort_event=abort_event,
                abort_reason=lambda: "Worker credential renewal failed",
                on_process_started=process_ids.append,
            )
        assert abort_event.is_set()
        assert len(process_ids) == 1
        with pytest.raises(ProcessLookupError):
            os.killpg(process_ids[0], 0)
    finally:
        stop_observer.set()
        observer.join(timeout=5)
        os.close(ready_fd)
        assert not observer.is_alive()


def test_jsonl_stream_uses_fixed_chunks_and_keeps_thread_and_error_tail() -> None:
    stream = _BoundedJsonlStream(capture_bytes=4096, line_bytes=1024)
    stream.feed(b'{"type":"thread.started","thread_id":"thread-189"}\n')
    noise_chunk = b"noise" * 1000
    for _ in range(500):
        stream.feed(noise_chunk)
    stream.feed(
        b'\n{"type":"turn.failed","error":{"message":"bounded failure"}}\n'
    )
    stream.finish()

    captured = stream.text()
    assert "thread-189" in captured
    assert "bounded failure" in captured
    assert len(captured.encode("utf-8")) <= 4096


def test_worker_process_bounds_stdout_and_stderr_without_rss_assertions(
    tmp_path: Path,
) -> None:
    script = tmp_path / "large-output.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-189'}))\n"
        "for _ in range(512):\n"
        "    print('x' * 8192)\n"
        "    print('y' * 8192, file=sys.stderr)\n"
        "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'tail failure'}}))\n",
        encoding="utf-8",
    )

    result = run_worker_process(
        [sys.executable, str(script)],
        cwd=tmp_path,
        prompt="",
        environment=os.environ.copy(),
        timeout=10,
    )

    assert result.returncode == 0
    assert "thread-189" in result.stdout
    assert "tail failure" in result.stdout
    assert len(result.stdout.encode("utf-8")) <= 256 * 1024
    assert len(result.stderr.encode("utf-8")) <= 128 * 1024


def test_worker_descendant_inherits_hidden_control_and_runner_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is unavailable")
    checkout = tmp_path / "repo" / ".agent-run" / "worktrees" / "run" / "ticket"
    checkout.mkdir(parents=True)
    control_root = tmp_path / "repo" / ".agent-run"
    for child in ("runs", "task-control", "profiles"):
        directory = control_root / child
        directory.mkdir(parents=True)
        (directory / "authority.json").write_text("secret", encoding="utf-8")
    custom_state_root = tmp_path / "custom-state"
    for child in ("runs", "task-control", "profiles"):
        directory = custom_state_root / child
        directory.mkdir(parents=True)
        (directory / "authority.json").write_text("authority", encoding="utf-8")
    (custom_state_root / ".lock").write_text("authority", encoding="utf-8")
    (control_root / "state.sqlite3").write_text("secret", encoding="utf-8")
    custom_run = custom_state_root / "state.sqlite3"
    custom_run.write_text("authority", encoding="utf-8")
    custom_control = custom_state_root / "task-control" / "authority.json"
    custom_profile = custom_state_root / "profiles" / "authority.json"
    custom_lock = custom_state_root / ".lock"
    data_home = tmp_path / "data"
    runner_root = data_home / "agent-run"
    runner_root.mkdir(parents=True)
    (runner_root / "management-secret").write_text("secret", encoding="utf-8")
    (runner_root / "run-locator.json").write_text("secret", encoding="utf-8")
    runtime_home = tmp_path / "runtime"
    systemd_private = runtime_home / "systemd" / "private"
    systemd_private.parent.mkdir(parents=True)
    systemd_private.write_text("fake-systemd-socket", encoding="utf-8")
    user_bus = runtime_home / "bus"
    user_bus.write_text("fake-user-bus", encoding="utf-8")
    carrier_root = runtime_home / "agent-run" / "executor"
    carrier_root.mkdir(parents=True)
    carrier = carrier_root / "environment-other-generation.json"
    carrier.write_text("secret-terminal-environment", encoding="utf-8")
    temporary = tmp_path / "worker-temp"
    temporary.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={user_bus}")
    monkeypatch.setenv("AGENT_RUN_EXECUTOR_ACTION_ID", "secret-action")
    monkeypatch.setenv("AGENT_RUN_INTERNAL_STATE_ROOT", str(custom_state_root))
    environment = worker_environment(tmp_path / "gh", "read-token")
    hidden = codex_module._worker_hidden_paths(None, checkout=checkout)
    # WAL/SHM can appear after the launch boundary collected existing paths.
    for suffix in ("-wal", "-shm"):
        Path(str(custom_run) + suffix).write_text("authority", encoding="utf-8")
    probe = checkout / "probe.py"
    probe.write_text(
        "import json, os, subprocess, sys\n"
        "code = 'import json, os, subprocess; from pathlib import Path; "
        "custom = Path(os.environ[\\\"CUSTOM_STATE\\\"]); "
        "custom_before = custom.read_text() == \\\"authority\\\" if custom.exists() else False; "
        "custom.write_text(\\\"mutated\\\"); "
        "control = Path(os.environ[\\\"CUSTOM_CONTROL\\\"]); "
        "control_before = control.read_text() == \\\"authority\\\" if control.exists() else False; "
        "control.write_text(\\\"mutated\\\"); "
        "profile = Path(os.environ[\\\"CUSTOM_PROFILE\\\"]); "
        "profile_before = profile.read_text() == \\\"authority\\\" if profile.exists() else False; "
        "profile.write_text(\\\"mutated\\\"); "
        "lock = Path(os.environ[\\\"CUSTOM_LOCK\\\"]); "
        "lock_before = subprocess.run([\\\"/bin/cat\\\", str(lock)], "
        "capture_output=True, text=True).stdout == \\\"authority\\\"; "
        "lock_write = subprocess.run([\\\"/usr/bin/tee\\\", str(lock)], "
        "input=\\\"mutated\\\", text=True, capture_output=True).returncode == 0; "
        "print(json.dumps({\\\"executor_env\\\": any(k.startswith(\\\"AGENT_RUN_EXECUTOR_\\\") for k in os.environ), "
        "\\\"control\\\": Path(os.environ[\\\"CONTROL_SECRET\\\"]).exists(), "
        "\\\"custom_before\\\": custom_before, "
        "\\\"custom_control_before\\\": control_before, "
        "\\\"custom_profile_before\\\": profile_before, "
        "\\\"custom_lock_before\\\": lock_before, "
        "\\\"custom_lock_write\\\": lock_write, "
        "\\\"runner\\\": Path(os.environ[\\\"RUNNER_SECRET\\\"]).exists(), "
        "\\\"locator\\\": Path(os.environ[\\\"LOCATOR_SECRET\\\"]).exists(), "
        "\\\"carrier\\\": Path(os.environ[\\\"CARRIER_SECRET\\\"]).exists(), "
        "\\\"user_bus\\\": subprocess.run([\\\"/bin/cat\\\", os.environ[\\\"USER_BUS\\\"]], capture_output=True, text=True).stdout == \\\"fake-user-bus\\\", "
        "\\\"systemd_private\\\": subprocess.run([\\\"/bin/cat\\\", os.environ[\\\"SYSTEMD_PRIVATE\\\"]], capture_output=True, text=True).stdout == \\\"fake-systemd-socket\\\", "
        "\\\"dbus_env\\\": \\\"DBUS_SESSION_BUS_ADDRESS\\\" in os.environ}))'\n"
        "subprocess.run([sys.executable, '-c', code], check=True)\n",
        encoding="utf-8",
    )
    environment.update(
        {
            "CONTROL_SECRET": str(control_root / "state.sqlite3"),
            "CUSTOM_STATE": str(custom_run),
            "CUSTOM_CONTROL": str(custom_control),
            "CUSTOM_PROFILE": str(custom_profile),
            "CUSTOM_LOCK": str(custom_lock),
            "RUNNER_SECRET": str(runner_root / "management-secret"),
            "LOCATOR_SECRET": str(runner_root / "run-locator.json"),
            "CARRIER_SECRET": str(carrier),
            "USER_BUS": str(user_bus),
            "SYSTEMD_PRIVATE": str(systemd_private),
        }
    )
    sidecar_probe = (
        "import os; from pathlib import Path\n"
        "for suffix in ('-wal', '-shm'):\n"
        "    path = Path(os.environ['CUSTOM_STATE'] + suffix)\n"
        "    assert not path.exists(), 'SQLite authority sidecar exposed'\n"
        "    path.write_text('mutated')\n"
    )
    with probe.open("a", encoding="utf-8") as stream:
        stream.write(f"subprocess.run([sys.executable, '-c', {sidecar_probe!r}], check=True)\n")
    command = bubblewrap_command(
        [sys.executable, str(probe)],
        checkout=checkout,
        temporary=temporary,
        writable_checkout=True,
        environment=environment,
        hidden_paths=hidden,
    )

    result = subprocess.run(
        command,
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert custom_run.read_text(encoding="utf-8") == "authority"
    for suffix in ("-wal", "-shm"):
        assert Path(str(custom_run) + suffix).read_text(encoding="utf-8") == "authority"
    assert custom_control.read_text(encoding="utf-8") == "authority"
    assert custom_profile.read_text(encoding="utf-8") == "authority"
    assert custom_lock.read_text(encoding="utf-8") == "authority"
    assert json.loads(result.stdout) == {
        "carrier": False,
        "control": False,
        "custom_before": False,
        "custom_control_before": False,
        "custom_profile_before": False,
        "custom_lock_before": False,
        "custom_lock_write": False,
        "dbus_env": False,
        "executor_env": False,
        "locator": False,
        "runner": False,
        "systemd_private": False,
        "user_bus": False,
    }


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("output_repair", [False, True], ids=["valid-first", "format-repair"])
@pytest.mark.parametrize("method", ["develop", "review", "publication", "run_publication"])
def test_language_does_not_rewrite_or_retry_valid_agent_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    language: str, output_repair: bool, method: str,
) -> None:
    from agent_run.prompt_resources import resolve_resources

    # Both opposite-language and mixed-language prose are valid wire content.
    prose = "Verified without changes." if language == "zh" else "已验证，无需改动。"
    mixed = "原始 evidence: pytest exited 0，保持原文。"
    if method == "develop":
        artifact: dict[str, Any] = {
            "result_kind": "development", "summary": prose + mixed, "human_blockers": None,
        }
        role = "development"
    elif method == "review":
        artifact = failed_acceptance_artifact(mixed)
        artifact["checks"]["e2e"]["findings"] = [prose + mixed]
        artifact["checks"]["standards"]["evidence"] = prose
        role = "review"
    else:
        artifact = {
            "result_kind": "publication", "commit_message": prose,
            "pr_title": mixed, "pr_body_markdown": prose + "\n\n" + mixed,
            "human_blockers": None,
        }
        role = "publication"
    calls: list[tuple[list[str], str]] = []

    def fake_run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, str(options["prompt"])))
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(json.dumps(
            {"invalid": True} if output_repair and len(calls) == 1 else artifact,
            ensure_ascii=False,
        ), encoding="utf-8")
        return subprocess.CompletedProcess(
            arguments, 0, '{"type":"thread.started","thread_id":"language-test"}\n', "",
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    resources = resolve_resources(language=language)
    backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
    result = getattr(backend, method)({
        "checkout": str(tmp_path), "acceptance_artifact": {}, "_prompt_resources": resources,
    })
    assert len(calls) == (2 if output_repair else 1)
    if method == "develop":
        assert result.summary == artifact["summary"]
    elif method == "run_publication":
        assert result == {**artifact, "_thread_id": "language-test"}
    else:
        assert result.artifact == artifact
    if output_repair:
        assert "resume" in calls[1][0]
        for static_part in resources[f"{role}/output-repair"].strip().split("{0}"):
            assert static_part in calls[1][1]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_no_run_publication_probe_uses_personal_language(
    tmp_path: Path, monkeypatch: Any, language: str,
) -> None:
    from agent_run.prompt_resources import read_builtin_resource
    from agent_run.user_defaults import UserDefaultsStore

    UserDefaultsStore().configure(language=language)
    prompts: list[str] = []
    wire = {"result_kind": "human_blocker", "commit_message": None,
            "pr_title": None, "pr_body_markdown": None,
            "human_blockers": ["原始 mixed language probe result"]}

    def worker(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        prompts.append(options["prompt"])
        Path(arguments[arguments.index("--output-last-message") + 1]).write_text(json.dumps(wire))
        return subprocess.CompletedProcess(arguments, 0,
            '{"type":"thread.started","thread_id":"probe-thread"}\n', "")

    monkeypatch.setattr("agent_run.codex.run_worker_process", worker)
    output, thread = CodexCliBackend(credential_provider=lambda: "isolated-reader").publication_schema_handshake(tmp_path)
    assert prompts == [read_builtin_resource("publication/handshake", language=language).rstrip("\n")]
    assert json.loads(output) == wire
    assert thread == "probe-thread"
