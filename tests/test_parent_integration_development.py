"""Parent Git retries share Development allowance and preserve exact evidence."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.delivery_step import DeliveryStepKind
from agent_run.development_review import DevelopmentReview
from agent_run.git_errors import GitError
from agent_run.parent_integration import finish_parent_snapshot


def _scene() -> dict[str, Any]:
    return dict(source_sha="source", target_sha="target", candidate_sha="candidate",
                stage="target", head_sha="candidate", merge_head_sha="target",
                paths=["file.txt"], conflict_paths=["file.txt"], resolved_stages=[])


def _stage(job: dict[str, Any], develop: Any, *, fail_checks: bool) -> Any:
    calls = 0

    def commit(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if fail_checks and calls == 1:
            raise GitError("whitespace", evidence=dict(
                operation="commit_integration_resolution", command=["git", "diff", "--cached", "--check"],
                returncode=2, stdout_json='"file.txt:1: trailing whitespace.\\r\\n"',
                head_sha="candidate", merge_head_sha="target",
            ))
        return "resolved"

    git = SimpleNamespace(
        checkout_head=lambda checkout: "candidate", commit_parents=lambda head: [],
        commit_integration_resolution=commit, is_ancestor=lambda *args: True,
        integrate_remote_heads=lambda *args, **kwargs: SimpleNamespace(status="clean", head_sha="resolved"),
        resolve=lambda ref: ref + "-tree",
    )
    return DevelopmentReview(
        git=git, agents=SimpleNamespace(develop=develop),
        github=SimpleNamespace(),
        states=SimpleNamespace(save_run=lambda *_args: None),
    )


def _state(job: dict[str, Any]) -> dict[str, Any]:
    # These tests start at an already-recorded integration scene. Supply the
    # original Parent identity required by the real request/currentness seam.
    job.update(base_sha="base", candidate_sha="candidate", effective_revision="revision", phase="accepted")
    return {
        "run_id": "run", "repository": "example/project",
        "base": {"branch": "main"},
        "parent": {"number": 1, "revision": "revision"},
        "policy_snapshot": job["policy_snapshot"], "prompt_resources": {},
    }


def _resolve(work: DevelopmentReview, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
    while True:
        step = work.develop_integration(state, job, checkout, scope="parent-only", allow_agent=True)
        if step.kind != DeliveryStepKind.CONTINUE:
            return False
        result = finish_parent_snapshot(
            git=work.git, github=work.github, state_store=work.state_store,
            state=state, job=job, checkout=checkout,
        )
        if result.status != "conflict":
            return True


def test_parent_git_failure_returns_exact_evidence_and_charges_each_new_work(tmp_path: Path) -> None:
    job: dict[str, Any] = dict(parent_branch="parent", policy_snapshot=DeliveryPolicy().snapshot(),
                               parent_integration_conflict=_scene())
    requests: list[dict[str, Any]] = []

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        requests.append(request)
        if len(requests) == 2:
            scene = json.loads(request["integration_evidence"])
            assert scene["source_sha"] == "source" and scene["target_sha"] == "target"
            assert scene["failure"]["command"] == ["git", "diff", "--cached", "--check"]
            assert json.loads(scene["failure"]["stdout_json"]).endswith("\r\n")
        assert request["repair_source"] == "integration"
        return DevelopmentResult(thread_id="parent-thread", summary="Handled files")

    state = _state(job)
    stage = _stage(job, develop, fail_checks=True)
    assert _resolve(stage, state, job, tmp_path)
    assert len(requests) == 2
    assert job["integration_development_budget"]["used"] == 2
    assert "parent_integration_conflict" not in job
    assert job["parent_integration"]["head_sha"] == "resolved"
    assert len(job["semantic_attempt_history"]) == 2


def test_parent_interrupted_attempt_resumes_without_charging_again(tmp_path: Path) -> None:
    job: dict[str, Any] = dict(parent_branch="parent", policy_snapshot=DeliveryPolicy().snapshot(),
                               parent_integration_conflict=_scene())
    calls = 0

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("interrupted")
        return DevelopmentResult(thread_id="parent-thread", summary="Handled files")

    state = _state(job)
    stage = _stage(job, develop, fail_checks=False)
    with pytest.raises(RuntimeError, match="interrupted"):
        _resolve(stage, state, job, tmp_path)
    pending_id = job["pending_semantic_attempt"]["attempt_id"]
    assert job["integration_development_budget"]["used"] == 1
    assert _resolve(stage, state, job, tmp_path)
    assert job["integration_development_budget"]["used"] == 1
    assert job["integration_development_budget"]["attempt_ids"] == [pending_id]


def test_parent_git_failure_exhausts_allowance_before_another_agent(tmp_path: Path) -> None:
    job: dict[str, Any] = dict(parent_branch="parent", policy_snapshot=DeliveryPolicy(run_repair_rounds=1).snapshot(),
                               parent_integration_conflict=_scene())
    calls = 0

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        nonlocal calls
        calls += 1
        return DevelopmentResult(thread_id="parent-thread", summary="Handled files")

    state = _state(job)
    stage = _stage(job, develop, fail_checks=True)
    assert not _resolve(stage, state, job, tmp_path)
    assert calls == 1
    assert job["blocked_reason"] == "integration_development_exhausted"
    assert job["integration_development_budget"]["used"] == 1
    assert state["terminal_kind"] == "waiting_human"


def test_parent_completed_summary_survives_interrupted_git_closeout(tmp_path: Path) -> None:
    job: dict[str, Any] = dict(parent_branch="parent", policy_snapshot=DeliveryPolicy().snapshot(),
                               parent_integration_conflict=_scene())
    calls = 0

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        nonlocal calls
        calls += 1
        return DevelopmentResult(thread_id="parent-thread", summary="Handled files")

    state = _state(job)
    stage = _stage(job, develop, fail_checks=False)
    commit = stage.git.commit_integration_resolution

    def interrupted(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("closeout interrupted")

    stage.git.commit_integration_resolution = interrupted
    with pytest.raises(RuntimeError, match="closeout interrupted"):
        _resolve(stage, state, job, tmp_path)
    stage.git.commit_integration_resolution = commit
    assert _resolve(stage, state, job, tmp_path)
    assert calls == 1
    assert job["integration_development_budget"]["used"] == 1


def test_parent_real_git_check_retries_original_failure(git_repo: Path) -> None:
    from agent_run.git import GitRepository

    git = GitRepository(git_repo)
    head = git.checkout_head(git_repo)
    scene = dict(_scene(), source_sha=head, target_sha=head, candidate_sha=head,
                 stage="git_check", head_sha=head, merge_head_sha=None,
                 paths=[], conflict_paths=[])
    job: dict[str, Any] = dict(parent_branch="parent", policy_snapshot=DeliveryPolicy().snapshot(),
                               parent_integration_conflict=scene)
    calls = 0

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            (git_repo / "new.csv").write_bytes(b"key,value\r\n1,2\r\n")
        else:
            failure = json.loads(request["integration_evidence"])["failure"]
            assert failure["command"] == ["git", "diff", "--cached", "--check"]
            assert failure["returncode"] != 0
            assert "new.csv" in json.loads(failure["stdout_json"])
            (git_repo / "new.csv").write_bytes(b"key,value\n1,2\n")
        return DevelopmentResult(thread_id="parent-thread", summary="Handled files")

    state = _state(job)
    job.update(base_sha=head, candidate_sha=head)
    stage = _stage(job, develop, fail_checks=False)
    stage.git = git
    assert _resolve(stage, state, job, git_repo)
    assert calls == 2
    assert job["integration_development_budget"]["used"] == 2
    assert git.checkout_head(git_repo) == job["parent_integration"]["head_sha"]
    assert git._run_in(git_repo, "status", "--porcelain=v1").stdout == ""
