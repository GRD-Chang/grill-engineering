from __future__ import annotations

import subprocess
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine

from support.workspace import prepare_workspace
from conftest import seed_idle_control
from test_cli import run_cli

from run_acceptance_test_support import (
    _candidate_finding_artifact,
    _completed_run,
    _passing_artifact,
    _sync_completed_ticket_integrated_sha,
)

@pytest.mark.parametrize(
    ("latest_combination_has_finding", "external_base_drift"),
    [(False, False), (True, False), (False, True)],
)
def test_merged_conflict_candidate_default_drift_stays_in_same_repair_cycle(
    git_repo: Path,
    latest_combination_has_finding: bool,
    external_base_drift: bool,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, git = _completed_run(repo, state_root=workspace.state_root)
    if not latest_combination_has_finding:
        seed_idle_control(
            TaskControlStore(states.root),
            TaskKey(repo, str(state["repository"]), 1),
            str(state["run_id"]),
            state_dir=states.root,
        )
    fixture = repo / "github.json"
    run_branch = str(state["run_branch"])

    subprocess.run(["git", "switch", run_branch], cwd=repo, check=True)
    (repo / "shared.txt").write_text("run branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change shared file on run branch"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    run_head = git.resolve(run_branch)
    subprocess.run(["git", "switch", "main"], cwd=repo, check=True)
    (repo / "shared.txt").write_text("default branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change shared file on default branch"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    initial_default = git.resolve("main")
    _sync_completed_ticket_integrated_sha(state, git, run_head)
    states.save_run(str(state["run_id"]), state)
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["default_head_sha"] = initial_default
    fixture_data.setdefault("delivery", {}).setdefault("published_branches", {})[
        run_branch
    ] = run_head
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    class ConflictThenFreshAgents:
        def __init__(self) -> None:
            self.development_requests: list[dict[str, Any]] = []
            self.review_requests: list[dict[str, Any]] = []
            self._reviews = [_passing_artifact()]
            self._reviews.extend(
                [_candidate_finding_artifact(), _candidate_finding_artifact()]
                if latest_combination_has_finding
                else [_passing_artifact()]
            )

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            if len(self.development_requests) == 1:
                assert request["repair_source"] == "merge_conflict"
                (checkout / "shared.txt").write_text("resolved\n", encoding="utf-8")
            else:
                assert request["repair_source"] == "acceptance"
                (checkout / "near-budget-repair.txt").write_text(
                    "tenth modification\n", encoding="utf-8"
                )
            return DevelopmentResult("conflict-developer", "Repaired latest finding.")

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            checkout = Path(str(request["checkout"]))
            assert (checkout / "shared.txt").read_text(encoding="utf-8") == "resolved\n"
            assert request["candidate_acceptance"] is True
            if len(self.review_requests) > 1:
                assert (checkout / "default-after-merge.txt").read_text(
                    encoding="utf-8"
                ) == "advanced\n"
            return ReviewResult(
                f"reviewer-{len(self.review_requests)}", self._reviews.pop(0)
            )

        def publication(self, _request: dict[str, Any]) -> dict[str, str]:
            return {
                "commit_message": "fix(run): resolve default conflict",
                "pr_title": "fix(run): resolve default conflict",
                "pr_body_markdown": (
                    "## What Problem This Solves\n\nThe Run conflicted.\n\n"
                    "## Why This Change Was Made\n\nThe exact merge was resolved.\n\n"
                    "## User Impact\n\nThe Run is mergeable.\n\n"
                    "## Evidence\n\nFresh acceptance passed."
                ),
            }

    class DriftAfterMergePublisher(FixtureGitHubPublisher):
        def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
            super().sync_run_branch(run_branch=run_branch, integrated_sha=integrated_sha)
            (repo / "default-after-merge.txt").write_text(
                "advanced\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "default-after-merge.txt"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "advance default after conflict merge"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["default_head_sha"] = git.resolve("main")
            fixture.write_text(json.dumps(data), encoding="utf-8")

    agents = ConflictThenFreshAgents()
    first = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=DriftAfterMergePublisher(fixture, git),
        default_head_sha=initial_default,
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    active_run = first["run_acceptance"]
    integrated = git.resolve(run_branch)
    latest_default = git.resolve("main")
    assert first["status"] == "run_acceptance_pending"
    assert first["terminal_kind"] == "run_repair_pending"
    assert active_run["phase"] == "repairing"
    active_job = active_run["repair_job"]
    generation = active_run["repair_generation"]
    thread_id = active_job["development_thread_id"]
    repair_checkout = Path(str(active_job["repair_checkout"]))
    assert active_job["phase"] == "candidate"
    assert active_job["base_sha"] == integrated
    assert active_job["candidate_sha"] == integrated
    assert active_job["default_base_sha"] == latest_default
    assert active_job["repair_mode"] == "squash"
    revalidation_merge = active_job["integrated_revalidation_merge"]
    assert set(revalidation_merge) == {
        "base_sha",
        "default_base_sha",
        "candidate_sha",
        "publication_sha",
    }
    assert all(isinstance(sha, str) and sha for sha in revalidation_merge.values())
    assert revalidation_merge["publication_sha"] == active_job["publication_sha"]
    assert active_run["repair_cycle"]["code_modification_attempts"] == 1
    assert repair_checkout.exists()
    assert len(agents.review_requests) == 1

    if external_base_drift:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        pull = data["delivery"]["pull_requests"][0]
        pull["base_branch"] = "main"
        mutations = deepcopy(data["delivery"].get("mutations", []))
        fixture.write_text(json.dumps(data), encoding="utf-8")
        empty_agents = repo / "blocked-revalidation-agents.json"
        empty_agents.write_text("{}", encoding="utf-8")

        blocked = run_cli(
            repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(empty_agents),
        )

        assert blocked.returncode == 2
        blocked_state = states.load_current_run(str(state["run_id"]))
        assert blocked_state is not None
        assert blocked_state["status"] == "blocked"
        assert blocked_state["diagnostics"][0]["code"] == (
            "change_pr_base_changed_externally"
        )
        after = json.loads(fixture.read_text(encoding="utf-8"))
        assert after["delivery"].get("mutations", []) == mutations
        assert len(agents.review_requests) == 1
        return

    if latest_combination_has_finding:
        # Model a Cycle already close to its ten-change ceiling.  The
        # revalidation Finding must consume the same budget instead of opening
        # a fresh Cycle.
        active_job["modification_attempts"] = 9
        active_job["code_modification_attempts"] = 9
        active_job["review_budget"]["development_attempts"] = 9
        active_run["repair_cycle"]["code_modification_attempts"] = 9
        active_run["modification_attempts"] = 9
        active_run["code_modification_attempts"] = 9
        states.save_run(str(state["run_id"]), first)

    if latest_combination_has_finding:
        exhausted = RunAcceptanceEngine(
            git=git,
            states=states,
            agents=agents,
            github=FixtureGitHubPublisher(fixture, git),
            default_head_sha=latest_default,
            currentness_reader=FixtureGitHubReader(fixture),
        ).accept(str(state["run_id"]))
    else:
        revalidation_agents = repo / "revalidation-agents.json"
        revalidation_agents.write_text(
            json.dumps(
                {
                    "run_reviews": [
                        {
                            "thread_id": "revalidation-reviewer",
                            **_passing_artifact(),
                        }
                    ],
                    "run_publications": [
                        {
                            "commit_message": "fix(run): complete revalidation",
                            "pr_title": "fix(run): complete revalidation",
                            "pr_body_markdown": (
                                "## What Problem This Solves\n\n"
                                "The merged repair must survive default drift.\n\n"
                                "## Why This Change Was Made\n\n"
                                "The public lifecycle revalidated the persisted repair.\n\n"
                                "## User Impact\n\n"
                                "The final Run can proceed to approval.\n\n"
                                "## Evidence\n\n"
                                "A separate CLI process promoted the revalidated Candidate."
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        resumed = run_cli(
            repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(revalidation_agents),
        )
        assert resumed.returncode == 0, resumed.stdout
        exhausted = states.load_current_run(str(state["run_id"]))
        assert exhausted is not None

    exhausted_run = exhausted["run_acceptance"]
    if not latest_combination_has_finding:
        assert exhausted["status"] == "run_approval_pending"
        assert exhausted_run["phase"] == "accepted"
        assert exhausted_run["repair_generation"] == generation
        assert exhausted_run["repair_cycle"]["generation"] == generation
        assert exhausted_run["repair_cycle"]["status"] == "promoted"
        assert exhausted_run["repair_cycle"]["code_modification_attempts"] == 1
        assert exhausted_run["development_thread_history"] == [thread_id]
        assert not repair_checkout.exists()
        assert len(agents.development_requests) == 1
        return

    assert exhausted["status"] == "ready_for_human"
    assert exhausted["terminal_kind"] == "waiting_human"
    assert exhausted_run["repair_generation"] == generation
    assert exhausted_run["repair_cycle"]["generation"] == generation
    assert exhausted_run["repair_cycle"]["status"] == "budget_exhausted"
    assert exhausted_run["repair_cycle"]["code_modification_attempts"] == 10
    assert exhausted_run["repair_job"]["development_thread_id"] == thread_id
    assert exhausted_run["repair_job"]["repair_checkout"] == str(repair_checkout)
    assert agents.development_requests[-1]["checkout"] == str(repair_checkout)
    assert not repair_checkout.exists()
    assert len(agents.development_requests) == 2
    assert len(agents.review_requests) == 3
@pytest.mark.parametrize("dirty_kind", ["unstaged", "untracked"])
def test_interrupted_staged_conflict_recovery_rejects_other_dirty_changes(
    git_repo: Path, dirty_kind: str
) -> None:
    git = GitRepository(git_repo)
    (git_repo / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add shared base"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "switch", "-c", "run"], cwd=git_repo, check=True)
    (git_repo / "shared.txt").write_text("run\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change shared on run"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    run_head = git.resolve("run")
    subprocess.run(["git", "switch", "main"], cwd=git_repo, check=True)
    (git_repo / "shared.txt").write_text("default\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change shared on default"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    default_head = git.resolve("main")
    checkout = git_repo.parent / f"repair-{dirty_kind}"
    git.prepare_ticket_checkout(
        branch=f"agent-run-repair/test-{dirty_kind}",
        base_sha=run_head,
        checkout=checkout,
    )
    try:
        git.prepare_integration_repair_checkout(
            checkout,
            run_head_sha=run_head,
            default_head_sha=default_head,
        )
        (checkout / "shared.txt").write_text("resolved\n", encoding="utf-8")
        subprocess.run(["git", "add", "shared.txt"], cwd=checkout, check=True)
        if dirty_kind == "unstaged":
            (checkout / "shared.txt").write_text(
                "changed after staging\n", encoding="utf-8"
            )
        else:
            (checkout / "untracked.txt").write_text("foreign\n", encoding="utf-8")

        with pytest.raises(GitError, match="contains unstaged changes"):
            git.prepare_integration_repair_checkout(
                checkout,
                run_head_sha=run_head,
                default_head_sha=default_head,
                allow_staged_resolution=True,
            )

        assert git.checkout_head(checkout) == run_head
        assert subprocess.run(
            ["git", "rev-parse", "MERGE_HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip() == default_head
    finally:
        git.remove_worktree(checkout)
