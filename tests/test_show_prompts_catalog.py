"""The source preview catalog displays current role inputs without running work."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run.contracts.artifact import role_schema
from agent_run.core.prompts.role import prompt_schema, render_prompt
from agent_run.install.probe import PROBE_SCHEMA
from agent_run.model import Checks


SCRIPT = Path(__file__).parents[1] / "show-prompts.py"
PARENT = "https://example.invalid/demo/issues/1"
TICKET = "https://example.invalid/demo/issues/2"


@pytest.fixture
def catalog(monkeypatch):
    spec = importlib.util.spec_from_file_location("show_prompts_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The standalone script locates source and suppresses bytecode. Do not let
    # those standalone startup choices leak into other installed-runtime tests.
    with monkeypatch.context() as startup:
        startup.setattr(sys, "path", list(sys.path))
        startup.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("language", ["zh", "en"])
def test_catalog_covers_only_current_delivery_roles_and_independent_schemas(catalog, language):
    previews = catalog.scenarios(PARENT, TICKET, language)
    required = {
        "development", "parent-development", "review", "parent-review", "run-review",
        "fallback-publication", "ci-waiver-publication", "publication", "parent-publication",
        "final-publication", "parent-integration-review", "run-integration-review",
        "parent-integration-review-followup", "run-integration-review-followup",
        "run-repair-human_revision", "run-human-revision-review",
        "runner-probe", "publication-probe",
    }
    for prefix in ("", "parent-", "run-"):
        required.update(prefix + name for name in (
            "repair-acceptance", "review-followup", "repair-required_checks", "ci-review",
            "ci-findings-repair", "ci-review-followup", "repair-integration", "repair-git_integrity"))
    assert required <= previews.keys()
    assert not {"candidate-run-review", "run-repair-review", "run-repair-publication",
                "run-development", "integration-review", "parent-ci-waiver-publication",
                "run-ci-waiver-publication", "repair-human_revision",
                "parent-repair-human_revision"} & previews.keys()
    for title, prompt, schema in previews.values():
        assert title and prompt and isinstance(schema, dict)
        assert schema["type"] == "object"
        assert "additionalProperties" not in prompt
    for name, role in (("development", "development"), ("review", "review"),
                       ("publication", "publication"), ("final-publication", "publication"),
                       ("output-repair-review", "review"), ("output-repair-publication", "publication")):
        assert previews[name][2] == role_schema(role)
    assert previews["runner-probe"][2] == PROBE_SCHEMA
    assert previews["publication-probe"][2] == role_schema("publication")
    expected = ("你是负责本次交付的独立验收工程师" if language == "zh"
                else "You are the independent acceptance engineer")
    other = ("You are the independent acceptance engineer" if language == "zh"
             else "你是负责本次交付的独立验收工程师")
    assert expected in previews["review"][1] and other not in previews["review"][1]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_catalog_routes_fixed_facts_and_original_evidence_through_shared_renderer(catalog, monkeypatch, language):
    calls = []

    def capture(request, role, *, continuation=False):
        calls.append((request, role, continuation))
        return render_prompt(request, role, continuation=continuation)

    monkeypatch.setattr(catalog, "render_prompt", capture)
    previews = catalog.scenarios(PARENT, TICKET, language)
    # Only the two explicitly labelled probe resources are not role requests.
    assert len(calls) == len(previews) - 2
    for (name, (_, prompt, schema)), (request, role, continuation) in zip(
        list(previews.items())[:-2], calls, strict=True,
    ):
        assert schema == prompt_schema(role, request)
        assert prompt == render_prompt(request, role, continuation=continuation)
        assert request["language"] == language
        assert "candidate_acceptance" not in request and "repair_scope" not in request
        if role == "review":
            context = request["review_context"]
            assert set(context) == {"facts", "review_range"}
            assert context["review_range"]["candidate_sha"] == request["reviewed_commit"]
            assert request["current_review_identity"]["reviewed_candidate_sha"] == request["reviewed_commit"]
            assert context["review_range"]["base_sha"] in prompt
            assert request["reviewed_commit"] in prompt
            assert "full_range" not in prompt and "focus_range" not in prompt
            assert "ticket_integration_records" not in request and "ticket_integration_records" not in context["facts"]
            if request["acceptance_scope"] == "run":
                completion = context["facts"]["ticket_completions"][0]
                assert completion["generation"] == 1 and completion["pr_number"] == 3
                assert "issue_url" not in completion and "result" not in completion
                assert completion["review_attempt"] == completion["review"]["attempt"]
                assert completion["development_attempt"] == completion["development"]["attempt"]
                assert completion["delivered_candidate"]["sha"] == completion["review"]["candidate_sha"]
                assert "example-completion-comment" in prompt and "example-ticket-close" in prompt
            if "ci-" in name:
                evidence = context["facts"]["ci_evidence"]
                assert Checks(**evidence).result == "fail"
                assert set(evidence) == {"pr_number", "head_sha", "result", "details", "logs"}
                assert evidence["pr_number"] == 3
                assert evidence["logs"] == "【示例原始日志】AssertionError: expected greeting\r\n"
                assert json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) in prompt
            if "integration-review" in name:
                evidence = context["facts"]["integration_record"]
                assert json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) in prompt
                assert "development_summary" not in context["facts"]
                assert prompt.count(evidence["development_summary"]) == 1
                if name.endswith("followup"):
                    assert evidence["development_attempt"] == "example-integration-findings-development"
                    assert evidence["development_result"] == "example-integration-findings-result"
                    assert "最新集成 Findings 修复" in evidence["development_summary"]
        if name.startswith("continue-"):
            assert continuation and request["_invocation_mode"] == "resume"
        if name.startswith("fresh-"):
            assert not continuation and request["_invocation_mode"] == "new-thread"
            assert request["thread_id"] is None
        if name.startswith("human-") and "revision-review" not in name:
            assert continuation
            assert "已确认目标行为" in prompt


def test_catalog_inspection_does_not_launch_processes_or_persist_state(catalog, monkeypatch):
    from agent_run.config.paths import app_data_root
    from agent_run.config.resources import personal_method_directory
    from agent_run.storage.sqlite import Storage

    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not launch a process, model, or state transaction")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("agent_run.runtime.codex.execute_selected_codex", forbidden)
    monkeypatch.setattr(Storage, "transaction", forbidden)
    assert not personal_method_directory().exists()
    assert not app_data_root().exists()
    assert catalog.scenarios(PARENT, TICKET)
    assert not personal_method_directory().exists()
    assert not app_data_root().exists()


def test_source_preview_cli_lists_and_writes_selected_schema_without_changing_cwd(tmp_path):
    def invoke(*arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=tmp_path,
                              capture_output=True, text=True, timeout=15)

    listing = invoke("--list")
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "ci-waiver-publication" in listing.stdout
    assert "candidate-run-review" not in listing.stdout
    output = tmp_path / "preview.md"
    rendered = invoke("run-ci-review-followup", "--parent-url", PARENT, "--task-url", TICKET,
                      "--language", "en", "--output", str(output))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    preview = output.read_text(encoding="utf-8")
    assert PARENT in preview
    assert "You are the independent acceptance engineer" in preview
    assert "你是负责本次交付的独立验收工程师" not in preview
    assert "```text\n" in preview and "```json\n" in preview
    schema = json.loads(preview.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert schema == role_schema("review")
    assert "均为示例" in preview and "#446" in preview
    invalid = invoke("candidate-run-review")
    assert invalid.returncode == 2
    assert "未知阶段" in invalid.stderr
