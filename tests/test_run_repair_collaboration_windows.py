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


def test_three_real_appends_preserve_repair_budget_until_explicit_renewal(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git = _completed_run(
        git_repo, delivery_policy=DeliveryPolicy(run_repair_rounds=1)
    )
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()

    save = states.save_run
    interrupt = True
    interrupted_candidates: set[str] = set()

    def stop_after_acceptance(run_id: str, value: dict) -> None:
        save(run_id, value)
        repair = value.get("run_acceptance", {}).get("repair_job", {})
        candidate = str(repair.get("candidate_sha"))
        if interrupt and repair.get("phase") == "accepted" and candidate not in interrupted_candidates:
            interrupted_candidates.add(candidate)
            raise OSError("interrupt before Run Branch update")

    def accept() -> dict:
        with monkeypatch.context() as pause:
            pause.setattr(states, "save_run", stop_after_acceptance)
            try:
                return RunAcceptanceEngine(
                    git=git, states=states, agents=agents,
                    github=FixtureGitHubPublisher(fixture, git),
                ).accept(str(state["run_id"]))
            except OSError as error:
                assert str(error) == "interrupt before Run Branch update"
                result = states.load_current_run(str(state["run_id"]))
                assert result is not None
                return result

    waiting = accept()
    job = waiting["run_acceptance"]["repair_job"]
    assert job["phase"] == "accepted"
    assert waiting["run_acceptance"]["review_budget"]["development_attempts"] == 1
    assert waiting["run_acceptance"]["review_budget"]["reviewer_invocations"] == 2
    assert "review_budget" not in job
    assert "review_budget_history" not in job
    branch = str(state["run_branch"])
    job_generation = job["repair_generation"]
    ci_budget = deepcopy(waiting["run_acceptance"]["ci_recovery"])
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", "-b", branch, str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")

    windows = []
    for index in (1, 2, 3):
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
        delivery["required_checks"] = ["pending"]
        delivery["check_position"] = 0
        fixture.write_text(json.dumps(data), encoding="utf-8")
        agents._reviews.append(_passing_artifact())

        previous_budget = deepcopy(waiting["run_acceptance"]["review_budget"])
        previous_history = deepcopy(waiting["run_acceptance"]["review_budget_history"])
        review_count = len(agents.review_requests)
        waiting = accept()
        job = waiting["run_acceptance"]["repair_job"]
        if index in (1, 3):
            assert waiting["status"] == "ready_for_human"
            assert job["phase"] == "blocked"
            assert job["blocked_reason"] == "review_budget_exhausted", json.dumps({"index": index, "resync": waiting["run_acceptance"]["repair_integration_budgets"]})
            assert waiting["run_acceptance"]["review_budget"]["reviewer_invocations"] == 2
            assert waiting["run_acceptance"]["review_budget"]["development_attempts"] == 1
            assert waiting["run_acceptance"]["review_budget"]["window"] == previous_budget["window"]
            assert waiting["run_acceptance"]["review_budget_history"] == previous_history
            assert len(agents.review_requests) == review_count
            assert job["repair_generation"] == job_generation
            assert "pr_number" not in job
            assert waiting["run_acceptance"]["ci_recovery"] == ci_budget
            candidate = job["candidate_sha"]
            renewed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
                str(state["run_id"]), explicit_resume=True, resume_budget_checkpoint=True,
            )
            renewed_job = renewed["run_acceptance"]["repair_job"]
            assert renewed_job["candidate_sha"] == candidate
            assert renewed_job["repair_generation"] == job_generation
            assert "pr_number" not in renewed_job
            assert renewed["run_acceptance"]["ci_recovery"] == ci_budget
            assert renewed["run_acceptance"]["review_budget"]["development_attempts"] == 1
            assert renewed["run_acceptance"]["review_budget"]["reviewer_invocations"] == 0
            assert renewed["run_acceptance"]["review_budget"]["window"] == previous_budget["window"] + 1
            assert len(renewed["run_acceptance"]["review_budget_history"]) == len(previous_history) + 1
            assert renewed["run_acceptance"]["review_budget_history"][-1]["review_budget"]["reviewer_invocations"] == 2
            waiting = accept()
            job = waiting["run_acceptance"]["repair_job"]
        else:
            assert waiting["run_acceptance"]["review_budget_history"] == previous_history
        assert job["phase"] == "accepted", job.get("blocked_reason")
        assert job["repair_generation"] == job_generation
        assert "pr_number" not in job
        assert git.is_ancestor(human_sha, str(job["candidate_sha"]))
        assert waiting["run_acceptance"]["ci_recovery"] == ci_budget
        assert "review_budget" not in job
        assert "review_budget_history" not in job
        assert waiting["run_acceptance"]["review_budget"]["development_attempts"] == 1
        assert waiting["run_acceptance"]["review_budget"]["reviewer_invocations"] == (2 if index == 2 else 1)
        windows.append(waiting["run_acceptance"]["review_budget"]["window"])

    interrupt = False
    completed = accept()
    assert completed["status"] == "run_publication_pending", completed.get("diagnostics")
    assert not json.loads(fixture.read_text())["delivery"].get("pull_requests")
    assert completed["run_acceptance"]["ci_recovery"] == ci_budget
    assert windows == [2, 2, 3]
    assert len(agents.review_requests) == 5  # Initial Run, first Repair, three appends.
    for index in (1, 2, 3):
        assert _git(git_repo, "show", f"{job['candidate_sha']}:human-{index}.txt") == f"human contribution {index}"


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

    save = states.save_run

    def stop_after_acceptance(run_id: str, value: dict) -> None:
        save(run_id, value)
        if value.get("run_acceptance", {}).get("repair_job", {}).get("phase") == "accepted":
            raise OSError("interrupt before Run Branch update")

    with monkeypatch.context() as pause:
        pause.setattr(states, "save_run", stop_after_acceptance)
        with pytest.raises(OSError, match="interrupt before Run Branch update"):
            accept()
    waiting = states.load_current_run(str(state["run_id"]))
    assert waiting is not None
    job = waiting["run_acceptance"]["repair_job"]
    assert job["phase"] == "accepted"
    original_generation = job["repair_generation"]
    original_candidate = job["candidate_sha"]
    original_ci = deepcopy(waiting["run_acceptance"]["ci_recovery"])
    review_count = len(agents.review_requests)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    if external_append:
        checkout = tmp_path / "human"
        _git(git_repo, "worktree", "add", "--detach", str(checkout), str(job["base_sha"]))
        (checkout / "human.txt").write_text("human contribution\n", encoding="utf-8")
        _git(checkout, "add", "human.txt")
        _git(checkout, "commit", "-m", "fix: append human contribution")
        human_sha = _git(checkout, "rev-parse", "HEAD")
        data["delivery"]["published_branches"][str(state["run_branch"])] = human_sha
    data["delivery"]["required_checks"] = ["none"]
    data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(data), encoding="utf-8")
    original_fetch = git.fetch_remote_commit
    failed = False

    def fail_once(branch: str, sha: str) -> str:
        nonlocal failed
        if branch == state["run_branch"] and not failed:
            failed = True
            raise GitError("temporary fetch transport failure")
        return original_fetch(branch, sha)

    monkeypatch.setattr(git, "fetch_remote_commit", fail_once)
    blocked = accept()
    assert failed
    blocked_job = blocked["run_acceptance"]["repair_job"]
    assert blocked["status"] == "ready_for_human"
    assert blocked["diagnostics"][0]["code"] == "run_integration_failed"
    assert "run_repair_integration_history_rewrite" not in blocked_job
    assert blocked_job["repair_generation"] == original_generation
    assert "pr_number" not in blocked_job
    assert blocked_job["candidate_sha"] == original_candidate
    monkeypatch.setattr(git, "fetch_remote_commit", original_fetch)

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"]), explicit_resume=True,
    )
    assert resumed["run_acceptance"]["repair_job"]["phase"] != "blocked"
    if external_append:
        agents._reviews.append(_passing_artifact())
    completed = accept()
    assert completed["status"] == "run_publication_pending"
    assert "repair_pr_number" not in completed["run_acceptance"]
    assert not json.loads(fixture.read_text())["delivery"].get("pull_requests")
    assert completed["run_acceptance"]["ci_recovery"] == original_ci
    assert len(agents.review_requests) == review_count + int(external_append)
