"""准确 Run 审核事实及实际 Worker 输入的回归。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.prompt_resources import resolve_resources
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_currentness import ticket_completion_records
from support.prompt_capture import _capture_public_prompt
from support.run_artifacts import _passing_artifact
from support.run_scenarios import _completed_run


@pytest.mark.parametrize("field", ["parent_revision", "ticket_graph_revision", "ticket_completion_records", "expected_merge_tree"])
def test_candidate_reuse_requires_scope_facts_even_with_identical_tree(git_repo: Path, field: str) -> None:
    state, _states, git = _completed_run(git_repo)
    head = git.resolve(state["run_branch"])
    job = {
        "base_sha": head, "candidate_sha": head,
        "default_base_sha": git.resolve("main"), "repair_mode": "squash",
        "repair_source": "acceptance", "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
    }
    acceptance = CandidateRunAcceptance(git)
    record = acceptance.record(job, "independent-reviewer", _passing_artifact())
    assert acceptance.is_current(state, job, record)
    changed = deepcopy(record)
    changed[field] = [] if field == "ticket_completion_records" else "different-fact"
    assert not acceptance.is_current(state, job, changed)
    assert git.resolve(f"{head}^{{tree}}") == record["reviewed_candidate_tree"]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("continuation", [False, True])
def test_worker_receives_complete_run_integration_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str, continuation: bool,
) -> None:
    resources = resolve_resources(language=language)
    request = {
        "acceptance_scope": "run", "repair_source": "integration",
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "integration_evidence": 'source=SOURCE target=TARGET command=git diff --cached --check original 原始失败',
        "_prompt_resources": resources,
    }
    if continuation:
        request.update(thread_id="same-work", _invocation_mode="resume")
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "develop", request, name="run-integration")
    assert resources["development/repair-run-integration"].format("integration_evidence", request["integration_evidence"]) in prompt
    assert "SOURCE" in prompt and "TARGET" in prompt and "原始失败" in prompt
    assert resources["development/repair-integration"].split("{0}")[0] not in prompt


@pytest.mark.parametrize("language", ["zh", "en"])
def test_worker_run_review_requires_current_overall_conclusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    resources = resolve_resources(language=language)
    prompt = _capture_public_prompt(tmp_path, monkeypatch, "review", {
        "acceptance_scope": "run", "_prompt_resources": resources,
        "parent_issue_url": "https://github.com/example/project/issues/1",
        "run_review_facts": {"parent_revision": "PARENT_VERSION", "ticket_completion_records": ["REAL_COMPLETION"]},
        "current_review_identity": {"expected_merge_tree": "CURRENT_COMBINATION"},
        "previous_acceptance_artifact": _passing_artifact(),
    }, name="run-review")
    assert resources["review/run-complete-scope"] in prompt
    assert "PARENT_VERSION" in prompt and "REAL_COMPLETION" in prompt and "CURRENT_COMBINATION" in prompt
    assert "previous_acceptance_artifact" in prompt
