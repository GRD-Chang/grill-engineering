from __future__ import annotations

import base64
import http.server
import io
import json
import os
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_run.codex import (
    CodexCliBackend,
    CodexProcessError,
    _terminal_error,
)
from agent_run.agents import PublicationResult
from agent_run.github_auth import (
    GitHubCredentialError,
    _create_app_jwt,
    mint_read_only_installation_token,
)
from agent_run.worker_sandbox import (
    WorkerSandboxError,
    bubblewrap_command,
    run_worker_process,
    worker_environment,
)


def test_publication_repairs_invalid_output_in_same_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
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
            "_invocation_event": lambda kind, **facts: events.append((kind, facts)),
        }
    )

    assert isinstance(result, PublicationResult)
    assert len(attempts) == 2
    assert "resume" in attempts[1]
    assert events[-1] == (
        "completed",
        {"reported_thread_id": "publication-thread", "attempt_count": 2},
    )


def test_run_publication_repairs_invalid_semantic_output_in_same_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        artifact = {
            "result_kind": "publication",
            "commit_message": (
                "invalid title"
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
            "acceptance_artifact": {"verdict": "pass"},
            "_invocation_event": lambda kind, **facts: events.append((kind, facts)),
        }
    )

    assert result["_thread_id"] == "run-publication-thread"
    assert len(attempts) == 2
    assert "resume" in attempts[1]
    assert events[-1] == (
        "completed",
        {"reported_thread_id": "run-publication-thread", "attempt_count": 2},
    )


def test_run_publication_marks_exhausted_semantic_output_as_failed(
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
                    "commit_message": "invalid title",
                    "pr_title": "invalid title",
                    "pr_body_markdown": "invalid body",
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
                "acceptance_artifact": {"verdict": "pass"},
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
    attempts: list[list[str]] = []

    def fake_run(arguments: list[str], **_options: Any) -> subprocess.CompletedProcess[str]:
        attempts.append(arguments)
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
        {"checkout": str(tmp_path)}
    )

    assert result.thread_id == "development-thread"
    assert len(attempts) == 2
    assert "resume" in attempts[1]


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

    assert "因前次调用失败而继续的同 Thread Resume" in prompts[0]
    assert "重新核验权威输入和实际工作" in prompts[0]


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

    assert "因前次调用失败而继续的同 Thread Resume" in prompts[0]
    assert "重新核验权威输入和实际工作" in prompts[0]


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
    assert captured["GH_TOKEN"] == "reader-secret"
    assert "GITHUB_TOKEN" not in captured
    assert "GH_ENTERPRISE_TOKEN" not in captured
    assert "GITHUB_ENTERPRISE_TOKEN" not in captured
    assert "SSH_AUTH_SOCK" not in captured
    assert "AGENT_RUN_GITHUB_APP_PRIVATE_KEY" not in captured
    assert "AGENT_RUN_GITHUB_READ_TOKEN" not in captured
    assert "AGENT_RUN_GITHUB_READ_PERMISSIONS" not in captured
    assert captured["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert "--dangerously-bypass-approvals-and-sandbox" in captured_arguments
    assert "--sandbox" not in captured_arguments
    assert gh_config is not None and not gh_config.exists()
    assert os.environ["GH_TOKEN"] == "publisher-secret"


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
            if "verdict" in schema["properties"]:
                output = json.dumps(
                    {
                        "verdict": "pass",
                        "checks": {
                            lane: {
                                "status": "pass",
                                "evidence": f"{lane} independently passed.",
                            }
                            for lane in ("e2e", "standards", "spec")
                        },
                        "findings": [],
                        "human_blockers": [],
                    }
                )
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
    assert "不得自行宣布" in development
    assert "三个不同 subagent" in acceptance
    assert "E2E" in acceptance
    assert acceptance.count("skill:code-review") >= 2
    assert "不得替代" in acceptance
    assert "findings 与 human_blockers 必须都是空数组" in acceptance
    assert len(schemas) == 2
    assert all("allOf" not in schema for schema in schemas)
    assert schemas[0]["required"] == ["result_kind", "summary", "human_blockers"]


def test_publication_prompts_require_semantic_titles() -> None:
    ticket_prompt = CodexCliBackend._publication_prompt(
        {"acceptance_scope": "ticket", "acceptance_artifact": {}}
    )
    run_prompt = CodexCliBackend._publication_prompt(
        {"acceptance_scope": "run", "acceptance_artifact": {}}
    )

    assert "Conventional Commit 语义标题格式" in ticket_prompt
    assert "Conventional Commit 语义标题格式" in run_prompt
    assert "`Primary Ticket: #" not in ticket_prompt
    assert "Delivery Run、SHA、CI 与生命周期事实由 Publisher 注入" in run_prompt


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
            "acceptance_artifact": {"verdict": "pass"},
        }
    )

    assert "parent_issue_url" in prompts[0]
    assert "https://github.com/example/project/issues/1" in prompts[0]
    assert "base-sha" not in prompts[0]
    assert "run-head-sha" not in prompts[0]
    assert "一次性的" not in prompts[0]
    assert "Codex" not in prompts[0]
    assert "Worker" not in prompts[0]


@pytest.mark.parametrize(
    ("request_extra", "mode_text", "evidence"),
    [
        ({}, "Development Brief", None),
        (
            {
                "repair_source": "acceptance",
                "acceptance_artifact": {
                    "verdict": "request_changes",
                    "findings": [
                        {
                            "id": "acceptance-finding-verbatim",
                            "problem": "Preserve this exact acceptance evidence.",
                        }
                    ],
                },
            },
            "Acceptance Repair",
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
            "Required-Checks Repair",
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
    assert "受影响的成功路径、失败路径和边界情况" in prompt
    assert "Standards Review Subagent" in prompt
    assert "Spec Review Subagent" in prompt
    assert "两个不同 subagent" in prompt
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


def test_top_level_prompts_allow_only_issue_urls_and_original_evidence() -> None:
    artifact = {
        "verdict": "request_changes",
        "checks": {"e2e": {"status": "fail", "evidence": "original"}},
    }
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
    request = {
        **internal,
        "acceptance_scope": "ticket",
        "repair_source": "acceptance",
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "task_issue_url": "https://github.com/example/project/issues/2",
        "acceptance_artifact": artifact,
    }

    development = CodexCliBackend._development_prompt(request)
    publication = CodexCliBackend._publication_prompt(
        {
            **request,
            "acceptance_artifact": artifact,
        }
    )
    review = CodexCliBackend._review_prompt(request)

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
                "acceptance_artifact": {
                    "verdict": "request_changes",
                    "findings": [{"id": "F1", "problem": "Repair this."}],
                },
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
                "acceptance_artifact": {"verdict": "pass"},
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


def test_live_codex_backend_rejects_empty_minted_token(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(CodexProcessError, match="empty token"):
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
        raise GitHubCredentialError("GitHub did not grant exact permissions")

    with pytest.raises(CodexProcessError, match="exact permissions"):
        CodexCliBackend(credential_provider=reject).develop(
            {
                "checkout": str(checkout),
                "thread_id": None,
                "ticket": {"number": 3},
            }
        )


def test_controller_mints_token_with_exact_read_permissions(
    monkeypatch: Any,
) -> None:
    permissions = {
        "issues": "read",
        "metadata": "read",
        "pull_requests": "read",
    }
    response = io.BytesIO(
        json.dumps({"token": "minted-reader", "permissions": permissions}).encode()
    )
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.data)
        return response

    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_ID", "123")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *_: "jwt")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    token = mint_read_only_installation_token()

    assert token == "minted-reader"
    assert captured == {
        "url": "https://api.github.com/app/installations/456/access_tokens",
        "method": "POST",
        "authorization": "Bearer jwt",
        "body": {"permissions": permissions},
    }


def test_controller_rejects_minted_token_with_different_permissions(
    monkeypatch: Any,
) -> None:
    response = io.BytesIO(
        json.dumps(
            {
                "token": "over-scoped",
                "permissions": {
                    "issues": "write",
                    "metadata": "read",
                    "pull_requests": "read",
                },
            }
        ).encode()
    )
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_ID", "123")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *_: "jwt")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: response,
    )

    with pytest.raises(GitHubCredentialError, match="exact.*permissions"):
        mint_read_only_installation_token()


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


def test_worker_has_real_git_helpers_and_read_only_gh_auth(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "worker"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "reader-secret")
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


def test_sigint_terminates_worker_process_group(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    child_path = tmp_path / "child.pid"
    code = f"""
from pathlib import Path
from agent_run.worker_sandbox import run_worker_process
run_worker_process(
    ["sh", "-c", "sleep 60 & echo $! > child.pid; wait"],
    cwd=Path({str(tmp_path)!r}),
    prompt="",
    environment={{"PATH": "/usr/bin:/bin"}},
    timeout=120,
)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    controller = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        if child_path.exists():
            break
        time.sleep(0.02)
    assert child_path.exists()
    child_pid = int(child_path.read_text(encoding="utf-8").strip())

    os.kill(controller.pid, signal.SIGINT)
    controller.wait(timeout=5)

    assert controller.returncode not in {None, 0}
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_successful_worker_cleans_background_processes(
    tmp_path: Path,
) -> None:
    child_path = tmp_path / "child.pid"

    result = run_worker_process(
        [
            "sh",
            "-c",
            (
                "sleep 60 </dev/null >/dev/null 2>&1 & "
                "echo $! > child.pid"
            ),
        ],
        cwd=tmp_path,
        prompt="",
        environment={"PATH": "/usr/bin:/bin"},
        timeout=5,
    )

    assert result.returncode == 0
    child_pid = int(child_path.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_stdout_callback_error_does_not_stop_pipe_drain(tmp_path: Path) -> None:
    callback_calls = 0

    def fail_first_line(_line: str) -> None:
        nonlocal callback_calls
        callback_calls += 1
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

    started = time.monotonic()
    with pytest.raises(CodexProcessError, match="Thread mismatch"):
        run_worker_process(
            command,
            cwd=tmp_path,
            prompt="",
            environment=os.environ.copy(),
            timeout=3,
            on_stdout_line=fail_first_line,
        )

    assert callback_calls == 1
    assert time.monotonic() - started < 2


def test_stdout_callback_error_terminates_hanging_worker(tmp_path: Path) -> None:
    def reject_thread(_line: str) -> None:
        raise CodexProcessError("reported Thread mismatch")

    started = time.monotonic()
    with pytest.raises(CodexProcessError, match="Thread mismatch"):
        run_worker_process(
            ["sh", "-c", "printf 'thread.started\\n'; sleep 60"],
            cwd=tmp_path,
            prompt="",
            environment={"PATH": "/usr/bin:/bin"},
            timeout=5,
            on_stdout_line=reject_thread,
        )

    assert time.monotonic() - started < 2


def test_streaming_timeout_joins_reader_threads(tmp_path: Path) -> None:
    before = {
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("agent-run-worker-")
    }

    with pytest.raises(WorkerSandboxError, match="timed out"):
        run_worker_process(
            ["sh", "-c", "sleep 60"],
            cwd=tmp_path,
            prompt="",
            environment={"PATH": "/usr/bin:/bin"},
            timeout=0.1,
            on_stdout_line=lambda _line: None,
        )

    after = {
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("agent-run-worker-")
    }
    assert after == before
