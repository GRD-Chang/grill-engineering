"""Parent acceptance reuse requires complete evidence for the exact result."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.development_prompts import development_prompt
from agent_run.parent_delivery_loop import ParentDeliveryAdapter
from agent_run.prompt_resources import builtin_resources
from agent_run.reviewer_prompts import review_prompt
from support.cli_payloads import passing_acceptance


def _artifact() -> dict[str, Any]:
    return {"checks": passing_acceptance("review-thread", "passed")["checks"]}


def _subject() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"run_id": "run", "repository": "example/repo", "parent": {
            "number": 399, "revision": "revision",
        }},
        {"base_sha": "base", "candidate_sha": "candidate", "effective_revision": "revision",
         "acceptance_artifact": _artifact()},
    )


class SnapshotGit:
    def resolve(self, ref: str) -> str:
        return ref.removesuffix("^{tree}") + "-tree"

    def expected_merge_tree(self, *, default_head_sha: str, run_head_sha: str) -> str:
        return f"merge-{default_head_sha}-{run_head_sha}"


@pytest.mark.parametrize("field,value", [
    ("acceptance_scope", "ticket"), ("parent_number", 400),
    ("effective_revision", "old-revision"), ("reviewed_base_sha", "other-base"),
    ("reviewed_candidate_sha", "other-candidate"), ("reviewed_candidate_tree", "other-tree"),
    ("expected_merge_tree", "other-merge"),
    ("reviewer_thread_id", ""),
])
def test_parent_reuse_rejects_each_changed_acceptance_fact(field: str, value: object) -> None:
    state, job = _subject()
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    record = adapter.acceptance_record(state, job, "review-thread", job["acceptance_artifact"])
    assert adapter.acceptance_is_current(state, job, record)
    record[field] = value
    assert not adapter.acceptance_is_current(state, job, record)


@pytest.mark.parametrize("status", ["fail", "blocked", "invalid"])
def test_parent_reuse_requires_all_three_lanes_to_pass(status: str) -> None:
    state, job = _subject()
    artifact = job["acceptance_artifact"]
    artifact["checks"]["spec"].update(
        status=status, findings=["missing requirement"] if status == "fail" else [],
    )
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    record = adapter.acceptance_record(state, job, "review-thread", artifact)
    assert not adapter.acceptance_is_current(state, job, record)


def test_parent_reuse_rejects_changed_requirement_and_mismatched_artifact() -> None:
    state, job = _subject()
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    record = adapter.acceptance_record(state, job, "review-thread", deepcopy(job["acceptance_artifact"]))
    state["parent"]["revision"] = "new-revision"
    assert not adapter.acceptance_is_current(state, job, record)
    state["parent"]["revision"] = "revision"
    job["acceptance_artifact"]["checks"]["e2e"]["evidence"] = "different evidence"
    assert not adapter.acceptance_is_current(state, job, record)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_parent_review_uses_compatible_full_baseline_for_changed_combination(tmp_path: Path, language: str) -> None:
    state, job = _subject()
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    previous = adapter.acceptance_record(state, job, "review-thread", job["acceptance_artifact"])
    job.update(previous_parent_acceptance_record=previous, base_sha="new-base", candidate_sha="new-candidate",
               parent_integration={"source_sha": "source", "target_sha": "new-base", "head_sha": "new-candidate"})
    request = adapter.review_request(state, job, tmp_path)
    assert request["previous_acceptance_artifact"] == previous["artifact"]
    assert request["previous_review_identity"]["expected_merge_tree"] == previous["expected_merge_tree"]
    assert request["current_review_identity"]["expected_merge_tree"] == "merge-new-base-new-candidate"
    assert request["work_source"] == "integration"
    assert request["integration_record"] == job["parent_integration"]
    prompt = review_prompt({**request, "_prompt_resources": builtin_resources(language)})
    assert "source" in prompt and "new-base" in prompt
    assert "merge-new-base-new-candidate" in prompt
    assert previous["artifact"]["checks"]["spec"]["evidence"] in prompt
    assert ("旧结果不能证明当前代码已经通过" if language == "zh"
            else "Previous results cannot prove that current code has passed") in prompt
    assert not adapter.acceptance_is_current(state, job, previous)
    previous["effective_revision"] = "old-revision"
    request = adapter.review_request(state, job, tmp_path)
    assert "previous_acceptance_artifact" not in request


def test_parent_findings_remain_a_full_review_baseline() -> None:
    state, job = _subject()
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    artifact = job["acceptance_artifact"]
    artifact["checks"]["spec"].update(status="fail", findings=["missing requirement"])
    record = adapter.acceptance_record(state, job, "review-thread", artifact)
    job["acceptance_record"] = record
    assert adapter.compatible_review_baseline(state, job) == record
    assert not adapter.acceptance_is_current(state, job, record)


@pytest.mark.parametrize("kind_field", ["next_attempt_kind", "pending_attempt_kind"])
def test_parent_integration_findings_development_has_complete_evidence(
    tmp_path: Path, kind_field: str,
) -> None:
    state, job = _subject()
    job.update(repair_source="acceptance", parent_integration={
        "source_sha": "collaborator-source", "target_sha": "live-target",
        "head_sha": "integrated-head", "conflict_paths": ["delivered.txt"],
    })
    job[kind_field] = "integration"
    job["acceptance_artifact"]["checks"]["spec"].update(
        status="fail", findings=["integrated behavior violates Parent requirement"],
    )
    adapter = ParentDeliveryAdapter(SnapshotGit(), SimpleNamespace())  # type: ignore[arg-type]
    adapter.git.checkout_head = lambda _: "candidate"  # type: ignore[method-assign]
    request = adapter.development_request(state, job, tmp_path)
    assert request["repair_source"] == request["work_source"] == "integration"
    assert "acceptance_artifact" not in request
    assert json.loads(request["integration_evidence"]) == {
        "acceptance_artifact": job["acceptance_artifact"],
        "parent_integration": job["parent_integration"],
    }
    prompt = development_prompt({**request, "_prompt_resources": builtin_resources("zh")})
    assert "integrated behavior violates Parent requirement" in prompt
    assert "collaborator-source" in prompt and "live-target" in prompt


def test_parent_integration_findings_repair_keeps_baseline_before_reintegration(git_repo: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=git_repo, capture_output=True,
                              check=True, text=True).stdout.strip()

    base = git("rev-parse", "HEAD")
    (git_repo / "parent.txt").write_text("parent\n", encoding="utf-8")
    git("add", "parent.txt")
    git("commit", "-m", "parent")
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    state, job = _subject()
    job.update(base_sha=base, candidate_sha=head)
    adapter = ParentDeliveryAdapter(GitRepository(git_repo), SimpleNamespace())  # type: ignore[arg-type]
    record = adapter.acceptance_record(state, job, "review-thread", job["acceptance_artifact"])
    assert record["expected_merge_tree"] == tree
    job.update(parent_integration_stage=True, parent_integration={
        "candidate_sha": head, "candidate_tree": tree, "source_sha": base,
        "target_sha": base, "head_sha": head, "head_tree": tree, "conflict_paths": [],
    })
    (git_repo / "parent.txt").write_text("repaired\n", encoding="utf-8")
    git("add", "parent.txt")
    git("commit", "-m", "repair findings")
    job["candidate_sha"] = git("rev-parse", "HEAD")
    assert adapter.base_is_current(base, job)
    assert not adapter.acceptance_is_current(state, job, record)
    job["base_sha"] = "wrong-base"
    assert not adapter.base_is_current(base, job)
