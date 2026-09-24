"""Run Repair collaboration against a second clone and durable PR state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import _candidate_finding_artifact, _passing_artifact
from support.merge_resolution import MergeResolutionFixture
from support.run_scenarios import _completed_run
from support.run_publication import RunPublicationAgents

@pytest.mark.parametrize(
    "scenario", [
        "source_and_target", "conflict", "target_only",
        "target_interrupted", "target_interrupted_rewritten",
        "target_interrupted_fetch_retry",
    ]
)
def test_run_repair_accepts_a_second_clone_append_on_its_existing_pr(
    git_repo: Path, tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conflict = scenario == "conflict"
    interrupted = scenario.startswith("target_interrupted")
    append_source = scenario == "source_and_target" or conflict
    append_target = scenario != "conflict"
    state, states, git = _completed_run(
        git_repo, delivery_policy=DeliveryPolicy(run_repair_rounds=1)
    )
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    class AppendAgents(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "merge_conflict":
                result = super().develop(request)
                if interrupted and len(self.development_requests) == 2:
                    (Path(str(request["checkout"])) / "follow-up-repair.txt").write_text(
                        "second repair\n", encoding="utf-8"
                    )
                return result
            self.development_requests.append(request)
            (Path(str(request["checkout"])) / "run-repair.txt").write_text(
                "repaired with human change\n", encoding="utf-8"
            )
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary="Resolved both repair changes.",
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            if request.get("candidate_acceptance") is True:
                content = (Path(str(request["checkout"])) / "run-repair.txt").read_text(
                    encoding="utf-8"
                )
                assert content == (
                    "repaired with human change\n" if conflict and len(self.review_requests) > 2
                    else "repaired\n"
                )
            return ReviewResult(
                thread_id=f"run-reviewer-{len(self.review_requests)}",
                artifact=self._reviews.pop(0),
            )

    agents = AppendAgents()
    first = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    job = first["run_acceptance"]["repair_job"]
    assert job["phase"] == "waiting_checks"
    branch = str(job["repair_branch"])
    original_publication = str(job["publication_sha"])
    original_pr = int(job["pr_number"])
    original_ci = json.loads(json.dumps(job["ci_recovery"]))

    def command(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    command(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    command(git_repo, "remote", "add", "origin", str(remote))
    command(git_repo, "push", "origin", f"{original_publication}:refs/heads/{branch}")
    command(git_repo, "push", "origin", f"{job['base_sha']}:refs/heads/{state['run_branch']}")
    collaborator = tmp_path / "collaborator"
    command(tmp_path, "clone", str(remote), str(collaborator))
    command(collaborator, "config", "user.name", "Collaborator")
    command(collaborator, "config", "user.email", "collaborator@example.invalid")
    command(collaborator, "checkout", branch)
    human_file = (
        "run-repair.txt" if conflict else
        "human-target.txt" if scenario == "target_only" or interrupted
        else "human.txt"
    )
    human_sha = original_publication
    if append_source:
        (collaborator / human_file).write_text("human repair\n", encoding="utf-8")
        command(collaborator, "add", human_file)
        command(collaborator, "commit", "-m", "fix: complete repair manually")
        human_sha = command(collaborator, "rev-parse", "HEAD")
        command(collaborator, "push", "origin", branch)

    target_sha = None
    if append_target:
        # The Run branch advances only on the remote; the local managed ref
        # stays at the previously confirmed boundary during Repair review.
        command(collaborator, "checkout", str(state["run_branch"]))
        (collaborator / "human-target.txt").write_text("human target\n", encoding="utf-8")
        command(collaborator, "add", "human-target.txt")
        command(collaborator, "commit", "-m", "fix: append to Run branch")
        target_sha = command(collaborator, "rev-parse", "HEAD")
        command(collaborator, "push", "origin", str(state["run_branch"]))

    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data["delivery"]
    delivery["published_branches"][branch] = human_sha
    if target_sha is not None:
        delivery["published_branches"][str(state["run_branch"])] = target_sha
        delivery["pull_requests"][0]["base_sha"] = target_sha
    delivery["pull_requests"][0]["head_sha"] = human_sha
    delivery["required_checks"] = ["none"]
    delivery["check_position"] = 0
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents._reviews = (
        [_candidate_finding_artifact(), _passing_artifact()]
        if interrupted else [_passing_artifact()]
    )
    if interrupted:
        save_run = states.save_run

        def interrupt_after_candidate(run_id: str, value: dict[str, Any]) -> None:
            save_run(run_id, value)
            repair = value.get("run_acceptance", {}).get("repair_job", {})
            if repair.get("phase") == "candidate" and len(agents.development_requests) == 2:
                raise OSError("interrupted after new repair Candidate was saved")

        with monkeypatch.context() as interruption:
            interruption.setattr(states, "save_run", interrupt_after_candidate)
            with pytest.raises(OSError, match="interrupted after new repair Candidate"):
                RunAcceptanceEngine(
                    git=git, states=states, agents=agents,
                    github=FixtureGitHubPublisher(fixture, git),
                ).accept(str(state["run_id"]))
        saved = states.load_current_run(str(state["run_id"]))
        assert saved is not None
        saved_job = saved["run_acceptance"]["repair_job"]
        assert saved_job["phase"] == "candidate"
        assert git.resolve(str(state["run_branch"])) != target_sha
        assert saved_job["base_sha"] == target_sha
        assert saved_job["pr_number"] == original_pr
        assert saved_job["ci_recovery"] == original_ci
        if scenario == "target_interrupted_fetch_retry":
            (collaborator / "later-target.txt").write_text(
                "later target\n", encoding="utf-8"
            )
            command(collaborator, "add", "later-target.txt")
            command(collaborator, "commit", "-m", "fix: append after interruption")
            later_target = command(collaborator, "rev-parse", "HEAD")
            command(collaborator, "push", "origin", str(state["run_branch"]))
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["delivery"]["published_branches"][str(state["run_branch"])] = later_target
            data["delivery"]["pull_requests"][0]["base_sha"] = later_target
            fixture.write_text(json.dumps(data), encoding="utf-8")
        if scenario == "target_interrupted_rewritten":
            rewritten = git.resolve("main")
            command(
                git_repo, "push", "--force", "origin",
                f"{rewritten}:refs/heads/{state['run_branch']}",
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["delivery"]["published_branches"][str(state["run_branch"])] = rewritten
            data["delivery"]["pull_requests"][0]["base_sha"] = rewritten
            fixture.write_text(json.dumps(data), encoding="utf-8")
            blocked, _ = Controller(
                FixtureGitHubReader(fixture), git, states
            ).resume(str(state["run_id"]))
            assert blocked["status"] == "blocked"
            assert blocked["diagnostics"][0]["code"] == "change_pr_base_changed_externally"
            assert blocked["run_acceptance"]["repair_job"]["pr_number"] == original_pr
            assert command(
                git_repo, "ls-remote", "origin", f"refs/heads/{state['run_branch']}"
            ).split()[0] == rewritten
            return
        if scenario == "target_interrupted_fetch_retry":
            fetch = git.fetch_remote_commit
            failed = False

            def fail_once(branch: str, sha: str) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise GitError("temporary fetch failure")
                fetch(branch, sha)

            with monkeypatch.context() as transient:
                transient.setattr(git, "fetch_remote_commit", fail_once)
                resumed, resumed_existing = Controller(
                    FixtureGitHubReader(fixture), git, states
                ).resume(str(state["run_id"]))
            assert failed
        else:
            resumed, resumed_existing = Controller(
                FixtureGitHubReader(fixture), git, states
            ).resume(str(state["run_id"]))
        assert resumed_existing
        assert resumed["status"] == "run_acceptance_pending", resumed.get("diagnostics")
        resumed_job = resumed["run_acceptance"]["repair_job"]
        assert resumed_job["pr_number"] == original_pr
        assert resumed_job["ci_recovery"] == original_ci
        assert resumed_job["candidate_sha"] == saved_job["candidate_sha"]
    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending", (
        result["status"], result.get("diagnostics"),
        result["run_acceptance"].get("repair_job", {}).get("blocked_reason"),
        result.get("terminal_kind"), result["run_acceptance"].get("phase"),
        result["run_acceptance"].get("discarded_repair_thread_ids"),
    )
    completed = result["run_acceptance"]["completed_repair_jobs"][0]
    assert result["run_acceptance"]["repair_pr_number"] == original_pr
    assert git.is_ancestor(human_sha, completed["publication_sha"])
    assert len(agents.development_requests) == (
        2 if conflict or interrupted else 1
    )
    assert sum(
        request.get("candidate_acceptance") is True
        for request in agents.review_requests
    ) == (3 if interrupted else 2)
    assert result["run_acceptance"]["ci_recovery"] == original_ci
    merged_content = command(git_repo, "show", f"{result['run_branch']}:{human_file}")
    assert merged_content == (
        "repaired with human change" if conflict else
        "human target" if scenario == "target_only" or interrupted
        else "human repair"
    )
    if target_sha is not None:
        assert command(git_repo, "show", f"{result['run_branch']}:human-target.txt") == "human target"
        final_engine = RunPublicationEngine(
            git=git, states=states, agents=RunPublicationAgents(),
            github=FixtureGitHubPublisher(fixture, git),
            default_branch="main", default_head_sha=str(state["base"]["sha"]),
        )
        assert final_engine.publish(str(state["run_id"]))["status"] == "run_approval_pending"
        assert final_engine.approve(str(state["run_id"]))["status"] == "completed"
        assert command(git_repo, "show", "main:human-target.txt") == "human target"
        if interrupted:
            assert command(git_repo, "show", "main:follow-up-repair.txt") == "second repair"
        if scenario == "target_interrupted_fetch_retry":
            assert command(git_repo, "show", "main:later-target.txt") == "later target"


@pytest.mark.parametrize("rewritten_branch", ["source", "target"])
def test_run_repair_pauses_when_published_source_history_is_rewritten(
    git_repo: Path, rewritten_branch: str,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    waiting = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    job = waiting["run_acceptance"]["repair_job"]
    assert job["phase"] == "waiting_checks"
    published = str(job["published_sha"])
    original_pr = int(job["pr_number"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data["delivery"]
    assert delivery["published_branches"][str(job["repair_branch"])] == published
    if rewritten_branch == "source":
        delivery["published_branches"][str(job["repair_branch"])] = str(job["base_sha"])
        delivery["pull_requests"][0]["head_sha"] = str(job["base_sha"])
    else:
        rewritten = git.resolve("main")
        delivery["published_branches"][str(state["run_branch"])] = rewritten
        delivery["pull_requests"][0]["base_sha"] = rewritten
    fixture.write_text(json.dumps(data), encoding="utf-8")

    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    blocked = result["run_acceptance"]["repair_job"]
    assert result["status"] == "ready_for_human"
    if rewritten_branch == "source":
        assert blocked["blocked_reason"] == "remote_source_changed"
    else:
        # Run Acceptance owns the Run branch as its source. It rejects the
        # rewritten target before re-entering the nested Repair Job.
        assert result["diagnostics"][0]["code"] == "integration_history_rewritten"
        assert blocked["phase"] == "waiting_checks"
    assert blocked["published_sha"] == published
    assert blocked["pr_number"] == original_pr
    assert len(agents.development_requests) == 1


def test_merge_resolution_repair_promotes_integrated_remote_append(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "happy_path")
    data = json.loads(fixture.fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.fixture.write_text(json.dumps(data), encoding="utf-8")
    waiting = fixture.accept()
    job = waiting["run_acceptance"]["repair_job"]
    assert job["repair_mode"] == "merge_resolution"
    assert job["phase"] == "waiting_checks"
    first_publication = str(job["publication_sha"])
    branch = str(job["repair_branch"])
    pr_number = int(job["pr_number"])

    def command(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    command(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    command(git_repo, "remote", "add", "origin", str(remote))
    command(git_repo, "push", "origin", f"{first_publication}:refs/heads/{branch}")
    command(git_repo, "push", "origin", f"{job['base_sha']}:refs/heads/{fixture.run_branch}")
    collaborator = tmp_path / "collaborator"
    command(tmp_path, "clone", str(remote), str(collaborator))
    command(collaborator, "config", "user.name", "Collaborator")
    command(collaborator, "config", "user.email", "collaborator@example.invalid")
    command(collaborator, "checkout", branch)
    (collaborator / "human.txt").write_text("human repair\n", encoding="utf-8")
    command(collaborator, "add", "human.txt")
    command(collaborator, "commit", "-m", "fix: complete repair manually")
    human_sha = command(collaborator, "rev-parse", "HEAD")
    command(collaborator, "push", "origin", branch)

    data = json.loads(fixture.fixture.read_text(encoding="utf-8"))
    delivery = data["delivery"]
    delivery["published_branches"][branch] = human_sha
    delivery["pull_requests"][0]["head_sha"] = human_sha
    delivery["required_checks"] = ["none"]
    delivery["check_position"] = 0
    fixture.fixture.write_text(json.dumps(data), encoding="utf-8")
    promoted = fixture.accept()

    assert promoted["status"] == "run_publication_pending"
    run = promoted["run_acceptance"]
    completed = run["completed_repair_jobs"][-1]
    publication = str(completed["publication_sha"])
    assert completed["candidate_sha"] == run["candidate_sha"]
    assert run["repair_pr_number"] == pr_number
    assert fixture.git.commit_parents(publication) == [human_sha, fixture.default_head]
    assert fixture.git.is_ancestor(first_publication, publication)
    assert fixture.git.resolve(f"{publication}^{{tree}}") == fixture.git.resolve(
        f"{run['candidate_sha']}^{{tree}}"
    )
    assert command(git_repo, "show", f"{fixture.run_branch}:human.txt") == "human repair"
    final_engine = RunPublicationEngine(
        git=fixture.git, states=fixture.states, agents=RunPublicationAgents(),
        github=FixtureGitHubPublisher(fixture.fixture, fixture.git),
        default_branch="main", default_head_sha=fixture.default_head,
    )
    published = final_engine.publish(str(fixture.state["run_id"]))
    assert published["status"] == "run_approval_pending"
    approved = final_engine.approve(str(fixture.state["run_id"]))
    assert approved["status"] == "completed"
    assert command(git_repo, "show", "main:human.txt") == "human repair"


def test_run_repair_integrates_remote_append_before_pr_creation(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["crash_after_ensure_change_branch_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    with pytest.raises(OSError, match="lost response"):
        RunAcceptanceEngine(
            git=git, states=states, agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
        ).accept(str(state["run_id"]))
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    job = saved["run_acceptance"]["repair_job"]
    branch = str(job["repair_branch"])

    def command(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    command(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    command(git_repo, "remote", "add", "origin", str(remote))
    command(git_repo, "push", "origin", f"{job['base_sha']}:refs/heads/{branch}")
    collaborator = tmp_path / "collaborator"
    command(tmp_path, "clone", str(remote), str(collaborator))
    command(collaborator, "config", "user.name", "Collaborator")
    command(collaborator, "config", "user.email", "collaborator@example.invalid")
    command(collaborator, "checkout", branch)
    (collaborator / "human-before-pr.txt").write_text("human\n", encoding="utf-8")
    command(collaborator, "add", "human-before-pr.txt")
    command(collaborator, "commit", "-m", "fix: append before PR")
    human_sha = command(collaborator, "rev-parse", "HEAD")
    command(collaborator, "push", "origin", branch)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["published_branches"][branch] = human_sha
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents._reviews.append(_passing_artifact())

    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    completed = result["run_acceptance"]["completed_repair_jobs"][0]
    assert git.is_ancestor(human_sha, completed["publication_sha"])
    assert command(git_repo, "show", f"{result['run_branch']}:human-before-pr.txt") == "human"
    assert len(agents.development_requests) == 1
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1


def test_run_repair_does_not_accept_an_append_from_a_foreign_pr(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = ScriptedRunAgents()
    waiting = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    job = waiting["run_acceptance"]["repair_job"]
    published = str(job["published_sha"])
    appended = subprocess.run(
        ["git", "commit-tree", git.resolve(f"{published}^{{tree}}"),
         "-p", published, "-m", "fix: append from another writer"],
        cwd=git_repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data["delivery"]
    delivery["published_branches"][str(job["repair_branch"])] = appended
    delivery["pull_requests"][0].update({
        "head_sha": appended,
        "base_repository": "foreign/project",
    })
    fixture.write_text(json.dumps(data), encoding="utf-8")
    reviews_before = len(agents.review_requests)

    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    blocked = result["run_acceptance"]["repair_job"]
    assert result["status"] == "ready_for_human"
    assert blocked["blocked_reason"] == "published_head_mismatch"
    assert blocked["published_sha"] == published
    assert len(agents.review_requests) == reviews_before
    assert len(delivery["pull_requests"]) == 1
