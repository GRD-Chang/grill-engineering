from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError


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
        "commit_message": "fix(agent): publish validated repair",
        "pr_title": "fix(agent): publish validated repair",
        "pr_body_markdown": (
            "## What Problem This Solves\n\nA blocker stopped delivery.\n\n"
            "## Why This Change Was Made\n\nThe repair restores progress.\n\n"
            "## User Impact\n\nDelivery can continue.\n\n"
            "## Evidence\n\nIndependent validation passed."
        ),
    }
    acceptance_result = {
        "verdict": "pass",
        "checks": {
            lane: {"status": "pass", "evidence": f"{lane} passed."}
            for lane in ("e2e", "standards", "spec")
        },
        "findings": [],
        "human_blockers": [],
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
            output = "Implemented and verified."
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
    artifact = {"verdict": "request_changes", "marker": "ARTIFACT_SENTINEL"}
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


def test_run_prompts_describe_run_scope_without_controller_private_records() -> None:
    checks_repair = CodexCliBackend._development_prompt(
        {
            "acceptance_scope": "run",
            "repair_source": "required_checks",
            "parent_issue_url": "https://github.com/example/project/issues/1",
            "ci_evidence": {"check": "failed"},
        }
    )
    run_acceptance = CodexCliBackend._review_prompt(
        {
            "acceptance_scope": "run",
            "parent_issue_url": "https://github.com/example/project/issues/1",
        }
    )

    assert "Required-Checks Repair：当前 Delivery Run" in checks_repair
    assert "Required-Checks Repair：当前 Ticket" not in checks_repair
    assert "Completion Record" not in run_acceptance
    assert "Expected Merge Result" not in run_acceptance
    assert "准备好的累计 diff" in run_acceptance


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
            "Access was rechecked; implementation completed.", encoding="utf-8"
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
            "Access was rechecked; implementation completed.", encoding="utf-8"
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
