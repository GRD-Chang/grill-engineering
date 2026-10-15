"""Parent repairs retain real remote Run Branch appends without internal PRs."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.delivery_step import AgentWorkRequired
from agent_run.git import GitError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import _candidate_finding_artifact, _passing_artifact
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _completed_run
from support.state_commits import observe_run_commits


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("scenario", [
    "append", "conflict", "conflict_git_check", "interrupted",
    "interrupted_rewritten", "interrupted_fetch_retry",
])
def test_run_repair_preserves_remote_run_append_and_resumes_same_work(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    conflict = scenario.startswith("conflict")
    git_check = scenario == "conflict_git_check"
    interrupted = scenario.startswith("interrupted")
    state, states, git = _completed_run(
        git_repo, delivery_policy=DeliveryPolicy(run_repair_rounds=3)
    )
    run_id = str(state["run_id"])
    fixture = git_repo / "github.json"

    class AppendAgents(ScriptedRunAgents):
        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            raise AssertionError("Parent repair must not call internal Publish")

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration" or "acceptance_artifact" in json.loads(request["integration_evidence"]):
                result = super().develop(request)
                if interrupted and len(self.development_requests) == 2:
                    (Path(request["checkout"]) / "follow-up-repair.txt").write_text("second repair\n")
                return result
            self.development_requests.append(request)
            event = request["_invocation_event"]
            event("started", requested_thread_id="run-repair-developer", attempt_count=1)
            event("thread_started", reported_thread_id="run-repair-developer")
            assert StateStore(states.root).load_current_run(run_id) is not None
            checkout = Path(request["checkout"])
            integration_calls = sum(item.get("repair_source") == "integration" for item in self.development_requests)
            if git_check:
                if integration_calls == 2:
                    evidence = json.loads(request["integration_evidence"])
                    assert "diff --cached --check" in evidence["failure"]["error"]
                    assert "market.csv" in evidence["failure"]["error"]
                    assert (checkout / "market.csv").read_bytes().endswith(b"\r\n")
                (checkout / "market.csv").write_bytes(
                    b"code,value\r\nA,1\r\n" if integration_calls == 1 else b"code,value\nA,1\n"
                )
            (checkout / "run-repair.txt").write_text("repaired with human change\n")
            event("completed")
            return DevelopmentResult("run-repair-developer", "Resolved both contributions")

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            if request.get("candidate_acceptance"):
                assert (Path(request["checkout"]) / "run-repair.txt").read_text() == (
                    "repaired with human change\n" if conflict and len(self.review_requests) > 2 else "repaired\n"
                )
            return ReviewResult(f"run-reviewer-{len(self.review_requests)}", self._reviews.pop(0))

    agents = AppendAgents()

    def accept() -> dict[str, Any]:
        for _ in range(4):
            try:
                return RunAcceptanceEngine(
                    git=git, states=states, agents=agents,
                    github=FixtureGitHubPublisher(fixture, git),
                ).accept(run_id)
            except AgentWorkRequired:
                # Public orchestration re-admits each independent Agent work unit.
                continue
        raise AssertionError("Repair did not finish within the existing work budget")

    def stop_after_review(identifier: str, value: dict[str, Any]) -> None:
        if value.get("run_acceptance", {}).get("repair_job", {}).get("phase") == "accepted":
            raise OSError("interrupt before Run Branch update")

    with monkeypatch.context() as pause:
        observe_run_commits(pause, states, after=stop_after_review)
        with pytest.raises(OSError, match="interrupt before Run Branch update"):
            accept()
    saved = states.load_current_run(run_id)
    assert saved is not None
    job = saved["run_acceptance"]["repair_job"]
    original_job_id = job["repair_generation"]
    original_ci = deepcopy(saved["run_acceptance"]["ci_recovery"])
    original_candidate = job["candidate_sha"]
    branch = str(state["run_branch"])
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", "-b", branch, str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")
    human_file = "run-repair.txt" if conflict else "human-target.txt"
    (collaborator / human_file).write_text("human repair\n")
    _git(collaborator, "add", human_file)
    _git(collaborator, "commit", "-m", "fix: append human contribution to Run")
    human_sha = _git(collaborator, "rev-parse", "HEAD")
    _git(collaborator, "push", "origin", branch)
    data = json.loads(fixture.read_text())
    data.setdefault("delivery", {}).setdefault("published_branches", {})[branch] = human_sha
    fixture.write_text(json.dumps(data))
    agents._reviews = [_candidate_finding_artifact(), _passing_artifact()] if interrupted else [_passing_artifact()]
    if interrupted:
        def stop_after_candidate(identifier: str, value: dict[str, Any]) -> None:
            repair = value.get("run_acceptance", {}).get("repair_job", {})
            if repair.get("phase") == "candidate" and len(agents.development_requests) == 2:
                raise OSError("interrupt after repair Candidate")
        with monkeypatch.context() as pause:
            observe_run_commits(pause, states, after=stop_after_candidate)
            with pytest.raises(OSError, match="interrupt after repair Candidate"):
                accept()
        saved = states.load_current_run(run_id)
        assert saved is not None
        saved_job = saved["run_acceptance"]["repair_job"]
        assert saved_job["phase"] == "candidate"
        assert saved_job["base_sha"] == human_sha
        assert saved_job["repair_generation"] == original_job_id
        assert "pr_number" not in saved_job
        assert saved["run_acceptance"]["ci_recovery"] == original_ci
        if scenario == "interrupted_rewritten":
            rewritten = git.resolve("main")
            _git(git_repo, "push", "--force", "origin", f"{rewritten}:refs/heads/{branch}")
            data = json.loads(fixture.read_text())
            data["delivery"]["published_branches"][branch] = rewritten
            fixture.write_text(json.dumps(data))
            blocked, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
            assert blocked["status"] == "run_acceptance_pending"
            blocked = accept()
            assert blocked["status"] == "ready_for_human"
            assert blocked["diagnostics"][0]["code"] == "integration_history_rewritten"
            assert _git(git_repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == rewritten
            assert blocked["run_acceptance"]["repair_job"]["candidate_sha"] == saved_job["candidate_sha"]
            return
        if scenario == "interrupted_fetch_retry":
            (collaborator / "later-target.txt").write_text("later target\n")
            _git(collaborator, "add", "later-target.txt")
            _git(collaborator, "commit", "-m", "fix: append after interruption")
            later = _git(collaborator, "rev-parse", "HEAD")
            _git(collaborator, "push", "origin", branch)
            data = json.loads(fixture.read_text())
            data["delivery"]["published_branches"][branch] = later
            fixture.write_text(json.dumps(data))
            fetch = git.fetch_remote_commit
            failed = False
            def fail_once(ref: str, sha: str) -> str:
                nonlocal failed
                if not failed:
                    failed = True
                    raise GitError("temporary fetch failure")
                return fetch(ref, sha)
            with monkeypatch.context() as retry:
                retry.setattr(git, "fetch_remote_commit", fail_once)
                resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
                paused = accept()
            assert failed
            assert paused["status"] == "ready_for_human"
            assert paused["diagnostics"][0]["code"] == "run_integration_failed"
            resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id, explicit_resume=True)
        else:
            resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
        assert resumed["status"] == "run_acceptance_pending", resumed.get("diagnostics")
        assert resumed["run_acceptance"]["repair_job"]["candidate_sha"] == saved_job["candidate_sha"]
        assert resumed["run_acceptance"]["ci_recovery"] == original_ci
    result = accept()
    assert result["status"] == "run_publication_pending", result.get("diagnostics")
    completed = result["run_acceptance"]["completed_repair_jobs"][-1]
    assert "repair_pr_number" not in result["run_acceptance"]
    assert not json.loads(fixture.read_text())["delivery"].get("pull_requests")
    assert git.is_ancestor(human_sha, completed["integrated_sha"])
    assert git.is_ancestor(str(state["base"]["sha"]), completed["integrated_sha"])
    assert git.resolve(f"{original_candidate}^{{tree}}") != git.resolve(f"{completed['candidate_sha']}^{{tree}}")
    assert result["run_acceptance"]["ci_recovery"] == original_ci
    assert len(agents.development_requests) == (3 if git_check else 2 if conflict or interrupted else 1)
    if git_check:
        assert result["run_acceptance"]["review_budget"]["development_attempts"] == 3
        history = states.load_current_run(run_id)
        assert history is not None
        attempts = [item["semantic_attempt"] for item in history["agent_invocation_history"]
                    if item["phase"] == "integration_conflict" and item["status"] == "completed"]
        assert [item["budget_snapshot"]["development_attempts"] for item in attempts] == [2, 3]
        assert len({item["attempt_id"] for item in attempts}) == 2
        assert _git(git_repo, "show", f"{branch}:market.csv") == "code,value\nA,1"
    assert _git(git_repo, "show", f"{branch}:{human_file}") == (
        "repaired with human change" if conflict else "human repair"
    )
    final = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=FixtureGitHubPublisher(fixture, git), default_branch="main",
        default_head_sha=str(state["base"]["sha"]),
    )
    assert final.publish(run_id)["status"] == "run_approval_pending"
    assert final.approve(run_id)["status"] == "completed"
    assert _git(git_repo, "show", f"main:{human_file}") == (
        "repaired with human change" if conflict else "human repair"
    )
    if interrupted:
        assert _git(git_repo, "show", "main:follow-up-repair.txt") == "second repair"
    if scenario == "interrupted_fetch_retry":
        assert _git(git_repo, "show", "main:later-target.txt") == "later target"


def test_merge_resolution_repair_preserves_default_ancestor_and_remote_run_append(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.merge_resolution import MergeResolutionFixture

    scene = MergeResolutionFixture(git_repo, "happy_path")
    def stop_after_review(run_id: str, value: dict[str, Any]) -> None:
        if value.get("run_acceptance", {}).get("repair_job", {}).get("phase") == "accepted":
            raise OSError("interrupt before Run Branch update")

    with monkeypatch.context() as pause:
        observe_run_commits(pause, scene.states, after=stop_after_review)
        with pytest.raises(OSError, match="interrupt before Run Branch update"):
            scene.accept()
    saved = scene.states.load_current_run(str(scene.state["run_id"]))
    assert saved is not None
    job = saved["run_acceptance"]["repair_job"]
    assert job["repair_mode"] == "merge_resolution"
    original_candidate = str(job["candidate_sha"])
    assert scene.git.is_ancestor(scene.default_head, original_candidate)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", "-b", scene.run_branch, str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")
    (collaborator / "human.txt").write_text("human repair\n")
    _git(collaborator, "add", "human.txt")
    _git(collaborator, "commit", "-m", "fix: append to Run Branch")
    human = _git(collaborator, "rev-parse", "HEAD")
    _git(collaborator, "push", "origin", scene.run_branch)
    data = json.loads(scene.fixture.read_text())
    data.setdefault("delivery", {}).setdefault("published_branches", {})[scene.run_branch] = human
    scene.fixture.write_text(json.dumps(data))
    promoted = scene.accept()
    assert promoted["status"] == "run_publication_pending", promoted.get("diagnostics")
    completed = promoted["run_acceptance"]["completed_repair_jobs"][-1]
    integrated = completed["integrated_sha"]
    assert integrated == completed["candidate_sha"]
    assert scene.git.is_ancestor(scene.default_head, integrated)
    assert scene.git.is_ancestor(human, integrated)
    assert scene.git.is_ancestor(original_candidate, integrated)
    assert not json.loads(scene.fixture.read_text())["delivery"].get("pull_requests")
    assert _git(git_repo, "show", f"{scene.run_branch}:human.txt") == "human repair"
    final = RunPublicationEngine(
        git=scene.git, states=scene.states, agents=RunPublicationAgents(),
        github=FixtureGitHubPublisher(scene.fixture, scene.git),
        default_branch="main", default_head_sha=scene.default_head,
    )
    assert final.publish(str(scene.state["run_id"]))["status"] == "run_approval_pending"
    assert final.approve(str(scene.state["run_id"]))["status"] == "completed"
    assert _git(git_repo, "show", "main:human.txt") == "human repair"
