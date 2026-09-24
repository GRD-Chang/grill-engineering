"""Run Repair collaboration retains independent review and CI allowances."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import _passing_artifact
from support.run_scenarios import _completed_run


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def test_three_real_appends_open_new_repair_review_windows(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git = _completed_run(
        git_repo, delivery_policy=DeliveryPolicy(run_repair_rounds=1)
    )
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()

    def accept() -> dict:
        return RunAcceptanceEngine(
            git=git, states=states, agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))

    waiting = accept()
    job = waiting["run_acceptance"]["repair_job"]
    assert job["phase"] == "waiting_checks"
    assert waiting["run_acceptance"]["review_budget"]["development_attempts"] == 1
    assert waiting["run_acceptance"]["review_budget"]["reviewer_invocations"] == 2
    branch = str(job["repair_branch"])
    pr_number = job["pr_number"]
    ci_budget = deepcopy(job["ci_recovery"])
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    _git(
        git_repo, "push", "origin",
        f"{job['publication_sha']}:refs/heads/{branch}",
        f"{job['base_sha']}:refs/heads/{state['run_branch']}",
    )
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", "-b", branch, str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")

    windows = []
    for index in (1, 2, 3):
        if index > 1:
            _git(git_repo, "push", "origin", f"{job['publication_sha']}:refs/heads/{branch}")
            _git(collaborator, "pull", "--ff-only")
        (collaborator / f"human-{index}.txt").write_text(
            f"human contribution {index}\n", encoding="utf-8"
        )
        _git(collaborator, "add", f"human-{index}.txt")
        _git(collaborator, "commit", "-m", f"fix: append human change {index}")
        human_sha = _git(collaborator, "rev-parse", "HEAD")
        _git(collaborator, "push", "origin", branch)
        data = json.loads(fixture.read_text(encoding="utf-8"))
        delivery = data["delivery"]
        delivery["published_branches"][branch] = human_sha
        delivery["pull_requests"][0]["head_sha"] = human_sha
        delivery["required_checks"] = ["pending"]
        delivery["check_position"] = 0
        fixture.write_text(json.dumps(data), encoding="utf-8")
        agents._reviews.append(_passing_artifact())

        waiting = accept()
        job = waiting["run_acceptance"]["repair_job"]
        assert job["phase"] == "waiting_checks", job.get("blocked_reason")
        assert job["pr_number"] == pr_number
        assert git.is_ancestor(human_sha, str(job["publication_sha"]))
        assert job["ci_recovery"] == ci_budget
        assert waiting["run_acceptance"]["review_budget"]["window"] == job["review_budget"]["window"]
        assert job["review_budget"]["window"] == index + 1
        windows.append(job["review_budget"]["window"])

    assert windows == [2, 3, 4]
    assert len(agents.review_requests) == 5  # Initial Run, first Repair, three appends.
    for index in (1, 2, 3):
        assert _git(git_repo, "show", f"{job['publication_sha']}:human-{index}.txt") == f"human contribution {index}"


@pytest.mark.parametrize("external_append", [False, True])
def test_temporary_repair_fetch_failure_resumes_without_duplicate_review(
    git_repo: Path, tmp_path: Path, monkeypatch, external_append: bool,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()

    def accept() -> dict:
        return RunAcceptanceEngine(
            git=git, states=states, agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))

    waiting = accept()
    job = waiting["run_acceptance"]["repair_job"]
    assert job["phase"] == "waiting_checks"
    original_pr = job["pr_number"]
    original_publication = job["publication_sha"]
    original_ci = deepcopy(job["ci_recovery"])
    review_count = len(agents.review_requests)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    if external_append:
        checkout = tmp_path / "human"
        _git(git_repo, "worktree", "add", "--detach", str(checkout), str(original_publication))
        (checkout / "human.txt").write_text("human contribution\n", encoding="utf-8")
        _git(checkout, "add", "human.txt")
        _git(checkout, "commit", "-m", "fix: append human contribution")
        human_sha = _git(checkout, "rev-parse", "HEAD")
        data["delivery"]["published_branches"][str(job["repair_branch"])] = human_sha
        data["delivery"]["pull_requests"][0]["head_sha"] = human_sha
    data["delivery"]["required_checks"] = ["none"]
    data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(data), encoding="utf-8")
    original_fetch = git.fetch_remote_commit
    failed = False

    def fail_once(branch: str, sha: str) -> str:
        nonlocal failed
        if branch == job["repair_branch"] and not failed:
            failed = True
            raise GitError("temporary fetch transport failure")
        return original_fetch(branch, sha)

    monkeypatch.setattr(git, "fetch_remote_commit", fail_once)
    blocked = accept()
    assert failed
    blocked_job = blocked["run_acceptance"]["repair_job"]
    assert blocked["status"] == "ready_for_human"
    assert blocked_job["blocked_reason"] == "integration_failed"
    assert "run_repair_integration_history_rewrite" not in blocked_job
    assert blocked_job["pr_number"] == original_pr
    assert blocked_job["publication_sha"] == original_publication
    monkeypatch.setattr(git, "fetch_remote_commit", original_fetch)

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"]), explicit_resume=True,
    )
    assert resumed["run_acceptance"]["repair_job"]["phase"] != "blocked"
    if external_append:
        agents._reviews.append(_passing_artifact())
    completed = accept()
    assert completed["status"] == "run_publication_pending"
    assert completed["run_acceptance"]["repair_pr_number"] == original_pr
    assert completed["run_acceptance"]["ci_recovery"] == original_ci
    assert len(agents.review_requests) == review_count + int(external_append)
